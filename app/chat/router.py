"""
동네레이더 챗봇 라우터 v2 — POST /chat/

똑똑해진 점
-----------
1. 인텐트 감지: 인사("안녕?"), 감사, 도움말을 알아듣고 상황에 맞게 답변
2. 카테고리 감지 3단계:
   a. 확장된 동의어 사전 (도서관←책/열람실, 병원←아파요/진료, 성심당 같은 고유명사 제외)
   b. DB에 실제 등록된 카테고리 동적 매칭 (공백 무시: "공공와이파이" = "공공 와이파이")
   c. 실패 시 문장에서 핵심 키워드 추출
3. 2중 데이터 소스:
   1순위 — community_response (이웃 제보, 생생한 후기)
   2순위 — place 테이블 (1,365건 공식 관광/시설 데이터) → "도서관 어디?"도 답 가능!
4. 구조화 응답: locations[] 와 suggestions[] 를 함께 반환
   → 프론트가 "지도에서 보기" 버튼과 추천 질문 칩을 렌더링

프론트 계약
------------
- 요청 : {"message": "도서관 어디에 있어?"}
- 응답 : {
    "reply": "...",
    "detected_facility": "도서관" | null,
    "match_count": 2,
    "locations": [{"name","latitude","longitude","address","description","source","response_id"}],
    "suggestions": ["철봉", "수유실", ...]
  }
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

<<<<<<< HEAD
load_dotenv()
=======
from app.chat.prompt import (
    EMPTY_CONTEXT_REPLY,
    build_messages,
    build_sources,
    format_context,
)
from app.chat.retrieval import (
    FACILITY_KEYWORDS,
    QueryIntent,
    haversine_m,
    retrieve,
    search_places,
    search_reports,
    walk_minutes,
)
from app.chat.schemas import (
    MAX_HISTORY_TURNS,
    ChatLocation,
    ChatRequest,
    ChatResponse,
)
from app.db import get_db
>>>>>>> dev

router = APIRouter(prefix="/chat", tags=["chat"])

DB_PATH = Path(os.getenv("DB_PATH", "local.db"))

# =========================================================
# 동의어 사전 — 키는 "표준 카테고리명"(가능하면 DB의 requested_facility와 일치)
# =========================================================
FACILITY_SYNONYMS: dict[str, list[str]] = {
    "철봉": ["철봉", "턱걸이", "풀업", "pullup", "pull-up"],
    "화장실": ["화장실", "변기", "용변", "화장싴", "toilet", "wc"],
    "주차장": ["주차장", "주차", "파킹", "parking"],
    "벤치": ["벤치", "앉을 곳", "앉을곳"],
    "포토존": ["포토존", "포토", "인생샷", "인스타 감성"],
    "수유실": ["수유실", "수유", "기저귀", "아기 케어", "젖병"],
    "도서관": ["도서관", "열람실", "책 읽을", "책읽을", "독서실"],
    "병원": ["병원", "진료", "응급실", "아파요", "아프면", "의원"],
    "약국": ["약국", "약 살", "약사", "상비약"],
    "카페": ["카페", "커피", "라떼", "아메리카노"],
    "편의점": ["편의점", "24시", "cu", "gs25", "세븐일레븐", "이마트24"],
    "공원": ["공원", "산책할", "산책로", "잔디밭"],
    "ATM": ["atm", "현금인출", "출금", "현금 뽑"],
    "공공 와이파이": ["와이파이", "wifi", "무료 인터넷", "핫스팟"],
    "놀이터": ["놀이터", "미끄럼틀", "그네", "시소"],
    "버스정류장": ["버스정류장", "정류장", "버스 타"],
    "쉼터": ["쉼터", "그늘", "쉴 곳", "쉴곳", "쉴만한", "정자"],
    "식수대": ["식수대", "식수", "물 마실", "정수기", "급수대", "음수대"],
    "운동시설": ["운동시설", "운동기구", "헬스", "체육시설", "운동할"],
    "자전거 거치대": ["자전거 거치대", "자전거", "따릉이", "타슈"],
    "전기차 충전소": ["전기차 충전", "충전소", "전기차", "ev충전"],
    "무인민원발급기": ["무인민원발급기", "민원발급", "등본", "발급기", "주민등록"],
    "흡연구역": ["흡연구역", "흡연", "담배 피", "담배피"],
}

# 인텐트 패턴
GREETING_RE = re.compile(r"안녕|하이|헬로|반가워|반갑|hello|\bhi\b|ㅎㅇ|하잉", re.IGNORECASE)
THANKS_RE = re.compile(r"고마워|고맙|감사|땡큐|thank", re.IGNORECASE)
HELP_RE = re.compile(r"도움말|사용법|어떻게 (써|사용)|뭘? ?할 ?수|무엇을 할|뭐 ?해줄|뭐 ?할 ?줄|기능|help", re.IGNORECASE)

# 키워드 추출 시 지워버릴 표현들 (조사·의문사·잡담)
STOPWORDS_RE = re.compile(
    r"어디|어딨|있어|있나|있니|있을까|있는지|알려|알려줘|알려주세요|찾아|찾고|근처|주변|"
    r"제일|가장|좀|요\b|나요|가요|해줘|주세요|합니다|입니다|에서|으로|이나|한테|에게|"
    r"[?!.,~ㅋㅎㅠㅜ]+"
)


# =========================================================
# DB 헬퍼
# =========================================================

def _connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise HTTPException(status_code=500, detail=f"DB 파일을 찾을 수 없습니다: {DB_PATH.resolve()}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def list_db_facilities() -> list[str]:
    """DB(community_request)에 등록된 서로 다른 카테고리 이름 전부."""
    if not DB_PATH.exists():
        return []
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT requested_facility, COUNT(*) AS c FROM community_request "
            "WHERE requested_facility IS NOT NULL AND requested_facility != '' "
            "GROUP BY requested_facility ORDER BY c DESC"
        ).fetchall()
        return [r["requested_facility"] for r in rows]
    finally:
        conn.close()


def search_community(facility: str, limit: int = 4) -> List[dict]:
    """이웃 제보(community_response) 검색 — 카테고리 정확 매칭 + 시설명/설명 부분 매칭."""
    conn = _connect()
    try:
        like = f"%{facility}%"
        rows = conn.execute(
            """
            SELECT resp.response_id, resp.request_id,
                   resp.facility_name, resp.latitude, resp.longitude,
                   resp.address, resp.description, resp.created_at,
                   r.requested_facility
            FROM community_response AS resp
            JOIN community_request  AS r ON r.request_id = resp.request_id
            WHERE r.requested_facility = ?
               OR resp.facility_name LIKE ?
               OR resp.description  LIKE ?
            ORDER BY resp.created_at DESC
            LIMIT ?
            """,
            (facility, like, like, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def search_community_keyword(keyword: str, limit: int = 4) -> List[dict]:
    """
    자유 키워드로 이웃 답변 전문 검색.
    시설명 · 설명(후기) · 원문 질문까지 모두 뒤져서
    '전자레인지 있는 곳', '타임월드' 같은 질문도 답변 내용에서 찾아낸다.
    """
    if not keyword or len(keyword) < 2:
        return []
    conn = _connect()
    try:
        like = f"%{keyword}%"
        rows = conn.execute(
            """
            SELECT resp.response_id, resp.request_id,
                   resp.facility_name, resp.latitude, resp.longitude,
                   resp.address, resp.description, resp.created_at,
                   r.requested_facility
            FROM community_response AS resp
            JOIN community_request  AS r ON r.request_id = resp.request_id
            WHERE resp.facility_name LIKE ?
               OR resp.description  LIKE ?
               OR r.question        LIKE ?
            ORDER BY resp.created_at DESC
            LIMIT ?
            """,
            (like, like, like, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


<<<<<<< HEAD
def count_open_requests(term: str) -> int:
    """
    답변을 기다리는(응답 0건) 관련 요청 글 수.
    '아직 정보 없음' 답변에 '게시판에 관련 요청 N건이 기다리고 있어요'로 활용.
    """
    if not term:
        return 0
    conn = _connect()
    try:
        like = f"%{term}%"
        row = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM community_request r
            WHERE (r.requested_facility = ? OR r.question LIKE ?)
              AND NOT EXISTS (
                    SELECT 1 FROM community_response resp
                    WHERE resp.request_id = r.request_id)
            """,
            (term, like),
        ).fetchone()
        return int(row["c"])
    finally:
        conn.close()
