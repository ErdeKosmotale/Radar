"""
SQLAlchemy ORM 모델.

RFP III-5-가 "SQLAlchemy ORM 적용 SQLite 스키마 설계" 대응.
팀원이 Colab 노트북에서 raw sqlite3로 만든 스키마와 컬럼이 1:1로 일치한다.

⚠️ BE 담당과 조율 필요
   이 파일은 BE 담당이 소유하는 게 자연스럽다. 이미 models.py가 있다면
   이 파일을 버리고 chat 모듈에서 그쪽 모델을 import 할 것.
   (중복 정의하면 metadata 충돌 남)
"""

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Place(Base):
    """
    한국관광공사 TourAPI 4.0 제공 JSON을 적재한 테이블. 읽기 전용.

    출처: 한국관광공사 TourAPI 4.0 / 공공누리 3유형(출처표시·변경금지)
    → 필드 값을 가공·수정해서 노출하지 말 것. 기능명세서 라이선스 항목과 연동.
    """

    __tablename__ = "place"

    contentid: Mapped[str] = mapped_column(String, primary_key=True)
    contenttypeid: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    addr1: Mapped[str | None] = mapped_column(String)
    firstimage: Mapped[str | None] = mapped_column(String)
    firstimage2: Mapped[str | None] = mapped_column(String)

    # 주의: mapx = 경도(longitude), mapy = 위도(latitude)
    # TourAPI 원본 명명을 그대로 쓴 것이라 헷갈리기 쉽다.
    mapx: Mapped[float] = mapped_column(Float, nullable=False)
    mapy: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        Index("idx_place_contenttypeid", "contenttypeid"),
        Index("idx_place_location", "mapx", "mapy"),
    )

    def __repr__(self) -> str:
        return f"<Place {self.contentid} {self.title}>"


class CommunityRequest(Base):
    """사용자가 올린 시설 위치 요청(질문) 글."""

    __tablename__ = "community_request"

    request_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    question: Mapped[str] = mapped_column(Text, nullable=False)
    requested_facility: Mapped[str] = mapped_column(String, nullable=False)

    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)

    address: Mapped[str | None] = mapped_column(String)

    location_input_type: Mapped[str] = mapped_column(String, nullable=False)

    created_at: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=func.current_timestamp()
    )

    responses: Mapped[list["CommunityResponse"]] = relationship(
        back_populates="request",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint(
            "location_input_type IN ('GPS', 'ADDRESS')",
            name="ck_request_location_input_type",
        ),
        Index("idx_request_location", "latitude", "longitude"),
        Index("idx_request_facility", "requested_facility"),
    )

    def __repr__(self) -> str:
        return f"<CommunityRequest {self.request_id} {self.requested_facility}>"


class CommunityResponse(Base):
    """다른 사용자가 요청 글에 단 시설 위치 제보."""

    __tablename__ = "community_response"

    response_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    request_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("community_request.request_id", ondelete="CASCADE"),
        nullable=False,
    )

    facility_name: Mapped[str | None] = mapped_column(String)

    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)

    address: Mapped[str | None] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text)

    place_contentid: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("place.contentid", ondelete="SET NULL"),
    )

    created_at: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=func.current_timestamp()
    )

    request: Mapped["CommunityRequest"] = relationship(back_populates="responses")
    place: Mapped["Place | None"] = relationship()

    __table_args__ = (
        Index("idx_response_request_id", "request_id"),
        Index("idx_response_location", "latitude", "longitude"),
    )

    def __repr__(self) -> str:
        return f"<CommunityResponse {self.response_id} {self.facility_name}>"
