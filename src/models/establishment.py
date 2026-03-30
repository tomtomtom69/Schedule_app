from datetime import date, time

from pydantic import BaseModel, model_validator
from sqlalchemy import Column, Date, Integer, String, Time

from src.db.database import Base
from src.models.enums import Season


# ── Pydantic schemas ────────────────────────────────────────────────────────


class EstablishmentSettingsBase(BaseModel):
    season: Season
    date_range_start: date
    date_range_end: date
    opening_time: time
    closing_time: time
    production_start: time
    max_cafe_per_day: int = 5   # hard cap on café staff; 6 allowed when ≥2 good ships
    max_prod_per_day: int = 4   # hard cap on production staff

    @model_validator(mode="after")
    def validate_times_and_dates(self) -> "EstablishmentSettingsBase":
        if self.opening_time >= self.closing_time:
            raise ValueError("opening_time must be before closing_time")
        if self.date_range_start >= self.date_range_end:
            raise ValueError("date_range_start must be before date_range_end")
        return self


class EstablishmentSettingsCreate(EstablishmentSettingsBase):
    pass


class EstablishmentSettingsRead(EstablishmentSettingsBase):
    id: int

    model_config = {"from_attributes": True}


# ── SQLAlchemy ORM ──────────────────────────────────────────────────────────


class EstablishmentSettingsORM(Base):
    __tablename__ = "establishment_settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    season = Column(String, nullable=False)
    date_range_start = Column(Date, nullable=False)
    date_range_end = Column(Date, nullable=False)
    opening_time = Column(Time, nullable=False)
    closing_time = Column(Time, nullable=False)
    production_start = Column(Time, nullable=False)
    max_cafe_per_day = Column(Integer, nullable=False, default=5)
    max_prod_per_day = Column(Integer, nullable=False, default=4)
