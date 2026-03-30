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

    @property
    def worked_hours(self) -> float:
        """Hours actually worked per shift (template duration minus 0.5h mandatory break).

        Standard 8h shifts (e.g. 08:00–16:00) → 7.5h worked.
        Shift 6 (10:00–17:00, 7h total) → 6.5h worked.
        """
        duration_minutes = _time_to_minutes(self.end_time) - _time_to_minutes(self.start_time)
        return duration_minutes / 60.0 - 0.5

    @property
    def worked_minutes(self) -> int:
        """Worked hours expressed as integer minutes (for use as CP-SAT coefficients)."""
        return int(round(self.worked_hours * 60))


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
