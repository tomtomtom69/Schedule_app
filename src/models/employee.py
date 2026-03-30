import uuid
from datetime import date
from typing import Any, Optional

from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import Boolean, Column, Date, Float, JSON, String
from sqlalchemy.dialects.postgresql import UUID

from src.db.database import Base
from src.models.enums import EmploymentType, Housing, RoleCapability


# ── Age helpers ──────────────────────────────────────────────────────────────


def get_age_on_date(date_of_birth: date, target_date: date) -> int:
    """Return the employee's age in whole years on *target_date*."""
    age = target_date.year - date_of_birth.year
    # Subtract 1 if birthday hasn't occurred yet in target year
    if (target_date.month, target_date.day) < (date_of_birth.month, date_of_birth.day):
        age -= 1
    return age


def get_age_category(age: int) -> str:
    """Return age category string: 'under_15', 'age_15_18', or 'adult'."""
    if age < 15:
        return "under_15"
    if age < 18:
        return "age_15_18"
    return "adult"

SEASON_START_MONTH = 5   # May
SEASON_START_DAY = 1
SEASON_END_MONTH = 10
SEASON_END_DAY = 15


# ── Pydantic schemas ────────────────────────────────────────────────────────


class EmployeeBase(BaseModel):
    name: str
    languages: list[str]
    role_capability: RoleCapability
    employment_type: EmploymentType
    contracted_hours: float
    housing: Housing
    driving_licence: bool
    availability_start: date
    availability_end: date
    preferences: dict[str, Any] | None = None
    date_of_birth: Optional[date] = None

    @field_validator("languages")
    @classmethod
    def languages_must_include_english(cls, v: list[str]) -> list[str]:
        normalised = [lang.lower().strip() for lang in v]
        if "english" not in normalised:
            normalised = ["english"] + normalised
        return normalised

    @field_validator("contracted_hours")
    @classmethod
    def hours_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("contracted_hours must be positive")
        return v

    @model_validator(mode="after")
    def validate_dates(self) -> "EmployeeBase":
        start = self.availability_start
        end = self.availability_end
        if start >= end:
            raise ValueError("availability_start must be before availability_end")
        season_start = date(start.year, SEASON_START_MONTH, SEASON_START_DAY)
        season_end = date(end.year, SEASON_END_MONTH, SEASON_END_DAY)
        if start < season_start or end > season_end:
            raise ValueError(
                f"Availability must fall within the season "
                f"(May 1 – Oct 15). Got {start} – {end}"
            )
        return self


class EmployeeCreate(EmployeeBase):
    pass


class EmployeeRead(EmployeeBase):
    id: uuid.UUID

    model_config = {"from_attributes": True}


# ── SQLAlchemy ORM ──────────────────────────────────────────────────────────


class EmployeeORM(Base):
    __tablename__ = "employees"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    languages = Column(JSON, nullable=False)          # stored as JSON array
    role_capability = Column(String, nullable=False)
    employment_type = Column(String, nullable=False)
    contracted_hours = Column(Float, nullable=False)
    housing = Column(String, nullable=False)
    driving_licence = Column(Boolean, nullable=False, default=False)
    availability_start = Column(Date, nullable=False)
    availability_end = Column(Date, nullable=False)
    preferences = Column(JSON, nullable=True)
    date_of_birth = Column(Date, nullable=True)
