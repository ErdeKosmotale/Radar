"""
RAG의 R(Retrieval). 벡터DB 없이 SQLite 쿼리로 컨텍스트를 뽑는다.

왜 벡터DB를 안 쓰나:
  - place 1,365건 + 제보 수십 건 규모는 벡터 인덱스가 필요 없다
  - OpenAI 임베딩 호출 = 비용. RFP II-2 "정해진 예산을 초과할 수 없음"
  - Render 무료 티어 메모리 + 3일 일정 대비 리스크가 크다
여유가 생기면 SQLite FTS5(trigram 토크나이저)로 키워드 매칭만 교체하면 된다.
"""

import math
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import CommunityRequest, CommunityResponse, Place

# ---------------------------------------------------------------
# 1. 키워드 사전
# ---------------------------------------------------------------

# 강사님 피드백: "일단은 철봉, 화장실만" → 나머지는 확장 여지로 남겨둠
FACILITY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "철봉": ("철봉", "턱걸이", "풀업", "운동기구"),
    "화장실": ("화장실", "공중화장실", "개방화장실", "볼일"),
    "주차장": ("주차", "주차장"),
    "벤치": ("벤치", "쉼터", "그늘", "앉을"),
    "포토존": ("포토존", "사진", "인생샷"),
}

# TourAPI contenttypeid 매핑
CONTENT_TYPE_KEYWORDS: dict[int, tuple[str, ...]] = {
    12: ("관광지", "명소", "구경", "나들이", "놀러", "가볼"),
    14: ("문화시설", "박물관", "미술관", "전시", "도서관"),
    15: ("축제", "행사", "공연", "페스티벌"),
    25: ("여행코스", "코스"),
    28: ("레포츠", "운동", "체육", "액티비티", "등산"),
    32: ("숙박", "호텔", "숙소", "펜션", "모텔", "게스트하우스"),
    38: ("쇼핑", "시장", "백화점", "아울렛"),
    39: ("맛집", "음식점", "식당", "밥", "먹을", "카페", "커피"),
}

CONTENT_TYPE_LABELS: dict[int, str] = {
    12: "관광지",
    14: "문화시설",
    15: "축제·공연·행사",
    25: "여행코스",
    28: "레포츠",
    32: "숙박",
    38: "쇼핑",
    39: "음식점",
}

# 대전 대표 좌표 (시청)
DAEJEON_CENTER = (36.3504, 127.3845)


@dataclass
class QueryIntent:
    """질문에서 뽑아낸 검색 조건."""

    facility: str | None = None
    contenttypeid: int | None = None
    keywords: list[str] = None

    def __post_init__(self) -> None:
        if self.keywords is None:
            self.keywords = []


def parse_intent(message: str) -> QueryIntent:
    """
    질문 문자열에서 시설 종류 / 장소 유형 / 검색 키워드를 뽑는다.

    형태소 분석기(konlpy 등)를 쓰면 정확도가 오르지만, Render 무료 티어에
    JVM(Mecab/Okt)을 올리는 건 3일 일정에서 위험하다. 사전 매칭으로 간다.
    """
    lowered = message.lower()

    facility = None
    for name, words in FACILITY_KEYWORDS.items():
        if any(word in lowered for word in words):
            facility = name
            break

    contenttypeid = None
    for type_id, words in CONTENT_TYPE_KEYWORDS.items():
        if any(word in lowered for word in words):
            contenttypeid = type_id
            break

    # 검색 키워드: 시설명이 잡혔으면 그걸 우선, 아니면 지명 후보를 쓴다.
    keywords: list[str] = []
    if facility:
        keywords.append(facility)

    # "탄방동", "둔산동" 같은 행정동/역명 추출 — 접미사 기반의 거친 방법
    for token in message.replace("?", " ").replace(",", " ").split():
        if len(token) >= 2 and token.endswith(("동", "구", "역", "로", "길", "시", "군")):
            keywords.append(token)

    return QueryIntent(facility=facility, contenttypeid=contenttypeid, keywords=keywords)


# ---------------------------------------------------------------
# 2. 거리 계산
# ---------------------------------------------------------------


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """두 좌표 사이 실제 거리(미터)."""
    radius = 6_371_000.0

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)

    d_phi = phi2 - phi1
    d_lambda = math.radians(lng2 - lng1)

    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )

    return 2 * radius * math.asin(math.sqrt(a))


def walk_minutes(meters: float) -> int:
    """도보 4km/h 기준 소요 시간(분). 시안의 '도보 6분' 표기용."""
    return max(1, round(meters / 66.7))


def _lng_correction(latitude: float) -> float:
    """
    경도 보정 계수 = cos(위도)^2.

    ⚠️ 팀원 노트북의 find_nearest_place() 버그 수정 지점.
    위경도 도(degree)를 그냥 제곱합하면 안 된다. 대전(36.35°N)에서
    위도 1도 ≈ 111km 인데 경도 1도 ≈ 90km 라서, 보정 없이 정렬하면
    경도 차이를 약 1.24배 과대평가한다 → "가장 가까운 곳"이 안 가까움.
    """
    return math.cos(math.radians(latitude)) ** 2


# ---------------------------------------------------------------
# 3. 검색
# ---------------------------------------------------------------


