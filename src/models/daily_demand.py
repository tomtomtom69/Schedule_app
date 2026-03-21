"""ORM model for cached daily staffing demand — Phase 2."""
import uuid
from datetime import date

from pydantic import BaseModel
from sqlalchemy import Column, Date, Float, Integer, JSON, String
from sqlalchemy.dialects.postgresql import UUID

from src.db.database import Base


# ── Pydantic schemas ────────────────────────────────────────────────────────


class DailyDemandRecordBase(BaseModel):
    date: date
    season: str
    production_needed: int
    cafe_needed: int
    languages_required: list[str]
    ship_summary: dict  # serialised list of ship names/details for display


class DailyDemandRecordCreate(DailyDemandRecordBase):
    pass


class DailyDemandRecordRead(DailyDemandRecordBase):
    id: uuid.UUID

    model_config = {"from_attributes": True}


# ── SQLAlchemy ORM ──────────────────────────────────────────────────────────


class DailyDemandORM(Base):
    __tablename__ = "daily_demands"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    date = Column(Date, nullable=False, unique=True, index=True)
    season = Column(String, nullable=False)
    production_needed = Column(Integer, nullable=False)
    cafe_needed = Column(Integer, nullable=False)
    languages_required = Column(JSON, nullable=False, default=list)
    ship_summary = Column(JSON, nullable=False, default=dict)
