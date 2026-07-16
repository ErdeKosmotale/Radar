"""
멀티턴 RAG의 핵심 난점: 지시대명사 해소.

문제:
    User: 탄방동 근처 철봉 어디 있어?
    Bot:  (제보 기반 추천)
    User: 거기 화장실도 있어?     ← 이걸 그대로 검색하면 0건

LLM한테는 history가 들어가니까 말은 그럴싸하게 하는데, 검색 결과가
비어 있으니 근거 없이 지어낸다. 시연에서 딱 걸리는 케이스.

해결: 검색 '직전에' 질문을 독립적인 질의로 재작성한다.
비용 관리를 위해 지시어가 감지될 때만 LLM을 호출한다 (대부분 턴은 0원).
"""

import logging

from openai import OpenAI, OpenAIError

from app.chat.schemas import ChatMessage

logger = logging.getLogger(__name__)

# 이 단어가 있으면 앞 턴을 참조하는 질문일 가능성이 높다
ANAPHORA_MARKERS: tuple[str, ...] = (
    "거기",
    "그곳",
    "여기",
    "저기",
    "아까",
    "방금",
    "그건",
    "그거",
    "이거",
    "위에",
    "말한",
    "알려준",
    "추천한",
    "둘 중",
    "첫번째",
    "첫 번째",
    "두번째",
    "두 번째",
)

REWRITE_SYSTEM_PROMPT = """이전 대화를 참고해서, 사용자의 마지막 질문을 검색 가능한 독립 질문으로 바꿔라.

규칙:
- 지시대명사(거기, 그곳 등)를 실제 장소명이나 지명으로 바꿔라.
- 질문의 의도는 절대 바꾸지 마라.
- 답변하지 마라. 재작성된 질문 한 문장만 출력해라.
- 이전 대화에서 무엇을 가리키는지 알 수 없으면 원문을 그대로 출력해라.

예시:
이전: "탄방동 하천변 산책로에 철봉 3개 있어요"
질문: "거기 화장실도 있어?"
출력: 탄방동 하천변 산책로 화장실"""


def needs_rewrite(message: str, history: list[ChatMessage]) -> bool:
    """재작성이 필요한지 규칙으로 먼저 거른다. LLM 호출을 아끼는 게 목적."""
    if not history:
        return False
    return any(marker in message for marker in ANAPHORA_MARKERS)


def rewrite_query(
    client: OpenAI,
    message: str,
    history: list[ChatMessage],
    model: str,
) -> str:
    """
    지시어가 있으면 LLM으로 독립 질의를 만든다.
    실패하면 원문을 그대로 돌려준다 — 재작성 실패로 전체 요청이 죽으면 안 된다.
    """
    if not needs_rewrite(message, history):
        return message

    # 재작성에는 최근 4턴이면 충분하다. 10턴 다 넣으면 토큰 낭비.
    recent = history[-4:]
    context = "\n".join(f"{m.role}: {m.content}" for m in recent)

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
                {"role": "user", "content": f"이전 대화:\n{context}\n\n질문: {message}"},
            ],
            max_tokens=60,
            temperature=0,
        )

        rewritten = (response.choices[0].message.content or "").strip()

        if not rewritten:
            return message

        logger.info("질의 재작성: %r → %r", message, rewritten)
        return rewritten

    except OpenAIError as error:
        # 재작성은 부가 기능이다. 실패해도 원문으로 검색을 계속한다.
        logger.warning("질의 재작성 실패, 원문 사용: %s", error)
        return message
