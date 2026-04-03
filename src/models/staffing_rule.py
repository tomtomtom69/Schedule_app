"""StaffingRule — DB-editable staffing minimums per season × scenario."""
from pydantic import BaseModel
from sqlalchemy import Column, Integer, String, UniqueConstraint

from src.db.database import Base


class StaffingRuleBase(BaseModel):
    season: str       # "low", "mid", "peak"
    scenario: str     # "no_cruise", "with_cruise", "with_good_ship", etc.
    cafe_needed: int
    production_needed: int


class StaffingRuleCreate(StaffingRuleBase):
    pass


class StaffingRuleRead(StaffingRuleBase):
    id: int

    model_config = {"from_attributes": True}


class StaffingRuleORM(Base):
    __tablename__ = "staffing_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    season = Column(String, nullable=False)
    scenario = Column(String, nullable=False)
    cafe_needed = Column(Integer, nullable=False)
    production_needed = Column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("season", "scenario", name="uq_staffing_rule_season_scenario"),
    )
