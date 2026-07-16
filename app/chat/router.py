"""
챗봇 라우터 — 검색(RAG의 R) 결과를 컨텍스트로 LLM이 답변을 생성(G)한다.

동작 순서
  1. 짧은 인사      → 고정 인사말 (LLM 0콜, 토큰 0원)
  2. retrieve()     → 사전 기반 제보·장소 검색
  3. 사전 매칭 실패 → 일반 키워드 2차 검색 ("성심당 알려줘" 같은 고유명사 대비)
  4. 결과 있음      → LLM이 컨텍스트를 근거로 답변 생성
                      (OPENAI_API_KEY 없음 / 호출 실패 → 기존 템플릿 응답으로 폴백.
                       LLM이 죽어도 챗봇은 살아있어야 한다 — 배포 안전장치)
  5. 결과 0건       → EMPTY_CONTEXT_REPLY + suggest_report=True
                      ⚠️ 이 경우 LLM을 부르지 않는다. 근거 없이 장소를
                      지어내는 환각을 원천 차단하는 게 이 서비스의 신뢰 포인트.
  6. 검색 의도 없음 → 스몰토크. LLM이 자연스럽게 응대하고 장소 질문을 유도.
"""

import asyncio
import logging
import os
import re

from fastapi import APIRouter, Depends
from openai import AsyncOpenAI, OpenAIError
from sqlalchemy.orm import Session

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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["Chatbot"])

# ---------------------------------------------------------------
# OpenAI 클라이언트 — 모듈에서 1개만 만들어 재사용
# (요청마다 생성하면 커넥션 풀을 매번 새로 여닫아서 낭비)
# ---------------------------------------------------------------

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
LLM_TIMEOUT_SECONDS = 20

_openai_client: AsyncOpenAI | None = None


def _get_openai_client() -> AsyncOpenAI | None:
    """키가 있으면 클라이언트를 1회만 생성해서 재사용. 없으면 None → 템플릿 폴백."""
    global _openai_client
    if _openai_client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            return None
        _openai_client = AsyncOpenAI(api_key=api_key)
    return _openai_client


# ---------------------------------------------------------------
# 0. 삐빅이 시그니처 — 모든 답변은 "삐빅!"으로 시작한다
# ---------------------------------------------------------------

BOT_PREFIX = "삐빅! "


def _bibik(reply: str) -> str:
    """
    레이더 효과음 접두어. LLM 생성/템플릿/고정 문구 어디서 온 답변이든
    응답 직전에 일괄 적용한다 — 프롬프트로 시키면 LLM이 가끔 까먹는다.
    (LLM이 이미 붙여서 답한 경우 중복 방지)
    """
    if reply.lstrip().startswith("삐빅"):
        return reply
    return BOT_PREFIX + reply


# ---------------------------------------------------------------
# 1. 인사 판별 (로컬 규칙 — LLM 호출 없이 0원 처리)
# ---------------------------------------------------------------

GREETING_REPLY = (
    "안녕하세요! 동네레이더 챗봇입니다. "
    "궁금한 장소나 시설명을 말씀해 주시면, 이웃 제보와 공공 데이터를 기준으로 찾아볼게요."
)

_GREETING_PATTERN = re.compile(
    r"\b(안녕|안녕하세요|hi|hello|hey|반가워|좋은\s*하루|잘\s*지내|여보세요|ㅎㅇ|ㄴㅇ|ㅇㅇ)\b"
)


def _is_local_greeting(message: str) -> bool:
    """
    아주 짧은 메시지만 인사로 취급한다 (검색이 빈손일 때만 호출됨).
    "안녕 성심당 어디야" 같은 인사+질문은 폴백 검색으로 넘어가야 하므로
    길이 제한을 빡빡하게(7자) 둔다. 인사말 단독은 대부분 7자 이하다.
    """
    text = message.strip().lower()
    return len(text) <= 7 and bool(_GREETING_PATTERN.search(text))


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


def _extract_fallback_keywords(message: str, max_tokens: int = 3) -> list[str]:
    """공백 단위 토큰에서 불용어를 거르고, 조사 하나 뗀 변형도 함께 반환한다."""
    keywords: list[str] = []
    seen: set[str] = set()

    for raw in message.split():
        token = raw.strip("?!.,~ㅋㅎ ")
        if len(token) < 2:
            continue

        # 조사 하나 붙은 형태 대비: 원형이 불용어면 원 토큰도 버린다 ("위치가" 등)
        base = token[:-1] if len(token) >= 3 and token[-1] in _TRAILING_PARTICLES else token
        if token in _FALLBACK_STOPWORDS or base in _FALLBACK_STOPWORDS:
            continue

        for candidate in (token, base):
            if len(candidate) >= 2 and candidate not in seen:
                seen.add(candidate)
                keywords.append(candidate)

        if len(keywords) >= max_tokens * 2:
            break

    return keywords


