# Phase 2: Demand Engine — Implementation Guide

**Goal:** Given a month + cruise ship data, produce a daily staffing demand profile (how many production + café staff needed each day, which languages required).

**Instructions for Claude Code:** Implement all files described below without requesting approval for each step. Only ask for approval before running system commands. Read SPEC.md Section 7 and 8 for the complete staffing rules.

---

## 2.1 Season Detection

### `src/demand/seasonal_rules.py`

```python
def get_season(date: date) -> Season:
    """Determine season for a given date."""
    # May 1 - May 31: low
    # Jun 1 - Jun 15: mid
    # Jun 16 - Aug 31: peak
    # Sep 1 - Oct 15: low
    # Outside range: closed (raise error)
```

Define staffing tables as data, not logic:

```python
STAFFING_RULES = {
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
```

These tables should be stored in the database (via EstablishmentSettings) so the business owner can adjust them in the UI. The hardcoded values above are defaults loaded by the seed script.

---

## 2.2 Cruise Ship Impact Calculator

### `src/demand/forecaster.py`

```python
from dataclasses import dataclass

@dataclass
class DailyDemand:
    date: date
    season: Season
    day_of_week: str
    has_cruise: bool
    has_good_ship: bool
    geiranger_ship_count: int
    hellesylt_ship_count: int
    effective_ship_impact: float  # geiranger=1.0 per ship, hellesylt=0.5
    production_needed: int
    cafe_needed: int
    languages_required: list[str]  # extra languages needed (besides English)
    ships_today: list[CruiseShipRead]  # full ship details for display

def calculate_daily_demand(
    date: date,
    ships_on_date: list[CruiseShipRead],
    season: Season,
) -> DailyDemand:
    """
    For a single day, calculate staffing needs.
    
    Logic:
    1. Count ships by port type (Geiranger vs Hellesylt)
    2. Calculate effective impact (Hellesylt = half)
    3. Determine if "good ship" present (any good_ship=True)
    4. Look up staffing rule from seasonal table
    5. Collect required languages from ships
    """

def generate_monthly_demand(
    year: int,
    month: int,
    ships: list[CruiseShipRead],
) -> list[DailyDemand]:
    """
    Generate demand profile for every day of the month.
    Returns list of DailyDemand, one per day.
    Only includes days within the operating season (May 1 - Oct 15).
    """
```

### Hellesylt Impact Rule

When calculating effective ship count:
- Each Geiranger ship = 1.0 impact
- Each Hellesylt ship = 0.5 impact
- Total effective impact determines which staffing tier applies

For staffing tier selection:
- effective_impact == 0 → "no_cruise" rules
- effective_impact > 0 and no good_ship → "with_cruise" rules  
- any good_ship present → "with_good_ship" rules

For low season, also check Saturday separately (different baseline).

---

## 2.3 Language Matcher

### `src/demand/language_matcher.py`

```python
def get_required_languages(
    ships_on_date: list[CruiseShipRead],
    ship_language_map: dict[str, str],  # ship_name → language
) -> list[str]:
    """
    Determine which extra languages are needed on a given day.
    
    1. For each ship in port, look up its primary language
    2. Use ship's extra_language field if set, otherwise use language map
    3. Filter out "english" (everyone speaks it)
    4. Return deduplicated list of required languages
    """

def check_language_coverage(
    required_languages: list[str],
    available_employees: list[EmployeeRead],
) -> dict[str, bool]:
    """
    For each required language, check if at least one available employee speaks it.
    Returns {"spanish": True, "german": False} etc.
    Used for warnings in the UI.
    """
```

---

## 2.4 Database Integration

Add to DB:
- `daily_demands` table (cache of computed demand, regenerated when ships change)
- Fields: date, season, production_needed, cafe_needed, languages_required (JSON), ship_summary (JSON)

This allows the UI to show the demand calendar without recalculating each time.

---

## 2.5 Acceptance Criteria

Phase 2 is complete when:
- [ ] `get_season(date)` correctly classifies all dates in the May–October range
- [ ] `generate_monthly_demand()` produces 28–31 DailyDemand objects for any valid month
- [ ] A peak-season day with a good Geiranger ship returns production=3, cafe=5
- [ ] A low-season Saturday with no ships returns production=1, cafe=3
- [ ] A day with only a Hellesylt big ship is treated as "with_cruise" (0.5 impact ≥ threshold)
- [ ] Language requirements are correctly extracted from ships and ship_language_map
- [ ] Demand results are stored in the database for UI display
