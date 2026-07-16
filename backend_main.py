"""
대전 레이더 — FastAPI 백엔드

우리 팀의 local.db(place / community_request / community_response) 스키마를
그대로 서빙하는 API 서버입니다.

실행 방법
---------
1. pip install fastapi "uvicorn[standard]" python-dotenv requests
2. 이 파일과 같은 위치에 .env 파일을 만들고 아래처럼 DB 경로를 지정합니다.
     DB_PATH=./local.db
   (.env는 반드시 .gitignore에 등록 — RFP 필수 요건)
3. local.db 파일을 이 파일과 같은 폴더에 둡니다.
   (Colab에서 만든 파일을 Google Drive에서 다운로드해서 옮기면 됩니다)
4. uvicorn backend_main:app --reload --port 8000
5. http://localhost:8000/docs 에서 Swagger로 바로 테스트 가능합니다.
"""

import os
import sqlite3
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

load_dotenv()

DB_PATH = Path(os.getenv("DB_PATH", "local.db"))

app = FastAPI(title="대전 레이더 API", version="1.0")

# 개발 중에는 전체 허용, 배포 시 Netlify 도메인으로 좁히세요.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve frontend static files (radar_implement) and mount as /static
FRONTEND_DIR = Path(__file__).parent / "radar_implement"
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

# Serve the main HTML at the root path.
@app.get("/", include_in_schema=False)
def serve_frontend():
    index = FRONTEND_DIR / "index_v1.html"
    if index.exists():
        return FileResponse(str(index))
    raise HTTPException(status_code=404, detail="Not Found")

# =========================================================
# 라우터 연결
# =========================================================
from app.chat.router import router as chat_router
app.include_router(chat_router)

# =========================================================
# DB 연결
# =========================================================

def get_db():
    if not DB_PATH.exists():
        raise HTTPException(
            status_code=500,
            detail=f"DB 파일을 찾을 수 없습니다: {DB_PATH}",
        )
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
    finally:
        connection.close()


# =========================================================
# 공통 유틸
# =========================================================

def place_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "contentid": row["contentid"],
        "contenttypeid": row["contenttypeid"],
        "title": row["title"],
        "addr1": row["addr1"],
        "firstimage": row["firstimage"],
        "firstimage2": row["firstimage2"],
        "latitude": row["mapy"],
        "longitude": row["mapx"],
    }


def weather_code_to_korean(code: int) -> str:
    table = {
        0: "맑음", 1: "대체로 맑음", 2: "부분적으로 흐림", 3: "흐림",
        45: "안개", 48: "서리 안개",
        51: "약한 이슬비", 53: "이슬비", 55: "강한 이슬비",
        56: "약한 어는 이슬비", 57: "강한 어는 이슬비",
        61: "약한 비", 63: "비", 65: "강한 비",
        66: "약한 어는 비", 67: "강한 어는 비",
        71: "약한 눈", 73: "눈", 75: "강한 눈", 77: "싸락눈",
        80: "약한 소나기", 81: "소나기", 82: "강한 소나기",
        85: "약한 눈 소나기", 86: "강한 눈 소나기",
        95: "천둥번개", 96: "약한 우박 동반 천둥번개", 99: "강한 우박 동반 천둥번개",
    }
    return table.get(code, f"알 수 없는 날씨 코드({code})")


def weather_code_to_icon(code: int) -> str:
    if code == 0:
        return "☀️"
    if code in (1, 2):
        return "🌤️"
    if code == 3:
        return "☁️"
    if code in (45, 48):
        return "🌫️"
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return "🌧️"
    if code in (71, 73, 75, 77, 85, 86):
        return "🌨️"
    if code in (95, 96, 99):
        return "⛈️"
    return "🌡️"


def fetch_weather(latitude: float, longitude: float) -> dict:
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": [
            "temperature_2m", "apparent_temperature",
            "relative_humidity_2m", "precipitation",
            "weather_code", "wind_speed_10m",
        ],
        "timezone": "Asia/Seoul",
    }
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="외부 날씨 API 요청에 실패했습니다.")

    current = response.json().get("current")
    if current is None:
        raise HTTPException(status_code=502, detail="날씨 API 응답에 current 데이터가 없습니다.")

    code = int(current.get("weather_code", -1))

    return {
        "time": current.get("time"),
        "weather_code": code,
        "weather_description": weather_code_to_korean(code),
        "weather_icon": weather_code_to_icon(code),
        "temperature": current.get("temperature_2m"),
        "apparent_temperature": current.get("apparent_temperature"),
        "relative_humidity": current.get("relative_humidity_2m"),
        "precipitation": current.get("precipitation"),
        "wind_speed": current.get("wind_speed_10m"),
        "units": {
            "temperature": "°C",
            "apparent_temperature": "°C",
            "relative_humidity": "%",
            "precipitation": "mm",
            "wind_speed": "km/h",
        },
    }


# =========================================================
# 1. 장소 (place) — 노트북 API 명세 그대로
# =========================================================

