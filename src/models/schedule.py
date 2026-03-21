import uuid
from datetime import date, datetime

from pydantic import BaseModel, field_validator
from sqlalchemy import Boolean, Column, DateTime, Date, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from src.db.database import Base
from src.models.enums import ScheduleStatus

VALID_MONTHS = range(5, 11)  # May–October


# ── Pydantic schemas ────────────────────────────────────────────────────────


class AssignmentBase(BaseModel):
    employee_id: uuid.UUID
    date: date
    shift_id: str
    is_day_off: bool = False
    notes: str | None = None


class AssignmentCreate(AssignmentBase):
    pass


class AssignmentRead(AssignmentBase):
    id: uuid.UUID
    schedule_id: uuid.UUID

    model_config = {"from_attributes": True}


class ScheduleBase(BaseModel):
    month: int
    year: int
    status: ScheduleStatus = ScheduleStatus.draft

    @field_validator("month")
    @classmethod
    def month_in_season(cls, v: int) -> int:
        if v not in VALID_MONTHS:
            raise ValueError(f"month must be between 5 and 10 (May–October), got {v}")
        return v

    @field_validator("year")
    @classmethod
    def year_reasonable(cls, v: int) -> int:
        if v < 2024 or v > 2100:
            raise ValueError(f"year {v} is out of expected range")
        return v


class ScheduleCreate(ScheduleBase):
    pass


class ScheduleRead(ScheduleBase):
    id: uuid.UUID
    assignments: list[AssignmentRead] = []
    created_at: datetime
    modified_at: datetime | None = None

    model_config = {"from_attributes": True}


# ── SQLAlchemy ORM ──────────────────────────────────────────────────────────


class ScheduleORM(Base):
    __tablename__ = "schedules"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    month = Column(Integer, nullable=False)
    year = Column(Integer, nullable=False)
    status = Column(String, nullable=False, default=ScheduleStatus.draft.value)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    modified_at = Column(DateTime, nullable=True)

    assignments = relationship("AssignmentORM", back_populates="schedule", cascade="all, delete-orphan")


class AssignmentORM(Base):
    __tablename__ = "assignments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    schedule_id = Column(UUID(as_uuid=True), ForeignKey("schedules.id"), nullable=False)
    employee_id = Column(UUID(as_uuid=True), ForeignKey("employees.id"), nullable=False)
    date = Column(Date, nullable=False)
    shift_id = Column(String, nullable=False)
    is_day_off = Column(Boolean, nullable=False, default=False)
    notes = Column(Text, nullable=True)

    schedule = relationship("ScheduleORM", back_populates="assignments")
