"""Integration tests for the schedule solver — Phase 3.

These tests run the actual OR-Tools CP-SAT solver with minimal datasets
to verify end-to-end correctness. No database required.
"""
import uuid
from datetime import date, time

import pytest

from src.demand.forecaster import DailyDemand
from src.models.employee import EmployeeRead
from src.models.enums import (
    EmploymentType,
    Housing,
    Port,
    RoleCapability,
    Season,
    ShipSize,
    ShiftRole,
)
from src.models.schedule import AssignmentRead, ScheduleRead
from src.models.shift_template import ShiftTemplateRead
from src.solver.scheduler import ScheduleGenerator
from src.solver.validator import Violation, validate_schedule

# ── Fixture builders ─────────────────────────────────────────────────────────


def _employee(
    name: str = "Alice",
    role_capability: RoleCapability = RoleCapability.cafe,
    employment_type: EmploymentType = EmploymentType.full_time,
    housing: Housing = Housing.geiranger,
    driving_licence: bool = False,
    languages: list[str] | None = None,
    availability_start: date = date(2026, 5, 1),
    availability_end: date = date(2026, 10, 15),
    preferences: dict | None = None,
) -> EmployeeRead:
    return EmployeeRead(
        id=uuid.uuid4(),
        name=name,
        languages=languages or ["english"],
        role_capability=role_capability,
        employment_type=employment_type,
        contracted_hours=37.5,
        housing=housing,
        driving_licence=driving_licence,
        availability_start=availability_start,
        availability_end=availability_end,
        preferences=preferences,
    )


def _shift(
    shift_id: str,
    role: ShiftRole = ShiftRole.cafe,
    start: time = time(8, 0),
    end: time = time(16, 0),
    label: str | None = None,
) -> ShiftTemplateRead:
    return ShiftTemplateRead(
        id=shift_id,
        role=role,
        label=label or f"Shift {shift_id}",
        start_time=start,
        end_time=end,
    )


def _demand(
    d: date,
    season: Season = Season.peak,
    production_needed: int = 1,
    cafe_needed: int = 2,
    languages_required: list[str] | None = None,
) -> DailyDemand:
    return DailyDemand(
        date=d,
        season=season,
        day_of_week=d.strftime("%A"),
        has_cruise=False,
        has_good_ship=False,
        geiranger_ship_count=0,
        hellesylt_ship_count=0,
        effective_ship_impact=0.0,
        production_needed=production_needed,
        cafe_needed=cafe_needed,
        languages_required=languages_required or [],
        ships_today=[],
    )


# Standard shift templates used in most tests
CAFE_SHIFTS = [
    _shift("1", ShiftRole.cafe, time(8, 0), time(16, 0)),
    _shift("2", ShiftRole.cafe, time(9, 30), time(17, 30)),
    _shift("3", ShiftRole.cafe, time(11, 0), time(19, 0)),
]
PROD_SHIFTS = [
    _shift("P1", ShiftRole.production, time(8, 0), time(16, 0)),
    _shift("P2", ShiftRole.production, time(9, 30), time(17, 30)),
]
ALL_SHIFTS = CAFE_SHIFTS + PROD_SHIFTS

# A 5-day test period in peak season
FIVE_DAYS = [date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 3),
             date(2026, 7, 4), date(2026, 7, 5)]


def _five_day_demand(cafe: int = 2, prod: int = 1) -> list[DailyDemand]:
    return [_demand(d, cafe_needed=cafe, production_needed=prod) for d in FIVE_DAYS]


# ── Basic feasibility ─────────────────────────────────────────────────────────


