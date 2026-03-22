"""Tests for Phase 2: Demand Engine."""
import uuid
from datetime import date, time

import pytest

from src.demand.forecaster import DailyDemand, calculate_daily_demand, generate_monthly_demand
from src.demand.language_matcher import check_language_coverage, get_required_languages
from src.demand.seasonal_rules import STAFFING_RULES, Season, get_season, get_staffing_scenario
from src.models.cruise_ship import CruiseShipRead
from src.models.employee import EmployeeRead
from src.models.enums import EmploymentType, Housing, Port, RoleCapability, ShipSize

# ── Helpers ─────────────────────────────────────────────────────────────────


def _ship(
    ship_name: str = "Test Ship",
    d: date = date(2026, 7, 1),
    port: Port = Port.geiranger_4B_SW,
    size: ShipSize = ShipSize.big,
    good_ship: bool = False,
    extra_language: str | None = None,
) -> CruiseShipRead:
    return CruiseShipRead(
        id=uuid.uuid4(),
        ship_name=ship_name,
        date=d,
        arrival_time=time(8, 0),
        departure_time=time(18, 0),
        port=port,
        size=size,
        good_ship=good_ship,
        extra_language=extra_language,
    )


def _employee(
    name: str = "Test Employee",
    languages: list[str] | None = None,
    role_capability: RoleCapability = RoleCapability.cafe,
) -> EmployeeRead:
    return EmployeeRead(
        id=uuid.uuid4(),
        name=name,
        languages=languages or ["english"],
        role_capability=role_capability,
        employment_type=EmploymentType.full_time,
        contracted_hours=37.5,
        housing=Housing.geiranger,
        driving_licence=False,
        availability_start=date(2026, 5, 1),
        availability_end=date(2026, 10, 15),
    )


# ── Season detection ─────────────────────────────────────────────────────────


class TestGetSeason:
    def test_low_may(self):
        assert get_season(date(2026, 5, 1)) == Season.low
        assert get_season(date(2026, 5, 31)) == Season.low

    def test_mid_june_first_half(self):
        assert get_season(date(2026, 6, 1)) == Season.mid
        assert get_season(date(2026, 6, 15)) == Season.mid

    def test_peak_june_second_half(self):
        assert get_season(date(2026, 6, 16)) == Season.peak
        assert get_season(date(2026, 6, 30)) == Season.peak

    def test_peak_july_august(self):
        assert get_season(date(2026, 7, 1)) == Season.peak
        assert get_season(date(2026, 8, 31)) == Season.peak

    def test_low_september_october(self):
        assert get_season(date(2026, 9, 1)) == Season.low
        assert get_season(date(2026, 10, 15)) == Season.low

    def test_outside_season_raises(self):
        with pytest.raises(ValueError):
            get_season(date(2026, 4, 30))
        with pytest.raises(ValueError):
            get_season(date(2026, 10, 16))
        with pytest.raises(ValueError):
            get_season(date(2026, 1, 1))
        with pytest.raises(ValueError):
            get_season(date(2026, 12, 31))


# ── Staffing scenario selection ───────────────────────────────────────────────


class TestGetStaffingScenario:
    def test_good_ship_always_wins(self):
        for season in Season:
            assert get_staffing_scenario(season, 2.0, True, False) == "with_good_ship"

    def test_with_cruise_no_good_ship(self):
        assert get_staffing_scenario(Season.peak, 1.0, False, False) == "with_cruise"
        assert get_staffing_scenario(Season.mid, 0.5, False, False) == "with_cruise"

    def test_low_no_cruise_weekday(self):
        assert get_staffing_scenario(Season.low, 0.0, False, False) == "no_cruise_weekday"

    def test_low_no_cruise_saturday(self):
        assert get_staffing_scenario(Season.low, 0.0, False, True) == "no_cruise_saturday"

    def test_mid_peak_no_cruise_no_saturday_split(self):
        assert get_staffing_scenario(Season.mid, 0.0, False, True) == "no_cruise"
        assert get_staffing_scenario(Season.peak, 0.0, False, True) == "no_cruise"


