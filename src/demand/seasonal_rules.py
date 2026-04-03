"""Seasonal rules engine — Phase 2."""
from datetime import date

from src.models.enums import Season


def get_season(d: date) -> Season:
    """Determine season for a given date.

    May 1  – May 31:  low
    Jun 1  – Jun 15:  mid
    Jun 16 – Aug 31:  peak
    Sep 1  – Oct 15:  low

    Raises ValueError for dates outside the operating season.
    """
    m, day = d.month, d.day

    if m == 5:
        return Season.low
    if m == 6 and day <= 15:
        return Season.mid
    if (m == 6 and day >= 16) or m == 7 or m == 8:
        return Season.peak
    if m == 9 or (m == 10 and day <= 15):
        return Season.low

    raise ValueError(
        f"Date {d} is outside the operating season (May 1 – Oct 15)."
    )


# Staffing tables keyed by season → scenario → role.
# These defaults are loaded into the database via EstablishmentSettings
# so the business owner can adjust them through the UI.
STAFFING_RULES: dict[Season, dict[str, dict[str, int]]] = {
    Season.low: {
        "no_cruise_weekday":  {"production": 1, "cafe": 2},
        "no_cruise_saturday": {"production": 1, "cafe": 3},
        "with_cruise":        {"production": 1, "cafe": 3},
        "with_good_ship":     {"production": 1, "cafe": 4},
    },
    Season.mid: {
        "no_cruise":          {"production": 1, "cafe": 2},
        "with_cruise":        {"production": 1, "cafe": 3},
        "with_good_ship":     {"production": 1, "cafe": 4},
    },
    Season.peak: {
        "no_cruise":          {"production": 2, "cafe": 3},
        "with_cruise":        {"production": 3, "cafe": 4},
        "with_good_ship":     {"production": 3, "cafe": 5},
    },
}


def load_staffing_rules_from_db() -> dict:
    """Load staffing rules from DB.  Falls back to hardcoded STAFFING_RULES if the
    table is empty or unavailable (e.g. before first migration).

    Returns the same nested structure as STAFFING_RULES:
      {Season: {scenario_str: {"cafe": int, "production": int}}}
    """
    try:
        from src.db.database import db_session
        from src.models.staffing_rule import StaffingRuleORM
        with db_session() as db:
            rows = db.query(StaffingRuleORM).all()
        if not rows:
            return STAFFING_RULES
        result: dict = {}
        for r in rows:
            try:
                season = Season(r.season)
            except ValueError:
                continue
            result.setdefault(season, {})[r.scenario] = {
                "cafe": r.cafe_needed,
                "production": r.production_needed,
            }
        # Fill any missing seasons/scenarios from defaults so the app never crashes
        for s, scenarios in STAFFING_RULES.items():
            for scenario, counts in scenarios.items():
                result.setdefault(s, {}).setdefault(scenario, counts)
        return result
    except Exception:
        return STAFFING_RULES


def get_staffing_scenario(
    season: Season,
    effective_impact: float,
    has_good_ship: bool,
    is_saturday: bool,
) -> str:
    """Return the staffing scenario key for looking up STAFFING_RULES."""
    if has_good_ship:
        return "with_good_ship"
    if effective_impact > 0:
        return "with_cruise"
    # No cruise
    if season == Season.low and is_saturday:
        return "no_cruise_saturday"
    if season == Season.low:
        return "no_cruise_weekday"
    return "no_cruise"
