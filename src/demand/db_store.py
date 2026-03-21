"""Persist and retrieve computed daily demand from the database — Phase 2."""
from datetime import date

from sqlalchemy.orm import Session

from src.demand.forecaster import DailyDemand
from src.models.daily_demand import DailyDemandORM, DailyDemandRecordRead


def _demand_to_orm(demand: DailyDemand) -> DailyDemandORM:
    ship_summary = [
        {
            "ship_name": s.ship_name,
            "port": s.port.value,
            "good_ship": s.good_ship,
            "arrival_time": s.arrival_time.isoformat(),
            "departure_time": s.departure_time.isoformat(),
        }
        for s in demand.ships_today
    ]
    return DailyDemandORM(
        date=demand.date,
        season=demand.season.value,
        production_needed=demand.production_needed,
        cafe_needed=demand.cafe_needed,
        languages_required=demand.languages_required,
        ship_summary={"ships": ship_summary},
    )


def upsert_daily_demand(db: Session, demand: DailyDemand) -> DailyDemandORM:
    """Insert or replace the daily demand record for the given date."""
    existing = db.query(DailyDemandORM).filter(DailyDemandORM.date == demand.date).first()
    if existing:
        existing.season = demand.season.value
        existing.production_needed = demand.production_needed
        existing.cafe_needed = demand.cafe_needed
        existing.languages_required = demand.languages_required
        existing.ship_summary = {
            "ships": [
                {
                    "ship_name": s.ship_name,
                    "port": s.port.value,
                    "good_ship": s.good_ship,
                    "arrival_time": s.arrival_time.isoformat(),
                    "departure_time": s.departure_time.isoformat(),
                }
                for s in demand.ships_today
            ]
        }
        db.flush()
        return existing
    orm = _demand_to_orm(demand)
    db.add(orm)
    db.flush()
    return orm


def save_monthly_demand(db: Session, demands: list[DailyDemand]) -> int:
    """Persist a full list of DailyDemand objects (upsert). Returns count saved."""
    for demand in demands:
        upsert_daily_demand(db, demand)
    return len(demands)


def get_demand_for_month(db: Session, year: int, month: int) -> list[DailyDemandRecordRead]:
    """Retrieve all cached demand rows for a given year/month."""
    start = date(year, month, 1)
    import calendar
    _, last_day = calendar.monthrange(year, month)
    end = date(year, month, last_day)
    rows = (
        db.query(DailyDemandORM)
        .filter(DailyDemandORM.date >= start, DailyDemandORM.date <= end)
        .order_by(DailyDemandORM.date)
        .all()
    )
    return [DailyDemandRecordRead.model_validate(r) for r in rows]


def get_demand_for_date(db: Session, d: date) -> DailyDemandRecordRead | None:
    """Retrieve a single cached demand row."""
    row = db.query(DailyDemandORM).filter(DailyDemandORM.date == d).first()
    return DailyDemandRecordRead.model_validate(row) if row else None
