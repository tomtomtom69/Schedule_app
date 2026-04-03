"""ClosedDay — dates when the shop is not open and no staffing is needed."""
import uuid
from datetime import date

from pydantic import BaseModel
from sqlalchemy import Column, Date, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from src.db.database import Base


class ClosedDayBase(BaseModel):
    date: date
    year: int
    reason: str | None = None


class ClosedDayCreate(ClosedDayBase):
    pass


class ClosedDayRead(ClosedDayBase):
    id: uuid.UUID

    model_config = {"from_attributes": True}


class ClosedDayORM(Base):
    __tablename__ = "closed_days"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    date = Column(Date, nullable=False, unique=True)
    year = Column(Integer, nullable=False)
    reason = Column(Text, nullable=True)