class TestBasicFeasibility:
    def test_simple_schedule_is_feasible(self):
        """3 café + 1 production employee, 5 days, demand=2 café + 1 prod → feasible."""
        employees = [
            _employee("Alice", RoleCapability.cafe),
            _employee("Bob", RoleCapability.cafe),
            _employee("Carol", RoleCapability.cafe),
            _employee("Dave", RoleCapability.production),
        ]
        demand = _five_day_demand(cafe=2, prod=1)
        gen = ScheduleGenerator(employees, demand, ALL_SHIFTS)
        gen.build_model()
        schedule = gen.solve()
        assert schedule is not None, "Expected a feasible schedule"

    def test_returns_schedule_read(self):
        """Solver output is a properly structured ScheduleRead."""
        employees = [
            _employee("Alice", RoleCapability.cafe),
            _employee("Bob", RoleCapability.cafe),
            _employee("Carol", RoleCapability.production),
        ]
        demand = _five_day_demand(cafe=2, prod=1)
        gen = ScheduleGenerator(employees, demand, ALL_SHIFTS)
        gen.build_model()
        schedule = gen.solve()
        assert schedule is not None
        assert isinstance(schedule, ScheduleRead)
        assert schedule.month == 7
        assert schedule.year == 2026

    def test_infeasible_returns_none(self):
        """No employees → infeasible → solver returns None."""
        demand = _five_day_demand(cafe=2, prod=1)
        gen = ScheduleGenerator([], demand, ALL_SHIFTS)
        gen.build_model()
        result = gen.solve()
        assert result is None

    def test_insufficient_employees_returns_none(self):
        """Only 1 café employee, demand=2 → infeasible."""
        employees = [_employee("Alice", RoleCapability.cafe)]
        demand = _five_day_demand(cafe=2, prod=1)
        gen = ScheduleGenerator(employees, demand, ALL_SHIFTS)
        gen.build_model()
        result = gen.solve()
        assert result is None


# ── One shift per day constraint ─────────────────────────────────────────────


class TestOneShiftPerDay:
    def test_no_employee_works_two_shifts_same_day(self):
        employees = [
            _employee("Alice", RoleCapability.cafe),
            _employee("Bob", RoleCapability.cafe),
            _employee("Carol", RoleCapability.cafe),
            _employee("Dave", RoleCapability.production),
        ]
        demand = _five_day_demand(cafe=2, prod=1)
        gen = ScheduleGenerator(employees, demand, ALL_SHIFTS)
        gen.build_model()
        schedule = gen.solve()
        assert schedule is not None
        # Group working assignments by (employee, date)
        by_emp_day: dict = {}
        for a in schedule.assignments:
            if not a.is_day_off:
                key = (str(a.employee_id), a.date)
                by_emp_day[key] = by_emp_day.get(key, 0) + 1
        assert all(count == 1 for count in by_emp_day.values()), \
            "Some employee works 2+ shifts on the same day"


# ── Daily staffing requirements ───────────────────────────────────────────────


class TestDailyStaffingRequirements:
    def test_cafe_staffing_met_each_day(self):
        employees = [
            _employee("C1", RoleCapability.cafe),
            _employee("C2", RoleCapability.cafe),
            _employee("C3", RoleCapability.cafe),
            _employee("P1", RoleCapability.production),
        ]
        demand = _five_day_demand(cafe=2, prod=1)
        gen = ScheduleGenerator(employees, demand, ALL_SHIFTS)
        gen.build_model()
        schedule = gen.solve()
        assert schedule is not None

        shift_lookup = {s.id: s for s in ALL_SHIFTS}
        by_date: dict = {}
        for a in schedule.assignments:
            if not a.is_day_off:
                by_date.setdefault(a.date, []).append(a)

        for d in FIVE_DAYS:
            cafe_count = sum(
                1 for a in by_date.get(d, [])
                if shift_lookup.get(a.shift_id) and
                   shift_lookup[a.shift_id].role == ShiftRole.cafe
            )
            assert cafe_count >= 2, f"Day {d}: only {cafe_count} café workers (need 2)"

    def test_production_staffing_met_each_day(self):
        employees = [
            _employee("C1", RoleCapability.cafe),
            _employee("C2", RoleCapability.cafe),
            _employee("C3", RoleCapability.cafe),
            _employee("P1", RoleCapability.production),
            _employee("P2", RoleCapability.production),
        ]
        demand = _five_day_demand(cafe=2, prod=1)
        gen = ScheduleGenerator(employees, demand, ALL_SHIFTS)
        gen.build_model()
        schedule = gen.solve()
        assert schedule is not None

        shift_lookup = {s.id: s for s in ALL_SHIFTS}
        by_date: dict = {}
        for a in schedule.assignments:
            if not a.is_day_off:
                by_date.setdefault(a.date, []).append(a)

        for d in FIVE_DAYS:
            prod_count = sum(
                1 for a in by_date.get(d, [])
                if shift_lookup.get(a.shift_id) and
                   shift_lookup[a.shift_id].role == ShiftRole.production
            )
            assert prod_count >= 1, f"Day {d}: no production worker assigned"


# ── Role capability ───────────────────────────────────────────────────────────


