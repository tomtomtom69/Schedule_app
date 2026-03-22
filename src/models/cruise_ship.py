import uuid
from datetime import date, time

from pydantic import BaseModel, field_validator
from sqlalchemy import Boolean, Column, Date, String, Time
from sqlalchemy.dialects.postgresql import UUID

from src.db.database import Base
from src.models.enums import Port, ShipSize

SEASON_START = (5, 1)    # (month, day)
SEASON_END = (10, 15)


def _in_season(d: date) -> bool:
    sm, sd = SEASON_START
    em, ed = SEASON_END
    start = date(d.year, sm, sd)
    end = date(d.year, em, ed)
    return start <= d <= end


# ── Pydantic schemas ────────────────────────────────────────────────────────


class CruiseShipBase(BaseModel):
    ship_name: str
    date: date
    arrival_time: time
    departure_time: time
    port: Port
    size: ShipSize
    good_ship: bool = False
    extra_language: str | None = None

    @field_validator("date")
    @classmethod
    def date_in_season(cls, v: date) -> date:
        if not _in_season(v):
            raise ValueError(f"Cruise ship date {v} is outside the operating season (May 1 – Oct 15)")
        return v

    @field_validator("extra_language")
    @classmethod
    def normalise_language(cls, v: str | None) -> str | None:
        if v is None:
            return v
        # Normalise each comma-separated language token
        parts = [p.lower().strip() for p in v.split(",") if p.strip()]
        return ",".join(parts) if parts else None


class CruiseShipCreate(CruiseShipBase):
    pass


class CruiseShipRead(CruiseShipBase):
    id: uuid.UUID

    model_config = {"from_attributes": True}


# ── SQLAlchemy ORM ──────────────────────────────────────────────────────────


class CruiseShipORM(Base):
    __tablename__ = "cruise_ships"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ship_name = Column(String, nullable=False)
    date = Column(Date, nullable=False)
    arrival_time = Column(Time, nullable=False)
    departure_time = Column(Time, nullable=False)
    port = Column(String, nullable=False)
    size = Column(String, nullable=False)
    good_ship = Column(Boolean, nullable=False, default=False)
    extra_language = Column(String, nullable=True)