# ── Acceptance criteria from PHASE2_DEMAND.md ───────────────────────────────


class TestAcceptanceCriteria:
    def test_peak_good_geiranger_ship(self):
        """Peak season + good Geiranger ship → production=3, cafe=5."""
        d = date(2026, 7, 15)  # peak, Tuesday
        ships = [_ship(port=Port.geiranger_4B_SW, good_ship=True, d=d)]
        demand = calculate_daily_demand(d, ships, Season.peak)
        assert demand.production_needed == 3
        assert demand.cafe_needed == 5

    def test_low_saturday_no_ships(self):
        """Low season Saturday, no ships → production=1, cafe=3."""
        d = date(2026, 5, 2)  # Saturday in May 2026
        assert d.weekday() == 5, f"Expected Saturday, got weekday {d.weekday()}"
        demand = calculate_daily_demand(d, [], Season.low)
        assert demand.production_needed == 1
        assert demand.cafe_needed == 3

    def test_hellesylt_big_ship_treated_as_with_cruise(self):
        """Hellesylt ship alone (0.5 impact > 0) → with_cruise tier."""
        d = date(2026, 7, 1)  # peak, Wednesday
        ships = [_ship(port=Port.hellesylt, good_ship=False, d=d)]
        demand = calculate_daily_demand(d, ships, Season.peak)
        # effective_impact = 0.5 > 0, no good_ship → "with_cruise"
        assert demand.effective_ship_impact == 0.5
        assert demand.has_cruise is True
        expected = STAFFING_RULES[Season.peak]["with_cruise"]
        assert demand.production_needed == expected["production"]
        assert demand.cafe_needed == expected["cafe"]


# ── Daily demand calculation ─────────────────────────────────────────────────


class TestCalculateDailyDemand:
    def test_no_ships_peak_weekday(self):
        d = date(2026, 7, 2)  # Thursday
        demand = calculate_daily_demand(d, [], Season.peak)
        assert demand.production_needed == 2
        assert demand.cafe_needed == 3
        assert demand.has_cruise is False
        assert demand.has_good_ship is False
        assert demand.geiranger_ship_count == 0
        assert demand.hellesylt_ship_count == 0
        assert demand.effective_ship_impact == 0.0

    def test_geiranger_ship_impact(self):
        d = date(2026, 7, 2)
        ships = [_ship(port=Port.geiranger_3S, d=d)]
        demand = calculate_daily_demand(d, ships, Season.peak)
        assert demand.effective_ship_impact == 1.0
        assert demand.geiranger_ship_count == 1
        assert demand.hellesylt_ship_count == 0

    def test_multiple_ships_impact(self):
        d = date(2026, 7, 2)
        ships = [
            _ship("Ship A", d, Port.geiranger_4B_SW),
            _ship("Ship B", d, Port.hellesylt),
        ]
        demand = calculate_daily_demand(d, ships, Season.peak)
        assert demand.effective_ship_impact == 1.5
        assert demand.geiranger_ship_count == 1
        assert demand.hellesylt_ship_count == 1

    def test_day_of_week_field(self):
        d = date(2026, 7, 6)  # Monday
        demand = calculate_daily_demand(d, [], Season.peak)
        assert demand.day_of_week == "Monday"

    def test_season_field_preserved(self):
        d = date(2026, 6, 1)
        demand = calculate_daily_demand(d, [], Season.mid)
        assert demand.season == Season.mid

    def test_ships_today_field(self):
        d = date(2026, 7, 1)
        ships = [_ship("Costa Luminosa", d)]
        demand = calculate_daily_demand(d, ships, Season.peak)
        assert len(demand.ships_today) == 1
        assert demand.ships_today[0].ship_name == "Costa Luminosa"


# ── Monthly demand generation ────────────────────────────────────────────────