class TestRoleCapability:
    def test_cafe_employee_only_on_cafe_shifts(self):
        employees = [
            _employee("Cafe1", RoleCapability.cafe),
            _employee("Cafe2", RoleCapability.cafe),
            _employee("Cafe3", RoleCapability.cafe),
            _employee("Prod1", RoleCapability.production),
        ]
        demand = _five_day_demand(cafe=2, prod=1)
        gen = ScheduleGenerator(employees, demand, ALL_SHIFTS)
        gen.build_model()
        schedule = gen.solve()
        assert schedule is not None

        shift_lookup = {s.id: s for s in ALL_SHIFTS}
        emp_lookup = {str(emp.id): emp for emp in employees}
        for a in schedule.assignments:
            if a.is_day_off:
                continue
            emp = emp_lookup[str(a.employee_id)]
            shift = shift_lookup.get(a.shift_id)
            if shift and emp.role_capability == RoleCapability.cafe:
                assert shift.role == ShiftRole.cafe, \
                    f"Café employee {emp.name} assigned to production shift {a.shift_id}"
            if shift and emp.role_capability == RoleCapability.production:
                assert shift.role == ShiftRole.production, \
                    f"Production employee {emp.name} assigned to café shift {a.shift_id}"


# ── Language requirements ─────────────────────────────────────────────────────


class TestLanguageRequirements:
    def test_spanish_ship_requires_spanish_speaker_on_cafe(self):
        """If a day has Spanish language requirement, a Spanish speaker must be on café shift."""
        spanish_speaker = _employee("Maria", RoleCapability.cafe, languages=["english", "spanish"])
        no_spanish = _employee("Erik", RoleCapability.cafe, languages=["english"])
        no_spanish2 = _employee("Anna", RoleCapability.cafe, languages=["english"])
        prod = _employee("Prod", RoleCapability.production)

        d = date(2026, 7, 1)
        single_day_demand = [_demand(d, cafe_needed=2, production_needed=1, languages_required=["spanish"])]
        employees = [spanish_speaker, no_spanish, no_spanish2, prod]

        gen = ScheduleGenerator(employees, single_day_demand, ALL_SHIFTS)
        gen.build_model()
        schedule = gen.solve()
        assert schedule is not None

        shift_lookup = {s.id: s for s in ALL_SHIFTS}
        emp_lookup = {str(emp.id): emp for emp in employees}

        spanish_on_cafe = any(
            str(a.employee_id) == str(spanish_speaker.id) and
            not a.is_day_off and
            shift_lookup.get(a.shift_id) and
            shift_lookup[a.shift_id].role == ShiftRole.cafe
            for a in schedule.assignments if a.date == d
        )
        assert spanish_on_cafe, "Spanish speaker should be assigned to a café shift"


# ── Availability constraint ───────────────────────────────────────────────────


class TestAvailability:
    def test_employee_not_assigned_outside_availability(self):
        """Employee only available Jul 3-5: should not appear on Jul 1-2."""
        limited = _employee(
            "Limited", RoleCapability.cafe,
            availability_start=date(2026, 7, 3),
            availability_end=date(2026, 7, 5),
        )
        always_on = _employee("Always1", RoleCapability.cafe)
        always_on2 = _employee("Always2", RoleCapability.cafe)
        prod = _employee("Prod", RoleCapability.production)

        demand = _five_day_demand(cafe=2, prod=1)
        gen = ScheduleGenerator([limited, always_on, always_on2, prod], demand, ALL_SHIFTS)
        gen.build_model()
        schedule = gen.solve()
        assert schedule is not None

        limited_id = str(limited.id)
        for a in schedule.assignments:
            if str(a.employee_id) == limited_id and not a.is_day_off:
                assert a.date >= date(2026, 7, 3), \
                    f"Limited employee assigned on {a.date} (outside availability)"


# ── Weekly hour limits ────────────────────────────────────────────────────────


class TestWeeklyHourLimits:
    def test_no_employee_exceeds_48h_per_week(self):
        """7 days, 3 café employees: none should exceed 48h in the week."""
        employees = [
            _employee("C1", RoleCapability.cafe),
            _employee("C2", RoleCapability.cafe),
            _employee("C3", RoleCapability.cafe),
            _employee("P1", RoleCapability.production),
        ]
        # 7-day demand (one full week in peak, Jul 6-12 = Mon-Sun)
        week_days = [date(2026, 7, 6 + i) for i in range(7)]
        demand = [_demand(d, cafe_needed=2, production_needed=1) for d in week_days]
        gen = ScheduleGenerator(employees, demand, ALL_SHIFTS)
        gen.build_model()
        schedule = gen.solve()
        assert schedule is not None

        shift_lookup = {s.id: s for s in ALL_SHIFTS}
        by_emp: dict = {}
        for a in schedule.assignments:
            if not a.is_day_off:
                shift = shift_lookup.get(a.shift_id)
                if shift:
                    dur = (shift.end_time.hour * 60 + shift.end_time.minute) - \
                          (shift.start_time.hour * 60 + shift.start_time.minute)
                    by_emp[str(a.employee_id)] = by_emp.get(str(a.employee_id), 0) + dur

        for emp_id, total_minutes in by_emp.items():
            assert total_minutes <= 48 * 60, \
                f"Employee exceeded 48h weekly limit: {total_minutes // 60}h"