def search_reports(
    db: Session,
    intent: QueryIntent,
    latitude: float | None = None,
    longitude: float | None = None,
    limit: int = 3,
) -> list[CommunityResponse]:
    """
    커뮤니티 제보 검색. RFP III-3-나 "커뮤니티 게시글 검색" 대응.

    이게 서비스의 차별화 지점이라 place보다 먼저, 우선적으로 인용한다.
    ("관광 데이터엔 맛집은 있어도 철봉·화장실은 없다")
    """
    stmt = select(CommunityResponse).join(
        CommunityRequest,
        CommunityResponse.request_id == CommunityRequest.request_id,
    )

    # 시설 종류는 AND 조건이다.
    # ⚠️ 여기를 다른 조건과 함께 or_() 로 묶으면, "화장실 어디?" 질문에
    #    지명만 겹치는 철봉 제보가 딸려나온다. 실제로 그 버그를 냈었다.
    if intent.facility:
        like = f"%{intent.facility}%"
        stmt = stmt.where(
            or_(
                CommunityRequest.requested_facility.like(like),
                CommunityResponse.facility_name.like(like),
                CommunityResponse.description.like(like),
            )
        )

    # 지명 키워드는 좌표가 없을 때만 쓴다.
    # 좌표가 있으면 거리순 정렬이 이미 '근처'를 담당하므로, 지명으로 또 좁히면
    # 주소 표기가 조금만 달라도(예: '탄방동' vs '서구 탄방1동') 0건이 된다.
    if latitude is None or longitude is None:
        location_keywords = [k for k in intent.keywords if k != intent.facility]
        if location_keywords:
            conditions = []
            for keyword in location_keywords:
                like = f"%{keyword}%"
                conditions.append(CommunityResponse.address.like(like))
                conditions.append(CommunityResponse.description.like(like))
                conditions.append(CommunityResponse.facility_name.like(like))
                conditions.append(CommunityRequest.question.like(like))
            stmt = stmt.where(or_(*conditions))

    if latitude is not None and longitude is not None:
        k = _lng_correction(latitude)
        distance = (
            (CommunityResponse.latitude - latitude)
            * (CommunityResponse.latitude - latitude)
            + (CommunityResponse.longitude - longitude)
            * (CommunityResponse.longitude - longitude)
            * k
        )
        stmt = stmt.order_by(distance.asc())
    else:
        stmt = stmt.order_by(CommunityResponse.created_at.desc())

    return list(db.execute(stmt.limit(limit)).scalars().all())


def search_places(
    db: Session,
    intent: QueryIntent,
    latitude: float | None = None,
    longitude: float | None = None,
    limit: int = 3,
) -> list[Place]:
    """공공데이터(TourAPI) 장소 검색."""
    has_coords = latitude is not None and longitude is not None

    stmt = select(Place)

    if intent.contenttypeid is not None:
        stmt = stmt.where(Place.contenttypeid == intent.contenttypeid)

    # 지명 키워드는 좌표가 없을 때만. (search_reports 와 동일한 이유)
    keyword_conditions = []
    if not has_coords:
        for keyword in intent.keywords:
            like = f"%{keyword}%"
            keyword_conditions.append(Place.title.like(like))
            keyword_conditions.append(Place.addr1.like(like))

        if keyword_conditions:
            stmt = stmt.where(or_(*keyword_conditions))

    # 유형·키워드·좌표가 전부 없으면 1,365건 중 아무거나 상위 N개가 나온다 → 무의미
    if intent.contenttypeid is None and not keyword_conditions and not has_coords:
        return []

    if latitude is not None and longitude is not None:
        k = _lng_correction(latitude)
        distance = (
            (Place.mapy - latitude) * (Place.mapy - latitude)
            + (Place.mapx - longitude) * (Place.mapx - longitude) * k
        )
        stmt = stmt.order_by(distance.asc())
    else:
        stmt = stmt.order_by(Place.title.asc())

    return list(db.execute(stmt.limit(limit)).scalars().all())


def retrieve(
    db: Session,
    message: str,
    latitude: float | None = None,
    longitude: float | None = None,
    report_limit: int = 3,
    place_limit: int = 3,
) -> tuple[list[CommunityResponse], list[Place], QueryIntent]:
    """
    질문 종류에 따라 검색 대상을 가른다.

      시설 질문 ("철봉 어디?")      → 제보만.
          TourAPI에 철봉·화장실은 없다. 이게 이 서비스의 존재 이유다.
          섞으면 "철봉 어디?"에 '탄방동칼국수'가 딸려나온다.

      카테고리 질문 ("둔산동 맛집") → 장소만.
          제보 테이블은 시설 위치용이라 맛집 질문과 무관하다.
          섞으면 "맛집 추천"에 철봉 제보가 딸려나온다.

      그 외 ("이 근처 뭐 있어?")    → 둘 다, 거리순.
    """
    intent = parse_intent(message)

    has_search_term = bool(
        intent.facility
        or intent.contenttypeid is not None
        or intent.keywords
        or (latitude is not None and longitude is not None)
    )

    if not has_search_term:
        return [], [], intent

    is_facility_query = intent.facility is not None
    is_category_query = intent.contenttypeid is not None and intent.facility is None

    reports = (
        []
        if is_category_query
        else search_reports(db, intent, latitude, longitude, report_limit)
    )

    places = (
        []
        if is_facility_query
        else search_places(db, intent, latitude, longitude, place_limit)
    )

    return reports, places, intent