class TestGenerateMonthlyDemand:
    def test_may_returns_31_days(self):
        demands = generate_monthly_demand(2026, 5, [])
        assert len(demands) == 31

    def test_june_returns_30_days(self):
        demands = generate_monthly_demand(2026, 6, [])
        assert len(demands) == 30

    def test_october_returns_only_15_days(self):
        """Oct 16-31 is outside the season and must be skipped."""
        demands = generate_monthly_demand(2026, 10, [])
        assert len(demands) == 15
        assert all(d.date.day <= 15 for d in demands)

    def test_outside_season_month_returns_empty(self):
        demands = generate_monthly_demand(2026, 4, [])
        assert demands == []
        demands = generate_monthly_demand(2026, 11, [])
        assert demands == []

    def test_ships_assigned_to_correct_days(self):
        ship_jul1 = _ship("Ship A", date(2026, 7, 1))
        ship_jul2 = _ship("Ship B", date(2026, 7, 2))
        demands = generate_monthly_demand(2026, 7, [ship_jul1, ship_jul2])
        day1 = next(d for d in demands if d.date.day == 1)
        day2 = next(d for d in demands if d.date.day == 2)
        day3 = next(d for d in demands if d.date.day == 3)
        assert len(day1.ships_today) == 1
        assert len(day2.ships_today) == 1
        assert len(day3.ships_today) == 0

    def test_generates_28_to_31_objects_for_valid_months(self):
        for month in range(5, 11):
            demands = generate_monthly_demand(2026, month, [])
            assert len(demands) >= 1, f"Month {month} should have at least 1 day in season"


# ── Language matching ────────────────────────────────────────────────────────


class TestGetRequiredLanguages:
    def test_extra_language_used(self):
        d = date(2026, 7, 1)
        ships = [_ship(extra_language="spanish", d=d)]
        langs = get_required_languages(ships)
        assert langs == ["spanish"]

    def test_comma_separated_extra_language(self):
        d = date(2026, 7, 1)
        ships = [_ship(extra_language="italian,spanish", d=d)]
        langs = get_required_languages(ships)
        assert langs == ["italian", "spanish"]

    def test_english_filtered_out(self):
        d = date(2026, 7, 1)
        ships = [_ship(extra_language="English", d=d)]
        langs = get_required_languages(ships)
        assert langs == []

    def test_deduplication(self):
        d = date(2026, 7, 1)
        ships = [
            _ship("Ship A", d, extra_language="German"),
            _ship("Ship B", d, extra_language="german"),
        ]
        langs = get_required_languages(ships)
        assert langs == ["german"]

    def test_multiple_languages_sorted(self):
        d = date(2026, 7, 1)
        ships = [
            _ship("Ship A", d, extra_language="Spanish"),
            _ship("Ship B", d, extra_language="German"),
        ]
        langs = get_required_languages(ships)
        assert langs == ["german", "spanish"]

    def test_no_ships_returns_empty(self):
        assert get_required_languages([]) == []

    def test_no_extra_language_returns_empty(self):
        d = date(2026, 7, 1)
        ships = [_ship("Unknown Ship", d)]
        langs = get_required_languages(ships)
        assert langs == []


class TestCheckLanguageCoverage:
    def test_covered(self):
        emps = [_employee(languages=["english", "german"])]
        result = check_language_coverage(["german"], emps)
        assert result == {"german": True}

    def test_not_covered(self):
        emps = [_employee(languages=["english"])]
        result = check_language_coverage(["spanish"], emps)
        assert result == {"spanish": False}

    def test_multiple_languages_mixed(self):
        emps = [
            _employee("Alice", ["english", "german"]),
            _employee("Bob", ["english", "spanish"]),
        ]
        result = check_language_coverage(["german", "spanish", "italian"], emps)
        assert result == {"german": True, "spanish": True, "italian": False}

    def test_no_required_languages(self):
        emps = [_employee()]
        result = check_language_coverage([], emps)
        assert result == {}

    def test_language_matching_is_case_insensitive(self):
        emps = [_employee(languages=["English", "German"])]
        result = check_language_coverage(["german"], emps)
        assert result == {"german": True}
