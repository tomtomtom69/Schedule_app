from datetime import datetime, time
from typing import Any

from pydantic import BaseModel, model_validator
from sqlalchemy import Column, String, Time

from src.db.database import Base
from src.models.enums import ShiftRole

MAX_SHIFT_HOURS = 10


def _time_to_minutes(t: time) -> int:
    return t.hour * 60 + t.minute


# ── Pydantic schemas ────────────────────────────────────────────────────────


class ShiftTemplateBase(BaseModel):
    id: str        # e.g. "1", "P2"
    role: ShiftRole
    label: str
    start_time: time
    end_time: time

    @model_validator(mode="after")
    def validate_shift_duration(self) -> "ShiftTemplateBase":
        duration_minutes = _time_to_minutes(self.end_time) - _time_to_minutes(self.start_time)
        if duration_minutes <= 0:
            raise ValueError("end_time must be after start_time")
        if duration_minutes > MAX_SHIFT_HOURS * 60:
            raise ValueError(f"Shift duration must not exceed {MAX_SHIFT_HOURS} hours")
        return self


class ShiftTemplateCreate(ShiftTemplateBase):
    pass


class ShiftTemplateRead(ShiftTemplateBase):
    model_config = {"from_attributes": True}


# ── SQLAlchemy ORM ──────────────────────────────────────────────────────────


class ShiftTemplateORM(Base):
    __tablename__ = "shift_templates"

    id = Column(String, primary_key=True)
    role = Column(String, nullable=False)
    label = Column(String, nullable=False)
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=False)
