"""
SQLite 연결 및 세션 관리.

⚠️ BE 담당과 조율 필요 — 이미 db.py가 있으면 그쪽을 쓸 것.
"""

import os
from collections.abc import Generator
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

load_dotenv()

# RFP III-5-나: DB 경로도 민감정보로 분류 → .env 관리
DB_PATH = Path(os.getenv("DB_PATH", "./local.db"))

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    # FastAPI는 요청마다 다른 스레드에서 돌 수 있음
    connect_args={"check_same_thread": False},
    echo=False,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


@event.listens_for(Engine, "connect")
def _enable_foreign_keys(dbapi_connection, connection_record) -> None:
    """SQLite는 외래키를 연결마다 켜줘야 한다."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI 의존성 주입용 세션."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
