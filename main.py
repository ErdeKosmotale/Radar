"""
FastAPI 앱 진입점.

⚠️ BE 담당과 조율 필요 — 이미 main.py 가 있으면 아래 두 줄만 옮겨붙일 것:
      from app.chat.router import router as chat_router
      app.include_router(chat_router)
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.chat.router import router as chat_router

app = FastAPI(title="동네레이더 API", version="0.1.0")

# Netlify(FE) → Render(BE) 크로스 오리진 요청 허용
# 배포 후 실제 Netlify 도메인으로 좁힐 것
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