=======
# ---------------------------------------------------------------
# 2. 폴백 키워드 추출
#    parse_intent()의 사전(철봉/화장실/맛집…)에 없는 고유명사 대비.
#    예) "성심당 알려줘" → 사전 매칭은 빈손이지만 여기서 '성심당'을 건진다.
# ---------------------------------------------------------------

# 검색어로서 의미가 없는 흔한 표현들
_FALLBACK_STOPWORDS = frozenset({
    "어디", "어디야", "어디에", "어딨어", "어디있어", "어딨나요",
    "있어", "있나", "있냐", "있나요", "있어요", "있을까", "있을까요",
    "알려줘", "알려줘요", "알려주세요", "찾아줘", "찾아주세요", "가르쳐줘",
    "추천", "추천해줘", "추천해주세요", "소개해줘", "소개해주세요",
    "근처", "주변", "가까운", "제일", "가장",
    "지금", "혹시", "그냥", "뭔가", "뭐가", "뭐야", "뭐지", "어때", "어때요",
    "위치", "장소", "해줘", "주세요", "부탁해",
    # 인사·감사 표현 — 장소 검색어가 아니다
    "안녕", "안녕하세요", "하이", "헬로", "반가워", "반가워요", "반갑습니다",
    "고마워", "고마워요", "고맙습니다", "감사", "감사해요", "감사합니다", "땡큐",
    "미안", "미안해", "죄송", "죄송해요", "잘가", "잘있어", "바이",
    # 감정·상태 표현 — 검색이 아니라 공감 대상 ("아 배고파" → 맛집 유도)
    "배고파", "배고파요", "배고프다", "배고프네", "배고픈데", "배고픔",
    "목말라", "목마르다", "목마른데",
    "심심해", "심심해요", "심심하다", "심심한데", "지루해", "지루하다",
    "피곤해", "피곤해요", "피곤하다", "피곤한데", "졸려", "졸려요", "졸리다",
    "힘들어", "힘들어요", "힘들다", "힘든데",
    "더워", "더워요", "덥다", "추워", "추워요", "춥다",
})

