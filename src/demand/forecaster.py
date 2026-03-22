"""Demand forecaster — Phase 2.

Translates cruise ship arrivals into daily staffing demand profiles.
"""
import calendar
from dataclasses import dataclass, field
from datetime import date

from src.demand.seasonal_rules import (
    STAFFING_RULES,
    Season,
    get_season,
    get_staffing_scenario,
)
from src.models.cruise_ship import CruiseShipRead
from src.models.enums import Port


# Geiranger ports all have full (1.0) impact; Hellesylt is half (0.5).
_PORT_IMPACT: dict[Port, float] = {
    Port.geiranger_4B_SW: 1.0,
    Port.geiranger_3S: 1.0,
    Port.geiranger_2: 1.0,
    Port.hellesylt: 0.5,
}


def _is_geiranger(port: Port) -> bool:
    return port in (Port.geiranger_4B_SW, Port.geiranger_3S, Port.geiranger_2)


@dataclass
class DailyDemand:
    date: date
    season: Season
    day_of_week: str               # "Monday", "Tuesday", …
    has_cruise: bool
    has_good_ship: bool
    geiranger_ship_count: int
    hellesylt_ship_count: int
    effective_ship_impact: float   # geiranger=1.0 per ship, hellesylt=0.5
    production_needed: int
    cafe_needed: int
    languages_required: list[str] = field(default_factory=list)
    ships_today: list[CruiseShipRead] = field(default_factory=list)


def calculate_daily_demand(
    d: date,
    ships_on_date: list[CruiseShipRead],
    season: Season,
) -> DailyDemand:
    """For a single day, calculate staffing needs.

    Steps:
    1. Count ships by port type (Geiranger vs Hellesylt).
    2. Calculate effective impact (Hellesylt = 0.5).
    3. Determine if any 'good_ship' is present.
    4. Look up staffing rule from STAFFING_RULES.
    5. Collect required languages from ship.extra_language fields.
    """
    from src.demand.language_matcher import get_required_languages

    geiranger_count = sum(1 for s in ships_on_date if _is_geiranger(s.port))
    hellesylt_count = sum(1 for s in ships_on_date if s.port == Port.hellesylt)
    effective_impact = sum(
        _PORT_IMPACT.get(s.port, 1.0) for s in ships_on_date
    )
    has_good_ship = any(s.good_ship for s in ships_on_date)
    has_cruise = len(ships_on_date) > 0

    is_saturday = d.weekday() == 5  # 0=Mon … 5=Sat, 6=Sun
    scenario = get_staffing_scenario(season, effective_impact, has_good_ship, is_saturday)

    rules = STAFFING_RULES[season][scenario]
    production_needed = rules["production"]
    cafe_needed = rules["cafe"]

    languages_required = get_required_languages(ships_on_date)

    return DailyDemand(
        date=d,
        season=season,
        day_of_week=d.strftime("%A"),
        has_cruise=has_cruise,
        has_good_ship=has_good_ship,
        geiranger_ship_count=geiranger_count,
        hellesylt_ship_count=hellesylt_count,
        effective_ship_impact=effective_impact,
        production_needed=production_needed,
        cafe_needed=cafe_needed,
        languages_required=languages_required,
        ships_today=list(ships_on_date),
    )


def generate_monthly_demand(
    year: int,
    month: int,
    ships: list[CruiseShipRead],
) -> list[DailyDemand]:
    """Generate demand profile for every day of the month.

    Returns one DailyDemand per day that falls within the operating season
    (May 1 – Oct 15). Days outside the season are silently skipped.
    Ships are filtered to those matching each day's date.
    """
    _, days_in_month = calendar.monthrange(year, month)
    demands: list[DailyDemand] = []

    for day in range(1, days_in_month + 1):
        d = date(year, month, day)
        try:
            season = get_season(d)
        except ValueError:
            continue  # outside operating season

        ships_today = [s for s in ships if s.date == d]
        demand = calculate_daily_demand(d, ships_today, season)
        demands.append(demand)

    return demands