def _has_search_terms(intent: QueryIntent, request: ChatRequest) -> bool:
    """retrieve()가 실제로 뭔가를 검색했는지 (=검색 의도가 있었는지) 판별."""
    return bool(
        intent.facility
        or intent.contenttypeid is not None
        or intent.keywords
        or (request.latitude is not None and request.longitude is not None)
    )


# ---------------------------------------------------------------
# 3. LLM 호출 (생성)
# ---------------------------------------------------------------

async def _call_llm(messages: list[dict[str, str]], max_tokens: int = 350) -> str | None:
    """
    실패하면 None을 돌려준다 — 예외를 위로 던지지 않는다.
    LLM은 부가 기능이고, 죽어도 템플릿 응답으로 서비스는 계속돼야 한다.
    """
    client = _get_openai_client()
    if client is None:
        return None

    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
                temperature=0.4,
                max_tokens=max_tokens,
            ),
            timeout=LLM_TIMEOUT_SECONDS,
        )
        reply = (response.choices[0].message.content or "").strip()
        return reply or None

    except (OpenAIError, asyncio.TimeoutError) as error:
        logger.warning("LLM 호출 실패 → 템플릿 폴백 사용: %s", error)
        return None
    except Exception:
        logger.exception("LLM 호출 중 예상 못 한 오류 → 템플릿 폴백 사용")
        return None


async def _generate_grounded_reply(
    request: ChatRequest,
    reports,
    places,
) -> str | None:
    """검색 결과를 컨텍스트로 넣어 답변을 생성한다. prompt.py의 부품을 그대로 조립."""
    context = format_context(reports, places, request.latitude, request.longitude)
    messages = build_messages(request.message, request.history, context, MAX_HISTORY_TURNS)
    return await _call_llm(messages)


# 검색 결과가 0건이거나 검색 의도가 없을 때 쓰는 프롬프트.
# 감정 표현("아 배고파"), 미등록 장소 질문("OO식당 어디야?"), 잡담을 한 번에 처리한다.
UNGROUNDED_SYSTEM_PROMPT = (
    "너는 대전 지역 시설·장소 안내 챗봇 '동네레이더'의 마스코트 '삐빅이'다. "
    "이름을 물으면 삐빅이라고 답해라. "
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
# 6. 엔드포인트
# ---------------------------------------------------------------

@router.post("/", response_model=ChatResponse)
async def chat_with_bot(
    request: ChatRequest,
    db: Session = Depends(get_db),
):
    message = request.message.strip()

    # 1) 사전 기반 검색 (기존 retrieve 그대로)
    #    인사보다 검색을 먼저 한다 — "안녕! 탄방동 맛집 알려줘"는 질문이다.
    reports, places, intent = retrieve(
        db, message, request.latitude, request.longitude
    )

    # 2) 사전 검색어(시설·카테고리·지명)가 있었는데 0건 → 제보 유도
    #    ⚠️ LLM을 부르지 않는다. 근거 없이 장소를 지어내는 환각을 차단하고,
    #    "레이더에 비는 곳 → 요청 글 올리기" UX로 연결한다.
    if not reports and not places and _has_search_terms(intent, request):
        return ChatResponse(
            reply=_bibik(EMPTY_CONTEXT_REPLY),
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
            return ChatResponse(reply=_bibik(GREETING_REPLY), sources=[], suggest_report=False)

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
            reply=_bibik(reply),
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
                reply=_bibik(EMPTY_CONTEXT_REPLY),
                sources=[],
                suggest_report=True,
                suggested_facility=suggested,
                suggestions=_suggestion_chips(intent.facility),
            )
        return ChatResponse(
            reply=_bibik(FALLBACK_GUIDE_REPLY), sources=[], suggest_report=False
        )

    return ChatResponse(
        reply=_bibik(reply),
        sources=[],
        suggest_report=searched_fallback,
        suggested_facility=suggested,
        suggestions=_suggestion_chips(suggested) if searched_fallback else [],
    )