# ── Eidsdal transport ─────────────────────────────────────────────────────────


class TestEidsdalTransport:
    def test_max_10_eidsdal_workers_per_day(self):
        """12 Eidsdal employees: at most 10 may be scheduled on any day."""
        eidsdal = [
            _employee(f"E{i}", RoleCapability.cafe, housing=Housing.eidsdal,
                      driving_licence=(i < 3))  # 3 drivers
            for i in range(12)
        ]
        # Add non-Eidsdal production to meet demand
        prod = _employee("Prod", RoleCapability.production, housing=Housing.geiranger)
        all_emps = eidsdal + [prod]
        demand = [_demand(d, cafe_needed=3, production_needed=1) for d in FIVE_DAYS]

        gen = ScheduleGenerator(all_emps, demand, ALL_SHIFTS)
        gen.build_model()
        schedule = gen.solve()
        assert schedule is not None

        eidsdal_ids = {str(e.id) for e in eidsdal}
        by_date: dict = {}
        for a in schedule.assignments:
            if not a.is_day_off and str(a.employee_id) in eidsdal_ids:
                by_date[a.date] = by_date.get(a.date, 0) + 1

        for d, count in by_date.items():
            assert count <= 10, f"Date {d}: {count} Eidsdal workers (max 10)"

    def test_driver_required_when_eidsdal_works(self):
        """At least 1 licensed driver must work when any Eidsdal employee is scheduled."""
        driver = _employee("Driver", RoleCapability.cafe, housing=Housing.eidsdal,
                           driving_licence=True)
        non_driver = _employee("Rider", RoleCapability.cafe, housing=Housing.eidsdal,
                               driving_licence=False)
        extra_cafe = _employee("Extra", RoleCapability.cafe, housing=Housing.geiranger)
        prod = _employee("Prod", RoleCapability.production)
        employees = [driver, non_driver, extra_cafe, prod]

        demand = [_demand(d, cafe_needed=2, production_needed=1) for d in FIVE_DAYS]
        gen = ScheduleGenerator(employees, demand, ALL_SHIFTS)
        gen.build_model()
        schedule = gen.solve()
        assert schedule is not None

        eidsdal_ids = {str(driver.id), str(non_driver.id)}
        driver_id = str(driver.id)

        by_date: dict[date, dict] = {}
        for a in schedule.assignments:
            if a.is_day_off:
                continue
            if str(a.employee_id) in eidsdal_ids:
                entry = by_date.setdefault(a.date, {"any": False, "driver": False})
                entry["any"] = True
                if str(a.employee_id) == driver_id:
                    entry["driver"] = True

        for d, info in by_date.items():
            if info["any"]:
                assert info["driver"], \
                    f"Date {d}: Eidsdal workers scheduled but no driver present"


# ── Full validation pass ──────────────────────────────────────────────────────


