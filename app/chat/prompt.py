"""프롬프트 조립 + sources 생성."""

from app.chat.retrieval import (
    CONTENT_TYPE_LABELS,
    haversine_m,
    walk_minutes,
)
from app.chat.schemas import ChatMessage, Source
from app.models import CommunityResponse, Place

SYSTEM_PROMPT = """너는 대전·충청 지역 정보 안내 챗봇 '동네레이더'다.

이 서비스의 정체성: 관광 데이터에는 맛집은 있어도 철봉·화장실 같은 작은 시설은 없다.
그 빈틈을 이웃들의 제보로 메운다.

규칙:
- 아래 [이웃 제보]와 [공공데이터 장소]에 있는 내용만 근거로 답한다.
  컨텍스트에 없는 장소명·주소·거리·시간을 절대 지어내지 마라.
- [이웃 제보]가 있으면 그것을 우선해서 인용한다. 제보 내용을 그대로 따옴표로 인용해도 좋다.
- 거리 정보가 주어졌으면 가까운 순으로 최대 3곳까지 말한다.
- 컨텍스트가 비어 있으면 솔직히 모른다고 말하고, 요청 글을 올려보라고 권한다.
  없는 정보를 추측해서 채우지 마라.
- 3~4문장 이내로 짧게. 친근한 존댓말.
- 장소를 나열할 때 번호를 붙인다."""

# 컨텍스트가 0건일 때 쓰는 고정 응답.
# LLM을 호출하지 않으므로 토큰 0원 + 환각 0%.
# 시안 ③번의 "📡 레이더에 비는 곳이에요 → 요청 글로 올리기" 대응.
EMPTY_CONTEXT_REPLY = (
    "아쉽게도 그 주변에는 아직 이웃들의 제보가 없어요. 📡\n"
    "혹시 알고 계신 곳이 있다면 첫 제보로 다음 이웃을 도와주시겠어요?\n"
    "요청 글로 올려두면 다른 현지인의 답변도 모을 수 있어요."
)


def format_context(
    reports: list[CommunityResponse],
    places: list[Place],
    latitude: float | None = None,
    longitude: float | None = None,
) -> str:
    """
    검색 결과를 프롬프트에 넣을 텍스트로 만든다.

    토큰 절약: 이미지 URL은 넣지 않는다. LLM이 쓸 일이 없고 길기만 하다.
    (이미지는 sources로만 내보내서 FE가 카드에 그린다)
    """
    blocks: list[str] = []

    if reports:
        lines = ["[이웃 제보]"]
        for index, report in enumerate(reports, start=1):
            parts = [f"{index}. {report.facility_name or '이름 미상'}"]

            if report.address:
                parts.append(f"주소: {report.address}")

            if report.description:
                parts.append(f'제보 내용: "{report.description}"')

            if latitude is not None and longitude is not None:
                meters = haversine_m(latitude, longitude, report.latitude, report.longitude)
                parts.append(f"거리: 도보 약 {walk_minutes(meters)}분 ({round(meters)}m)")

            lines.append(" / ".join(parts))
        blocks.append("\n".join(lines))

    if places:
        lines = ["[공공데이터 장소]"]
        for index, place in enumerate(places, start=1):
            label = CONTENT_TYPE_LABELS.get(place.contenttypeid, "장소")
            parts = [f"{index}. {place.title} ({label})"]

            if place.addr1:
                parts.append(f"주소: {place.addr1}")

            if latitude is not None and longitude is not None:
                meters = haversine_m(latitude, longitude, place.mapy, place.mapx)
                parts.append(f"거리: 도보 약 {walk_minutes(meters)}분 ({round(meters)}m)")

            lines.append(" / ".join(parts))
        blocks.append("\n".join(lines))

    if not blocks:
        return "(검색 결과 없음)"

    return "\n\n".join(blocks)


def build_messages(
    message: str,
    history: list[ChatMessage],
    context: str,
    max_turns: int,
) -> list[dict[str, str]]:
    """OpenAI에 보낼 messages 배열을 만든다."""
    messages: list[dict[str, str]] = [
        {"role": "system", "content": f"{SYSTEM_PROMPT}\n\n---\n{context}"}
    ]

    # 최근 N턴만. 앞쪽은 버린다.
    for turn in history[-max_turns:]:
        messages.append({"role": turn.role, "content": turn.content})

    messages.append({"role": "user", "content": message})

    return messages


def build_sources(
    reports: list[CommunityResponse],
    places: list[Place],
    latitude: float | None = None,
    longitude: float | None = None,
) -> list[Source]:
    """
    근거 카드 데이터를 만든다.

    ⚠️ 이건 LLM이 만들면 안 된다. 존재하지 않는 contentid를 지어낸다.
       검색해서 실제로 DB에서 꺼낸 행만 담는다.
    """
    sources: list[Source] = []

    for report in reports:
        distance_m = None
        minutes = None

        if latitude is not None and longitude is not None:
            meters = haversine_m(latitude, longitude, report.latitude, report.longitude)
            distance_m = round(meters)
            minutes = walk_minutes(meters)

        sources.append(
            Source(
                type="report",
                id=str(report.response_id),
                title=report.facility_name or "이름 미상",
                address=report.address,
                latitude=report.latitude,
                longitude=report.longitude,
                distance_m=distance_m,
                walk_minutes=minutes,
                request_id=report.request_id,
            )
        )

    for place in places:
        distance_m = None
        minutes = None

        if latitude is not None and longitude is not None:
            meters = haversine_m(latitude, longitude, place.mapy, place.mapx)
            distance_m = round(meters)
            minutes = walk_minutes(meters)

        sources.append(
            Source(
                type="place",
                id=place.contentid,
                title=place.title,
                address=place.addr1,
                latitude=place.mapy,
                longitude=place.mapx,
                distance_m=distance_m,
                walk_minutes=minutes,
                image_url=place.firstimage or place.firstimage2,
            )
        )

    return sources