# "성심당은", "성심당이" → "성심당" 처럼 붙기 쉬운 한 글자 조사
_TRAILING_PARTICLES = "이가은는을를도의에만"
>>>>>>> dev


def search_places(keyword: str, limit: int = 3) -> List[dict]:
    """공식 place 테이블(1,365건)에서 이름/주소로 검색."""
    if not keyword or len(keyword) < 2:
        return []
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT contentid, title, addr1, mapy AS latitude, mapx AS longitude
            FROM place
            WHERE title LIKE ? OR addr1 LIKE ?
            LIMIT ?
            """,
            (f"%{keyword}%", f"%{keyword}%", limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# =========================================================
# 카테고리 / 키워드 감지
# =========================================================

def _norm(s: str) -> str:
    """소문자화 + 공백 제거 (공백 차이 무시 매칭용)."""
    return re.sub(r"\s+", "", (s or "").lower())


def detect_facility(message: str) -> Optional[str]:
    """
    1) 동의어 사전 매칭 (표준 카테고리명 반환)
    2) DB에 실제 존재하는 카테고리 매칭 (공백 무시, 긴 이름 우선)
    """
    msg_norm = _norm(message)
    if not msg_norm:
        return None

    # 1) 동의어 사전 — 긴 동의어 먼저 (예: "자전거 거치대" > "자전거")
    candidates: list[tuple[str, str]] = []  # (동의어, 표준명)
    for canon, syns in FACILITY_SYNONYMS.items():
        for syn in syns:
            candidates.append((syn, canon))
    candidates.sort(key=lambda x: len(_norm(x[0])), reverse=True)
    for syn, canon in candidates:
        if _norm(syn) in msg_norm:
            return canon

    # 2) DB 카테고리 동적 매칭
    for fac in sorted(list_db_facilities(), key=lambda f: len(_norm(f)), reverse=True):
        if _norm(fac) and _norm(fac) in msg_norm:
            return fac

    return None


JOSA_RE = re.compile(r"(에서|으로|이랑|한테|에게|은|는|이|가|을|를|에|로|의|도|만|랑)$")


def extract_keyword(message: str) -> Optional[str]:
    """
    카테고리 감지에 실패했을 때, 검색용 핵심 키워드 추출.
    예: '성심당 어디야?' → '성심당', '타임월드에 뭐 있어?' → '타임월드'
    """
    cleaned = STOPWORDS_RE.sub(" ", message or "")
    tokens = []
    for t in cleaned.split():
        # 끝에 붙은 조사 제거 ('타임월드에' → '타임월드'), 단 2글자 이상 남을 때만
        stripped = JOSA_RE.sub("", t)
        if len(stripped) >= 2:
            t = stripped
        if len(t) >= 2:
            tokens.append(t)
    if not tokens:
        return None
    # 가장 긴 토큰을 핵심 키워드로 (고유명사일 확률이 높음)
    return max(tokens, key=len)


# =========================================================
# 응답 모델
# =========================================================

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=500)


class ChatLocation(BaseModel):
    name: str
    latitude: float
    longitude: float
    address: Optional[str] = None
    description: Optional[str] = None
    source: str = "community"          # "community" | "official"
    response_id: Optional[int] = None  # community 제보일 때만


class ChatResponse(BaseModel):
    reply: str
    detected_facility: Optional[str] = None
    match_count: int = 0
    locations: List[ChatLocation] = []
    suggestions: List[str] = []


# =========================================================
# 답변 빌더
# =========================================================

def _greeting_reply() -> ChatResponse:
    facs = list_db_facilities()[:8]
    fac_line = " · ".join(facs) if facs else "철봉 · 화장실 · 주차장"
    return ChatResponse(
        reply=(
            "안녕하세요! 저는 대전 동네 정보를 찾아주는 레이더 챗봇이에요 📡\n\n"
            "이웃들의 생생한 제보와 공식 장소 데이터를 함께 검색해드려요.\n"
            f"지금 이런 걸 물어보실 수 있어요: {fac_line}\n\n"
            "아래 추천 질문을 눌러보거나 편하게 물어보세요!"
        ),
        suggestions=facs[:6],
    )


def _thanks_reply() -> ChatResponse:
    return ChatResponse(
        reply="도움이 됐다니 다행이에요! 😊 또 필요한 장소가 있으면 언제든 물어보세요.",
        suggestions=list_db_facilities()[:4],
    )


def _help_reply() -> ChatResponse:
    facs = list_db_facilities()
    return ChatResponse(
        reply=(
            "제가 할 수 있는 일이에요 💡\n\n"
            "1️⃣ 이웃 제보 검색 — 게시판에 달린 답변 속 위치를 찾아드려요\n"
            "2️⃣ 공식 장소 검색 — 대전 관광·문화시설 데이터에서도 찾아요 (예: 도서관, 성심당)\n"
            "3️⃣ 지도 연동 — 답변 속 '지도에서 보기'를 누르면 바로 위치 확인!\n\n"
            f"현재 이웃들이 활발히 찾는 카테고리는 {len(facs)}개예요."
        ),
        suggestions=facs[:6],
    )


def _to_locations(community: List[dict], official: List[dict]) -> List[ChatLocation]:
    locs: List[ChatLocation] = []
    for r in community:
        locs.append(ChatLocation(
            name=r["facility_name"],
            latitude=float(r["latitude"]),
            longitude=float(r["longitude"]),
            address=r.get("address"),
            description=r.get("description"),
            source="community",
            response_id=r["response_id"],
        ))
    for p in official:
        locs.append(ChatLocation(
            name=p["title"],
            latitude=float(p["latitude"]),
            longitude=float(p["longitude"]),
            address=p.get("addr1"),
            source="official",
        ))
    return locs


def _suggest_similar(exclude: Optional[str], n: int = 4) -> list[str]:
    return [f for f in list_db_facilities() if f != exclude][:n]


def build_reply(message: str) -> ChatResponse:
    # ── 0. 카테고리부터 감지 ("안녕! 철봉 어디?"는 인사가 아니라 질문) ──
    facility = detect_facility(message)

    # ── 1. 카테고리 없으면 인텐트 체크 ──
    if facility is None:
        if GREETING_RE.search(message):
            return _greeting_reply()
        if THANKS_RE.search(message):
            return _thanks_reply()
        if HELP_RE.search(message):
            return _help_reply()

    # ── 2. 데이터 검색 ──
    community: List[dict] = []
    official: List[dict] = []
    keyword: Optional[str] = None

    if facility:
        community = search_community(facility, limit=4)
        if not community:
            # 동의어가 표준 카테고리로 점프하며 원문 단어를 잃었을 수 있음
            # (예: '정수기' → '식수대') → 원문 키워드로 이웃 답변 한 번 더 검색
            kw2 = extract_keyword(message)
            if kw2 and _norm(kw2) != _norm(facility):
                community = search_community_keyword(kw2, limit=4)
                if community:
                    keyword = kw2
        if not community:
            official = search_places(facility, limit=3)
    else:
        # 카테고리도 인텐트도 아님 → 핵심 키워드로 검색
        keyword = extract_keyword(message)
        if keyword:
            # ⭐ 이웃 답변의 시설명·설명·원문 질문부터 먼저 검색
            community = search_community_keyword(keyword, limit=4)
            if not community:
                official = search_places(keyword, limit=3)
            if community or official:
                facility = keyword  # 감지된 것으로 취급 (표시용)

    locations = _to_locations(community, official)

    # ── 3. 답변 텍스트 구성 ──
    if community:
        if keyword:
            header = f"이웃 답변 속에서 '{keyword}' 관련 정보를 찾았어요 🔎"
        else:
            header = f"이웃들이 알려준 '{facility}' 정보예요 📍"
        lines = [header, ""]
        for i, r in enumerate(community, 1):
            lines.append(f"{i}. {r['facility_name']}" + (f" — {r['address']}" if r.get("address") else ""))
            if r.get("description"):
                lines.append(f"   💬 \"{r['description']}\"")
        lines += ["", "아래 버튼으로 지도에서 바로 확인해보세요!"]
        return ChatResponse(
            reply="\n".join(lines),
            detected_facility=facility,
            match_count=len(community),
            locations=locations,
        )

    if official:
        lines = [f"이웃 제보는 아직 없지만, 공식 장소 데이터에서 '{facility}'을(를) 찾았어요 🗂️", ""]
        for i, p in enumerate(official, 1):
            lines.append(f"{i}. {p['title']}" + (f" — {p['addr1']}" if p.get("addr1") else ""))
        lines += ["", "직접 가보셨다면 게시판에 후기를 남겨주세요. 다음 이웃에게 큰 도움이 돼요 🙌"]
        return ChatResponse(
            reply="\n".join(lines),
            detected_facility=facility,
            match_count=len(official),
            locations=locations,
        )

    if facility:
        # 카테고리는 알아들었는데 데이터가 아무것도 없음
        waiting = count_open_requests(facility)
        wait_line = (
            f"지금 게시판에 '{facility}' 관련 요청 {waiting}건이 답변을 기다리고 있어요. "
            "혹시 아신다면 이웃에게 답을 남겨주세요 🙌\n"
            if waiting > 0 else
            f"게시판에 '{facility}' 요청 글을 올려두시면 현지인이 위치로 답해줄 거예요!\n"
        )
        return ChatResponse(
            reply=(
                f"'{facility}'... 아직 이웃 제보도, 공식 데이터도 없네요 😢\n\n"
                + wait_line +
                "대신 이런 건 어떠세요?"
            ),
            detected_facility=facility,
            suggestions=_suggest_similar(facility),
        )

    # 완전히 못 알아들음
    return ChatResponse(
        reply=(
            "음, 어떤 장소를 찾으시는지 잘 모르겠어요 🤔\n"
            "'철봉 어디 있어?', '도서관 알려줘' 처럼 물어봐 주시면 찾아드릴게요.\n\n"
            "지금 이웃들이 자주 찾는 곳들이에요:"
        ),
        suggestions=list_db_facilities()[:6],
    )


# =========================================================
# (선택) OpenAI로 답변 문장만 다듬기 — 구조 데이터는 그대로 유지
# =========================================================

def polish_with_llm(result: ChatResponse, user_message: str) -> ChatResponse:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or not result.locations:
        return result
    try:
        from openai import OpenAI
    except ImportError:
        return result

    loc_lines = "\n".join(
        f"- {l.name}" + (f" ({l.address})" if l.address else "") +
        (f" / 후기: {l.description}" if l.description else "") +
        f" / 출처: {'이웃 제보' if l.source == 'community' else '공식 데이터'}"
        for l in result.locations
    )
    try:
        client = OpenAI(api_key=api_key)
        completion = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[
                {"role": "system", "content": (
                    "당신은 대전 동네 장소를 안내하는 '동네레이더' 챗봇입니다. "
                    "아래 장소 목록만 근거로, 3~6줄로 친근하게 안내하세요. "
                    "없는 정보를 지어내지 말고, 좌표 숫자는 언급하지 마세요. "
                    "이모지를 적당히 사용하세요."
                )},
                {"role": "user", "content": f"[질문]\n{user_message}\n\n[장소 목록]\n{loc_lines}"},
            ],
            max_tokens=400,
            temperature=0.6,
        )
        text = (completion.choices[0].message.content or "").strip()
        if text:
            result.reply = text
    except Exception as exc:  # noqa: BLE001
        print(f"[chat] OpenAI 다듬기 실패, 템플릿 유지: {exc}")
    return result


<<<<<<< HEAD
# =========================================================
# 라우트
# =========================================================
=======
# 검색 결과가 0건이거나 검색 의도가 없을 때 쓰는 프롬프트.
# 감정 표현("아 배고파"), 미등록 장소 질문("OO식당 어디야?"), 잡담을 한 번에 처리한다.
UNGROUNDED_SYSTEM_PROMPT = (
    "너는 대전 지역 시설·장소 안내 챗봇 '동네레이더'다. "
    "사용자의 메시지에 대해 답변에 쓸 장소 데이터가 없는 상황이다. 상황에 맞게 답해라:\n"
    "- 감정·상태 표현(배고파, 심심해, 피곤해, 더워 등)이면: 한 문장으로 가볍게 공감해 주고, "
    "그 상태와 어울리는 장소(배고프면 맛집·식당, 심심하면 관광지·문화시설, 더우면 카페 등)를 "
    "찾아줄 수 있다고 이어라. 동네 이름을 알려주면 검색해 보겠다고 해라.\n"
    "- 특정 장소나 시설을 찾는 질문이면: 아직 등록된 정보가 없다고 솔직히 말하고, "
    "커뮤니티에 요청 글을 올려 이웃 제보를 받아보라고 권해라.\n"
    "- 그 외 잡담·감사 인사면: 자연스럽게 응대하고, 궁금한 장소나 시설을 물어보도록 부드럽게 유도해라.\n"
    "장소명·주소·거리를 절대 지어내지 마라. 2~3문장 이내, 친근한 존댓말."
)

# LLM까지 못 쓸 때(키 없음 등) 쓰는 고정 안내
FALLBACK_GUIDE_REPLY = "장소나 시설명을 알려주시면 대전 이웃 제보와 공공 데이터를 기준으로 찾아볼게요."


async def _generate_ungrounded_reply(
    request: ChatRequest,
    searched: bool,
) -> str | None:
    # LLM이 상황을 정확히 가르도록 힌트를 준다:
    # 검색을 했는데 0건인지(→ 제보 권유가 적절), 검색어 자체가 없었는지(→ 공감/잡담)
    hint = (
        "(참고: 이 메시지의 키워드로 DB를 검색했지만 결과가 0건이었다)"
        if searched
        else "(참고: 이 메시지에는 검색할 장소 키워드가 없다 — 일반 대화나 감정 표현이다)"
    )
    messages: list[dict[str, str]] = [
        {"role": "system", "content": f"{UNGROUNDED_SYSTEM_PROMPT}\n{hint}"}
    ]
    for turn in request.history[-4:]:
        messages.append({"role": turn.role, "content": turn.content})
    messages.append({"role": "user", "content": request.message})
    return await _call_llm(messages, max_tokens=150)


# ---------------------------------------------------------------
# 4. FE 연동 데이터 (위치 카드 / 추천 칩)
# ---------------------------------------------------------------

# 0건일 때 대신 탐색해 볼 시설 칩 — FE에서 클릭하면 "{이름} 어디 있어?"로 질문된다
SUGGESTION_CHIPS: list[str] = list(FACILITY_KEYWORDS)[:4]  # 철봉, 화장실, 주차장, 벤치


def _suggestion_chips(exclude: str | None = None) -> list[str]:
    """방금 검색에 실패한 시설은 칩에서 뺀다 — 눌러봤자 또 0건이다."""
    return [chip for chip in SUGGESTION_CHIPS if chip != exclude]


def _build_locations(
    reports,
    places,
    latitude: float | None,
    longitude: float | None,
) -> list[ChatLocation]:
    """
    FE 위치 카드용 데이터. renderBotResponse()가 카드를 그리고,
    '지도에서 보기' 클릭 시 focusOnLocation()이
      - community: response_id 로 지도의 기존 제보 핀을 선택
      - official : 좌표로 임시 마커를 찍음
    build_sources()와 마찬가지로 LLM이 아닌 검색 결과에서 결정론적으로 만든다.
    """
    locations: list[ChatLocation] = []

    for report in reports:
        distance_m = None
        minutes = None
        if latitude is not None and longitude is not None:
            meters = haversine_m(latitude, longitude, report.latitude, report.longitude)
            distance_m = round(meters)
            minutes = walk_minutes(meters)

        locations.append(
            ChatLocation(
                name=report.facility_name or "이름 미상",
                source="community",
                latitude=report.latitude,
                longitude=report.longitude,
                address=report.address,
                description=report.description,
                distance_m=distance_m,
                walk_minutes=minutes,
                response_id=report.response_id,
            )
        )

    for place in places:
        distance_m = None
        minutes = None
        if latitude is not None and longitude is not None:
            meters = haversine_m(latitude, longitude, place.mapy, place.mapx)
            distance_m = round(meters)
            minutes = walk_minutes(meters)

        locations.append(
            ChatLocation(
                name=place.title,
                source="official",
                latitude=place.mapy,
                longitude=place.mapx,
                address=place.addr1,
                distance_m=distance_m,
                walk_minutes=minutes,
                place_contentid=place.contentid,
            )
        )

    return locations


# ---------------------------------------------------------------
# 5. 템플릿 응답 (LLM 폴백용 — 기존 동작 유지)
# ---------------------------------------------------------------

def _build_report_reply(reports, facility_label: str) -> str:
    # ⚠️ 예전 버전은 "철봉"이 하드코딩돼 있어서 화장실을 물어봐도
    #    "철봉 정보가 있어요"라고 답했다. 라벨을 intent에서 받도록 수정.
    lines = [f"대전 지역 이웃 제보로는 다음과 같은 {facility_label} 정보가 있어요:"]
    for index, report in enumerate(reports[:3], start=1):
        title = report.facility_name or facility_label
        parts = [f"{index}. {title}"]
        if report.address:
            parts.append(f"주소: {report.address}")
        elif report.latitude is not None and report.longitude is not None:
            parts.append(f"위치: {report.latitude:.4f}, {report.longitude:.4f}")
        parts.append(f"설명: {report.description or '제보가 등록되어 있습니다.'}")
        lines.append(" / ".join(parts))
    return "\n".join(lines)


def _build_place_reply(places) -> str:
    lines = ["공공데이터 장소로는 다음과 같은 장소가 있습니다:"]
    for index, place in enumerate(places[:3], start=1):
        parts = [f"{index}. {place.title}"]
        if place.addr1:
            parts.append(f"주소: {place.addr1}")
        elif place.mapy is not None and place.mapx is not None:
            parts.append(f"위치: {place.mapy:.4f}, {place.mapx:.4f}")
        lines.append(" / ".join(parts))
    return "\n".join(lines)


def _build_template_reply(reports, places, intent: QueryIntent) -> str:
    parts = []
    if reports:
        parts.append(_build_report_reply(reports, intent.facility or "시설"))
    if places:
        parts.append(_build_place_reply(places))
    return "\n\n".join(parts) or EMPTY_CONTEXT_REPLY


# ---------------------------------------------------------------
# 5. 엔드포인트
# ---------------------------------------------------------------
>>>>>>> dev

@router.post("/", response_model=ChatResponse)
def chat(body: ChatRequest) -> ChatResponse:
    result = build_reply(body.message)
    return polish_with_llm(result, body.message)


<<<<<<< HEAD
@router.get("/health")
def chat_health() -> dict:
    return {
        "status": "ok",
        "db_exists": DB_PATH.exists(),
        "known_facilities": len(list_db_facilities()),
        "openai_configured": bool(os.getenv("OPENAI_API_KEY")),
    }
=======
    # 2) 사전 검색어(시설·카테고리·지명)가 있었는데 0건 → 제보 유도
    #    ⚠️ LLM을 부르지 않는다. 근거 없이 장소를 지어내는 환각을 차단하고,
    #    "레이더에 비는 곳 → 요청 글 올리기" UX로 연결한다.
    if not reports and not places and _has_search_terms(intent, request):
        return ChatResponse(
            reply=EMPTY_CONTEXT_REPLY,
            sources=[],
            suggest_report=True,
            suggested_facility=intent.facility,
            suggestions=_suggestion_chips(intent.facility),
        )

    # 3) 사전 매칭이 아예 빈손일 때
    searched_fallback = False
    if not reports and not places:
        # 3-a) 짧은 인사 → 고정 응답 (LLM 0콜)
        if _is_local_greeting(message):
            return ChatResponse(reply=GREETING_REPLY, sources=[], suggest_report=False)

        # 3-b) 일반 키워드로 2차 검색 ("성심당 알려줘" 같은 고유명사 대비)
        fallback_keywords = _extract_fallback_keywords(message)
        if fallback_keywords:
            searched_fallback = True
            fallback_intent = QueryIntent(keywords=fallback_keywords)
            reports = search_reports(
                db, fallback_intent, request.latitude, request.longitude
            )
            places = search_places(
                db, fallback_intent, request.latitude, request.longitude
            )

    # 4) 결과 있음 → LLM이 컨텍스트 기반으로 생성, 실패하면 템플릿
    if reports or places:
        reply = await _generate_grounded_reply(request, reports, places)
        if reply is None:
            reply = _build_template_reply(reports, places, intent)
        return ChatResponse(
            reply=reply,
            sources=build_sources(
                reports, places, request.latitude, request.longitude
            ),
            locations=_build_locations(
                reports, places, request.latitude, request.longitude
            ),
            suggest_report=False,
        )

    # 5) 여기까지 왔으면 결과 0건 — 스몰토크이거나 미등록 장소 질문.
    #    LLM이 상황을 보고 자연스럽게 응대한다 (컨텍스트 없음을 명시했으므로
    #    지어내지 않고 제보를 권하거나 잡담에 답한다).
    #    폴백 검색까지 했으면 = 특정 장소를 찾다 실패한 것 → 제보 유도 + 프리필 제공
    suggested = fallback_keywords[0] if searched_fallback and fallback_keywords else None

    reply = await _generate_ungrounded_reply(request, searched_fallback)
    if reply is None:
        # 키 없음/호출 실패: 2차 검색까지 했으면 제보 유도, 아니면 사용 안내
        if searched_fallback:
            return ChatResponse(
                reply=EMPTY_CONTEXT_REPLY,
                sources=[],
                suggest_report=True,
                suggested_facility=suggested,
                suggestions=_suggestion_chips(intent.facility),
            )
        return ChatResponse(
            reply=FALLBACK_GUIDE_REPLY, sources=[], suggest_report=False
        )

    return ChatResponse(
        reply=reply,
        sources=[],
        suggest_report=searched_fallback,
        suggested_facility=suggested,
        suggestions=_suggestion_chips(suggested) if searched_fallback else [],
    )

    #한줄 추가
>>>>>>> dev