class TestValidator:
    def _make_valid_schedule(self) -> tuple:
        employees = [
            _employee("C1", RoleCapability.cafe),
            _employee("C2", RoleCapability.cafe),
            _employee("C3", RoleCapability.cafe),
            _employee("P1", RoleCapability.production),
        ]
        demand = _five_day_demand(cafe=2, prod=1)
        gen = ScheduleGenerator(employees, demand, ALL_SHIFTS)
        gen.build_model()
        schedule = gen.solve()
        return schedule, employees, demand

    def test_valid_schedule_has_no_errors(self):
        schedule, employees, demand = self._make_valid_schedule()
        assert schedule is not None
        violations = validate_schedule(schedule, employees, demand, ALL_SHIFTS)
        errors = [v for v in violations if v.severity == "error"]
        assert errors == [], f"Expected no errors, got: {errors}"

    def test_validator_flags_role_violation(self):
        """Manually inject a role violation and check the validator catches it."""
        employees = [
            _employee("CafeOnly", RoleCapability.cafe),
            _employee("C2", RoleCapability.cafe),
            _employee("C3", RoleCapability.cafe),
            _employee("P1", RoleCapability.production),
        ]
        demand = _five_day_demand(cafe=2, prod=1)
        gen = ScheduleGenerator(employees, demand, ALL_SHIFTS)
        gen.build_model()
        schedule = gen.solve()
        assert schedule is not None

        # Inject a role violation: put café-only employee on a production shift
        cafe_only = employees[0]
        bad_assignment = AssignmentRead(
            id=uuid.uuid4(),
            schedule_id=schedule.id,
            employee_id=cafe_only.id,
            date=FIVE_DAYS[0],
            shift_id="P1",  # production shift for café employee!
            is_day_off=False,
        )
        modified = ScheduleRead(
            id=schedule.id,
            month=schedule.month,
            year=schedule.year,
            status=schedule.status,
            created_at=schedule.created_at,
            assignments=schedule.assignments + [bad_assignment],
        )
        violations = validate_schedule(modified, employees, demand, ALL_SHIFTS)
        role_errors = [v for v in violations if v.constraint == "role_capability"]
        assert len(role_errors) >= 1, "Validator should flag the role capability violation"

    def test_validator_flags_staffing_shortage(self):
        """Manual schedule with insufficient café staff triggers a staffing error."""
        emp = _employee("Solo", RoleCapability.cafe)
        prod = _employee("Prod", RoleCapability.production)
        d = date(2026, 7, 1)
        demand = [_demand(d, cafe_needed=2, production_needed=1)]

        # Only 1 café assignment on a day that needs 2
        sched = ScheduleRead(
            id=uuid.uuid4(),
            month=7,
            year=2026,
            status="draft",
            created_at=__import__("datetime").datetime.utcnow(),
            assignments=[
                AssignmentRead(
                    id=uuid.uuid4(),
                    schedule_id=uuid.uuid4(),
                    employee_id=emp.id,
                    date=d,
                    shift_id="1",
                    is_day_off=False,
                ),
                AssignmentRead(
                    id=uuid.uuid4(),
                    schedule_id=uuid.uuid4(),
                    employee_id=prod.id,
                    date=d,
                    shift_id="P1",
                    is_day_off=False,
                ),
            ],
        )
        violations = validate_schedule(sched, [emp, prod], demand, ALL_SHIFTS)
        staffing_errors = [v for v in violations if v.constraint == "daily_staffing"]
        assert any("Café" in v.message or "café" in v.message.lower() for v in staffing_errors)

    def test_validator_clean_report_returns_empty_errors(self):
        """A solver-produced schedule should have zero hard constraint violations."""
        employees = [
            _employee("C1", RoleCapability.cafe),
            _employee("C2", RoleCapability.cafe),
            _employee("C3", RoleCapability.cafe),
            _employee("P1", RoleCapability.production),
            _employee("P2", RoleCapability.production),
        ]
        demand = _five_day_demand(cafe=2, prod=1)
        gen = ScheduleGenerator(employees, demand, ALL_SHIFTS)
        gen.build_model()
        schedule = gen.solve()
        assert schedule is not None

        violations = validate_schedule(schedule, employees, demand, ALL_SHIFTS)
        errors = [v for v in violations if v.severity == "error"]
        assert errors == [], f"Hard violations in solver-produced schedule: {errors}"


# ── Demand changes affect output ──────────────────────────────────────────────


class TestDemandChangesAffectOutput:
    def test_higher_demand_requires_more_staff(self):
        """Increasing café demand from 2 to 3 should require more café assignments."""
        employees = [
            _employee("C1", RoleCapability.cafe),
            _employee("C2", RoleCapability.cafe),
            _employee("C3", RoleCapability.cafe),
            _employee("C4", RoleCapability.cafe),
            _employee("P1", RoleCapability.production),
        ]

        def count_cafe_on_day(cafe_needed: int, d: date) -> int:
            demand = [_demand(d, cafe_needed=cafe_needed, production_needed=1)]
            gen = ScheduleGenerator(employees, demand, ALL_SHIFTS)
            gen.build_model()
            sched = gen.solve()
            if sched is None:
                return 0
            shift_lookup = {s.id: s for s in ALL_SHIFTS}
            return sum(
                1 for a in sched.assignments
                if not a.is_day_off and a.date == d and
                   shift_lookup.get(a.shift_id) and
                   shift_lookup[a.shift_id].role == ShiftRole.cafe
            )

        d = date(2026, 7, 1)
        cafe_low = count_cafe_on_day(2, d)
        cafe_high = count_cafe_on_day(3, d)
        assert cafe_low >= 2
        assert cafe_high >= 3