@app.get("/places")
def list_places(
    contenttypeid: Optional[int] = None,
    keyword: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: sqlite3.Connection = Depends(get_db),
):
    query = "SELECT * FROM place WHERE 1=1"
    params: list = []

    if contenttypeid is not None:
        query += " AND contenttypeid = ?"
        params.append(contenttypeid)

    if keyword:
        query += " AND (title LIKE ? OR addr1 LIKE ?)"
        like = f"%{keyword}%"
        params.extend([like, like])

    query += " LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = db.execute(query, params).fetchall()
    return {"count": len(rows), "places": [place_row_to_dict(r) for r in rows]}


@app.get("/places/bounds")
def places_in_bounds(
    south: float = Query(..., ge=-90, le=90),
    west: float = Query(..., ge=-180, le=180),
    north: float = Query(..., ge=-90, le=90),
    east: float = Query(..., ge=-180, le=180),
    contenttypeid: Optional[int] = None,
    limit: int = Query(500, ge=1, le=2000),
    db: sqlite3.Connection = Depends(get_db),
):
    query = """
        SELECT * FROM place
        WHERE mapy BETWEEN ? AND ?
          AND mapx BETWEEN ? AND ?
    """
    params: list = [south, north, west, east]

    if contenttypeid is not None:
        query += " AND contenttypeid = ?"
        params.append(contenttypeid)

    query += " LIMIT ?"
    params.append(limit)

    rows = db.execute(query, params).fetchall()
    return {"count": len(rows), "places": [place_row_to_dict(r) for r in rows]}


@app.get("/places/nearest")
def nearest_place(
    latitude: float = Query(..., ge=-90, le=90),
    longitude: float = Query(..., ge=-180, le=180),
    db: sqlite3.Connection = Depends(get_db),
):
    row = db.execute(
        """
        SELECT *,
            ((mapy - ?) * (mapy - ?)) + ((mapx - ?) * (mapx - ?)) AS dist
        FROM place
        ORDER BY dist ASC
        LIMIT 1
        """,
        (latitude, latitude, longitude, longitude),
    ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다.")

    return place_row_to_dict(row)


@app.get("/places/{contentid}")
def place_detail(contentid: str, db: sqlite3.Connection = Depends(get_db)):
    row = db.execute(
        "SELECT * FROM place WHERE contentid = ?", (contentid,)
    ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다.")

    return place_row_to_dict(row)


# =========================================================
# 2. 날씨 / 위치 통합 조회
# =========================================================

@app.get("/weather/current")
def weather_current(
    latitude: float = Query(..., ge=-90, le=90),
    longitude: float = Query(..., ge=-180, le=180),
):
    weather = fetch_weather(latitude, longitude)
    return {"latitude": latitude, "longitude": longitude, **weather}


@app.get("/location-info")
def location_info(
    latitude: float = Query(..., ge=-90, le=90),
    longitude: float = Query(..., ge=-180, le=180),
    db: sqlite3.Connection = Depends(get_db),
):
    place_row = db.execute(
        """
        SELECT *,
            ((mapy - ?) * (mapy - ?)) + ((mapx - ?) * (mapx - ?)) AS dist
        FROM place
        ORDER BY dist ASC
        LIMIT 1
        """,
        (latitude, latitude, longitude, longitude),
    ).fetchone()

    weather = fetch_weather(latitude, longitude)

    return {
        "selected_location": {"latitude": latitude, "longitude": longitude},
        "nearest_place": place_row_to_dict(place_row) if place_row else None,
        "weather": weather,
    }


# =========================================================
# 3. 커뮤니티 요청 (community_request)
# =========================================================

class RequestCreate(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)
    requested_facility: str = Field(..., min_length=1, max_length=50)
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    address: Optional[str] = None
    location_input_type: str = Field(..., pattern="^(GPS|ADDRESS)$")
    password: str = Field(..., min_length=1, max_length=50)


class PasswordCheck(BaseModel):
    password: str


def request_row_to_dict(row: sqlite3.Row, response_count: Optional[int] = None) -> dict:
    data = {
        "request_id": row["request_id"],
        "question": row["question"],
        "requested_facility": row["requested_facility"],
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "address": row["address"],
        "location_input_type": row["location_input_type"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if response_count is not None:
        data["response_count"] = response_count
    return data


def response_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "response_id": row["response_id"],
        "request_id": row["request_id"],
        "facility_name": row["facility_name"],
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "address": row["address"],
        "description": row["description"],
        "place_contentid": row["place_contentid"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@app.get("/requests")
def list_requests(
    requested_facility: Optional[str] = None,
    keyword: Optional[str] = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: sqlite3.Connection = Depends(get_db),
):
    query = """
        SELECT r.*,
               (SELECT COUNT(*) FROM community_response resp
                WHERE resp.request_id = r.request_id) AS response_count
        FROM community_request r
        WHERE 1=1
    """
    params: list = []

    if requested_facility:
        query += " AND r.requested_facility = ?"
        params.append(requested_facility)

    if keyword:
        query += " AND (r.question LIKE ?)"
        params.append(f"%{keyword}%")

    query += " ORDER BY r.request_id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = db.execute(query, params).fetchall()
    return {
        "count": len(rows),
        "requests": [request_row_to_dict(r, r["response_count"]) for r in rows],
    }


@app.get("/requests/{request_id}")
def request_detail(request_id: int, db: sqlite3.Connection = Depends(get_db)):
    req_row = db.execute(
        "SELECT * FROM community_request WHERE request_id = ?", (request_id,)
    ).fetchone()

    if req_row is None:
        raise HTTPException(status_code=404, detail="요청 글을 찾을 수 없습니다.")

    response_rows = db.execute(
        "SELECT * FROM community_response WHERE request_id = ? ORDER BY response_id ASC",
        (request_id,),
    ).fetchall()

    data = request_row_to_dict(req_row)
    data["responses"] = [response_row_to_dict(r) for r in response_rows]
    return data


@app.post("/requests", status_code=201)
def create_request(body: RequestCreate, db: sqlite3.Connection = Depends(get_db)):
    try:
        cursor = db.execute(
            """
            INSERT INTO community_request (
                question, requested_facility,
                latitude, longitude, address,
                location_input_type, password
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                body.question, body.requested_facility,
                body.latitude, body.longitude, body.address,
                body.location_input_type, body.password,
            ),
        )
        db.commit()
    except sqlite3.IntegrityError as error:
        raise HTTPException(status_code=422, detail=str(error))

    row = db.execute(
        "SELECT * FROM community_request WHERE request_id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return request_row_to_dict(row, response_count=0)


@app.delete("/requests/{request_id}", status_code=204)
def delete_request(
    request_id: int,
    body: PasswordCheck,
    db: sqlite3.Connection = Depends(get_db),
):
    row = db.execute(
        "SELECT password FROM community_request WHERE request_id = ?",
        (request_id,),
    ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="요청 글을 찾을 수 없습니다.")

    if row["password"] != body.password:
        raise HTTPException(status_code=403, detail="비밀번호가 일치하지 않습니다.")

    db.execute("DELETE FROM community_request WHERE request_id = ?", (request_id,))
    db.commit()
    return None


# =========================================================
# 4. 커뮤니티 응답 (community_response)
# =========================================================

class ResponseCreate(BaseModel):
    facility_name: str = Field(..., min_length=1, max_length=100)
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    address: Optional[str] = None
    description: Optional[str] = Field(None, max_length=500)
    place_contentid: Optional[str] = None
    password: str = Field(..., min_length=1, max_length=50)


@app.post("/requests/{request_id}/responses", status_code=201)
def create_response(
    request_id: int,
    body: ResponseCreate,
    db: sqlite3.Connection = Depends(get_db),
):
    exists = db.execute(
        "SELECT 1 FROM community_request WHERE request_id = ?", (request_id,)
    ).fetchone()
    if exists is None:
        raise HTTPException(status_code=404, detail="요청 글을 찾을 수 없습니다.")

    try:
        cursor = db.execute(
            """
            INSERT INTO community_response (
                request_id, facility_name,
                latitude, longitude, address,
                description, place_contentid, password
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id, body.facility_name,
                body.latitude, body.longitude, body.address,
                body.description, body.place_contentid, body.password,
            ),
        )
        db.commit()
    except sqlite3.IntegrityError as error:
        raise HTTPException(status_code=422, detail=str(error))

    row = db.execute(
        "SELECT * FROM community_response WHERE response_id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return response_row_to_dict(row)


@app.delete("/responses/{response_id}", status_code=204)
def delete_response(
    response_id: int,
    body: PasswordCheck,
    db: sqlite3.Connection = Depends(get_db),
):
    row = db.execute(
        "SELECT password FROM community_response WHERE response_id = ?",
        (response_id,),
    ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="응답을 찾을 수 없습니다.")

    if row["password"] != body.password:
        raise HTTPException(status_code=403, detail="비밀번호가 일치하지 않습니다.")

    db.execute("DELETE FROM community_response WHERE response_id = ?", (response_id,))
    db.commit()
    return None


# =========================================================
# 5. 지도용 — 좌표 있는 응답 전체 (커뮤니티 제보 핀)
# =========================================================

@app.get("/responses")
def list_responses_for_map(
    requested_facility: Optional[str] = None,
    db: sqlite3.Connection = Depends(get_db),
):
    query = """
        SELECT resp.*, r.requested_facility
        FROM community_response resp
        JOIN community_request r ON r.request_id = resp.request_id
        WHERE 1=1
    """
    params: list = []

    if requested_facility:
        query += " AND r.requested_facility = ?"
        params.append(requested_facility)

    rows = db.execute(query, params).fetchall()

    pins = []
    for row in rows:
        pin = response_row_to_dict(row)
        pin["requested_facility"] = row["requested_facility"]
        pins.append(pin)

    return {"count": len(pins), "responses": pins}


# =========================================================
# 헬스체크
# =========================================================

@app.get("/health")
def health():
    return {"status": "ok", "db_path": str(DB_PATH), "db_exists": DB_PATH.exists()}