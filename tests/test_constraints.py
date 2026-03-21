"""Unit tests for constraint functions — Phase 3.

Tests add constraints to a CP-SAT model and verify they produce the
expected solution or infeasibility without running the full solver pipeline.
"""
import uuid
from datetime import date, time, timedelta

import pytest
from ortools.sat.python import cp_model

from src.models.employee import EmployeeRead
from src.models.enums import EmploymentType, Housing, RoleCapability, ShiftRole
from src.models.shift_template import ShiftTemplateRead
from src.solver.constraints import (
    Variables,
    add_daily_rest,
    add_daily_staffing_requirements,
    add_one_shift_per_day,
    add_weekly_hour_limits,
    add_weekly_rest,
    add_language_requirements,
)
from src.solver.transport import (
    MAX_EIDSDAL_WORKERS,
    add_driver_requirement,
    add_eidsdal_transport_constraints,
)
from src.solver.validator import Violation, validate_schedule
from src.demand.forecaster import DailyDemand
from src.models.schedule import AssignmentRead, ScheduleRead

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_employee(
    role: RoleCapability = RoleCapability.cafe,
    housing: Housing = Housing.geiranger,
    driving_licence: bool = False,
    languages: list[str] | None = None,
    name: str = "Test",
) -> EmployeeRead:
    return EmployeeRead(
        id=uuid.uuid4(),
        name=name,
        languages=languages or ["english"],
        role_capability=role,
        employment_type=EmploymentType.full_time,
        contracted_hours=37.5,
        housing=housing,
        driving_licence=driving_licence,
        availability_start=date(2026, 5, 1),
        availability_end=date(2026, 10, 15),
    )


def _make_shift(
    shift_id: str, role: ShiftRole = ShiftRole.cafe,
    start: time = time(8, 0), end: time = time(16, 0),
) -> ShiftTemplateRead:
    return ShiftTemplateRead(
        id=shift_id, role=role,
        label=f"Shift {shift_id}",
        start_time=start, end_time=end,
    )


def _make_demand(
    d: date,
    cafe_needed: int = 1,
    production_needed: int = 1,
    languages_required: list[str] | None = None,
) -> DailyDemand:
    return DailyDemand(
        date=d, season="peak", day_of_week=d.strftime("%A"),
        has_cruise=False, has_good_ship=False,
        geiranger_ship_count=0, hellesylt_ship_count=0,
        effective_ship_impact=0.0,
        production_needed=production_needed,
        cafe_needed=cafe_needed,
        languages_required=languages_required or [],
        ships_today=[],
    )


def _build_vars(
    model: cp_model.CpModel,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
) -> Variables:
    """Create one BoolVar per (employee, day, shift) triple."""
    variables: Variables = {}
    for emp in employees:
        for d in days:
            for s in shifts:
                var = model.NewBoolVar(f"x_{emp.id}_{d}_{s.id}")
                variables[(emp.id, d, s.id)] = var
    return variables


def _solve(model: cp_model.CpModel) -> cp_model.CpSolver | None:
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 5
    status = solver.Solve(model)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return solver
    return None


# ── add_one_shift_per_day ────────────────────────────────────────────────────


class TestOneShiftPerDay:
    def test_allows_one_shift(self):
        model = cp_model.CpModel()
        emp = _make_employee()
        s1 = _make_shift("1")
        s2 = _make_shift("2", start=time(9, 30), end=time(17, 30))
        d = date(2026, 7, 1)
        variables = _build_vars(model, [emp], [s1, s2], [d])
        add_one_shift_per_day(model, variables, [emp], [s1, s2], [d])
        # Force employee to work shift 1
        model.Add(variables[(emp.id, d, "1")] == 1)
        solver = _solve(model)
        assert solver is not None
        # Must not also work shift 2
        assert solver.Value(variables[(emp.id, d, "2")]) == 0

    def test_forbids_two_shifts_same_day(self):
        model = cp_model.CpModel()
        emp = _make_employee()
        s1 = _make_shift("1")
        s2 = _make_shift("2", start=time(9, 30), end=time(17, 30))
        d = date(2026, 7, 1)
        variables = _build_vars(model, [emp], [s1, s2], [d])
        add_one_shift_per_day(model, variables, [emp], [s1, s2], [d])
        # Force BOTH shifts → infeasible
        model.Add(variables[(emp.id, d, "1")] == 1)
        model.Add(variables[(emp.id, d, "2")] == 1)
        solver = _solve(model)
        assert solver is None


# ── add_daily_staffing_requirements ──────────────────────────────────────────


class TestDailyStaffingRequirements:
    def test_staffing_requirement_satisfied(self):
        model = cp_model.CpModel()
        c1 = _make_employee(RoleCapability.cafe, name="C1")
        c2 = _make_employee(RoleCapability.cafe, name="C2")
        s = _make_shift("1", ShiftRole.cafe)
        d = date(2026, 7, 1)
        variables = _build_vars(model, [c1, c2], [s], [d])
        demand_map = {d: _make_demand(d, cafe_needed=2, production_needed=0)}
        add_daily_staffing_requirements(model, variables, demand_map, [c1, c2], [s], [d])
        solver = _solve(model)
        assert solver is not None
        total = solver.Value(variables[(c1.id, d, "1")]) + solver.Value(variables[(c2.id, d, "1")])
        assert total >= 2

    def test_staffing_infeasible_when_too_few_employees(self):
        model = cp_model.CpModel()
        c1 = _make_employee(RoleCapability.cafe)
        s = _make_shift("1", ShiftRole.cafe)
        d = date(2026, 7, 1)
        variables = _build_vars(model, [c1], [s], [d])
        demand_map = {d: _make_demand(d, cafe_needed=2, production_needed=0)}
        add_daily_staffing_requirements(model, variables, demand_map, [c1], [s], [d])
        solver = _solve(model)
        assert solver is None


# ── add_weekly_hour_limits ────────────────────────────────────────────────────


class TestWeeklyHourLimits:
    def test_48h_limit_enforced(self):
        """6 × 8h shifts = 48h (allowed). 7 × 8h shifts = 56h (forbidden)."""
        model = cp_model.CpModel()
        emp = _make_employee()
        # 7 days in the same ISO week: Jul 6 (Mon) - Jul 12 (Sun)
        days = [date(2026, 7, 6 + i) for i in range(7)]
        shifts = [_make_shift(str(i + 1), start=time(8, 0), end=time(16, 0)) for i in range(1)]
        variables = _build_vars(model, [emp], shifts, days)
        add_one_shift_per_day(model, variables, [emp], shifts, days)
        add_weekly_hour_limits(model, variables, [emp], shifts, days)
        # Force all 7 days → 56h total → infeasible
        for d in days:
            model.Add(variables[(emp.id, d, shifts[0].id)] == 1)
        solver = _solve(model)
        assert solver is None

    def test_6_shifts_per_week_is_feasible(self):
        """6 × 8h = 48h exactly → feasible."""
        model = cp_model.CpModel()
        emp = _make_employee()
        days = [date(2026, 7, 6 + i) for i in range(6)]  # only 6 days
        shifts = [_make_shift("1", start=time(8, 0), end=time(16, 0))]
        variables = _build_vars(model, [emp], shifts, days)
        add_weekly_hour_limits(model, variables, [emp], shifts, days)
        for d in days:
            model.Add(variables[(emp.id, d, shifts[0].id)] == 1)
        solver = _solve(model)
        assert solver is not None


# ── add_daily_rest ────────────────────────────────────────────────────────────


class TestDailyRest:
    def test_short_rest_forbidden(self):
        """Shift ending at 22:00 + shift starting at 06:00 next day = 8h rest (< 11h) → forbidden."""
        model = cp_model.CpModel()
        emp = _make_employee()
        # Late shift: 14:00-22:00
        late = _make_shift("late", start=time(14, 0), end=time(22, 0))
        # Very early shift next day: 06:00-14:00 → 8h rest (22:00→06:00 = 8h)
        early = _make_shift("early", start=time(6, 0), end=time(14, 0))
        d1 = date(2026, 7, 1)
        d2 = date(2026, 7, 2)
        variables = _build_vars(model, [emp], [late, early], [d1, d2])
        add_daily_rest(model, variables, [emp], [late, early], [d1, d2])
        # Force late on d1 and early on d2 → should be infeasible (8h rest < 11h)
        model.Add(variables[(emp.id, d1, "late")] == 1)
        model.Add(variables[(emp.id, d2, "early")] == 1)
        solver = _solve(model)
        assert solver is None, "8h rest between consecutive shifts should be forbidden"

    def test_11h_rest_is_allowed(self):
        """Shift ending 21:00 + shift starting 08:00 next day = 11h rest → allowed."""
        model = cp_model.CpModel()
        emp = _make_employee()
        s1 = _make_shift("s1", start=time(13, 0), end=time(21, 0))
        s2 = _make_shift("s2", start=time(8, 0), end=time(16, 0))
        d1 = date(2026, 7, 1)
        d2 = date(2026, 7, 2)
        variables = _build_vars(model, [emp], [s1, s2], [d1, d2])
        add_daily_rest(model, variables, [emp], [s1, s2], [d1, d2])
        model.Add(variables[(emp.id, d1, "s1")] == 1)
        model.Add(variables[(emp.id, d2, "s2")] == 1)
        solver = _solve(model)
        assert solver is not None, "11h rest between consecutive shifts should be allowed"


# ── add_weekly_rest ───────────────────────────────────────────────────────────


class TestWeeklyRest:
    def test_7_consecutive_days_forbidden(self):
        """Working all 7 days in a window violates weekly rest."""
        model = cp_model.CpModel()
        emp = _make_employee()
        days = [date(2026, 7, 1 + i) for i in range(7)]
        shifts = [_make_shift("1")]
        variables = _build_vars(model, [emp], shifts, days)
        add_weekly_rest(model, variables, [emp], shifts, days)
        for d in days:
            model.Add(variables[(emp.id, d, "1")] == 1)
        solver = _solve(model)
        assert solver is None, "7 consecutive working days violates weekly rest"

    def test_6_days_then_off_is_allowed(self):
        """6 days on, 1 day off in a 7-day window is fine."""
        model = cp_model.CpModel()
        emp = _make_employee()
        days = [date(2026, 7, 1 + i) for i in range(7)]
        shifts = [_make_shift("1")]
        variables = _build_vars(model, [emp], shifts, days)
        add_weekly_rest(model, variables, [emp], shifts, days)
        for d in days[:-1]:  # first 6 days only
            model.Add(variables[(emp.id, d, "1")] == 1)
        model.Add(variables[(emp.id, days[-1], "1")] == 0)  # day 7 = off
        solver = _solve(model)
        assert solver is not None, "6 on + 1 off should satisfy weekly rest"


# ── Eidsdal transport ─────────────────────────────────────────────────────────


class TestEidsdalTransportConstraints:
    def test_capacity_cap_10(self):
        """11 Eidsdal employees wanting to work → capped at 10."""
        model = cp_model.CpModel()
        emps = [
            _make_employee(housing=Housing.eidsdal, driving_licence=(i < 3), name=f"E{i}")
            for i in range(11)
        ]
        shifts = [_make_shift("1")]
        days = [date(2026, 7, 1)]
        variables = _build_vars(model, emps, shifts, days)
        add_eidsdal_transport_constraints(model, variables, emps, shifts, days)
        # Force all 11 to work → infeasible (cap is 10)
        for emp in emps:
            model.Add(variables[(emp.id, days[0], "1")] == 1)
        solver = _solve(model)
        assert solver is None

    def test_10_eidsdal_is_feasible(self):
        """Exactly 10 Eidsdal workers is allowed (boundary)."""
        model = cp_model.CpModel()
        emps = [
            _make_employee(housing=Housing.eidsdal, driving_licence=(i < 2), name=f"E{i}")
            for i in range(10)
        ]
        shifts = [_make_shift("1")]
        days = [date(2026, 7, 1)]
        variables = _build_vars(model, emps, shifts, days)
        add_eidsdal_transport_constraints(model, variables, emps, shifts, days)
        for emp in emps:
            model.Add(variables[(emp.id, days[0], "1")] == 1)
        solver = _solve(model)
        assert solver is not None

    def test_driver_required(self):
        """1 Eidsdal worker with no driver available → infeasible."""
        model = cp_model.CpModel()
        emp = _make_employee(housing=Housing.eidsdal, driving_licence=False)
        shifts = [_make_shift("1")]
        days = [date(2026, 7, 1)]
        variables = _build_vars(model, [emp], shifts, days)
        add_driver_requirement(model, variables, [emp], shifts, days)
        model.Add(variables[(emp.id, days[0], "1")] == 1)
        solver = _solve(model)
        assert solver is None, "No licensed driver among working Eidsdal employees"

    def test_driver_present_satisfies_constraint(self):
        """1 Eidsdal worker who IS a driver → feasible."""
        model = cp_model.CpModel()
        emp = _make_employee(housing=Housing.eidsdal, driving_licence=True)
        shifts = [_make_shift("1")]
        days = [date(2026, 7, 1)]
        variables = _build_vars(model, [emp], shifts, days)
        add_driver_requirement(model, variables, [emp], shifts, days)
        model.Add(variables[(emp.id, days[0], "1")] == 1)
        solver = _solve(model)
        assert solver is not None


# ── Validator unit tests ──────────────────────────────────────────────────────


class TestValidatorUnits:
    def _base_schedule(
        self,
        assignments: list[AssignmentRead],
        month: int = 7,
        year: int = 2026,
    ) -> ScheduleRead:
        return ScheduleRead(
            id=uuid.uuid4(),
            month=month,
            year=year,
            status="draft",
            created_at=__import__("datetime").datetime.utcnow(),
            assignments=assignments,
        )

    def test_no_violations_for_clean_schedule(self):
        emp = _make_employee(RoleCapability.cafe)
        d = date(2026, 7, 1)
        demand = [_make_demand(d, cafe_needed=1, production_needed=0)]
        shifts = [_make_shift("1")]
        sched = self._base_schedule([
            AssignmentRead(
                id=uuid.uuid4(), schedule_id=uuid.uuid4(),
                employee_id=emp.id, date=d, shift_id="1", is_day_off=False,
            )
        ])
        violations = validate_schedule(sched, [emp], demand, shifts)
        errors = [v for v in violations if v.severity == "error"]
        assert errors == []

    def test_flags_two_shifts_same_day(self):
        emp = _make_employee(RoleCapability.cafe)
        d = date(2026, 7, 1)
        demand = [_make_demand(d, cafe_needed=1, production_needed=0)]
        shifts = [_make_shift("1"), _make_shift("2", start=time(9, 30), end=time(17, 30))]
        sched = self._base_schedule([
            AssignmentRead(
                id=uuid.uuid4(), schedule_id=uuid.uuid4(),
                employee_id=emp.id, date=d, shift_id="1", is_day_off=False,
            ),
            AssignmentRead(
                id=uuid.uuid4(), schedule_id=uuid.uuid4(),
                employee_id=emp.id, date=d, shift_id="2", is_day_off=False,
            ),
        ])
        violations = validate_schedule(sched, [emp], demand, shifts)
        assert any(v.constraint == "one_shift_per_day" for v in violations)

    def test_flags_availability_violation(self):
        emp = _make_employee(
            RoleCapability.cafe,
            availability_start=date(2026, 7, 5),
            availability_end=date(2026, 10, 15),
        )
        d = date(2026, 7, 1)  # before availability_start
        demand = [_make_demand(d, cafe_needed=1, production_needed=0)]
        shifts = [_make_shift("1")]
        sched = self._base_schedule([
            AssignmentRead(
                id=uuid.uuid4(), schedule_id=uuid.uuid4(),
                employee_id=emp.id, date=d, shift_id="1", is_day_off=False,
            )
        ])
        violations = validate_schedule(sched, [emp], demand, shifts)
        assert any(v.constraint == "availability" for v in violations)

    def test_flags_eidsdal_no_driver(self):
        emp = _make_employee(housing=Housing.eidsdal, driving_licence=False)
        d = date(2026, 7, 1)
        demand = [_make_demand(d, cafe_needed=1, production_needed=0)]
        shifts = [_make_shift("1")]
        sched = self._base_schedule([
            AssignmentRead(
                id=uuid.uuid4(), schedule_id=uuid.uuid4(),
                employee_id=emp.id, date=d, shift_id="1", is_day_off=False,
            )
        ])
        violations = validate_schedule(sched, [emp], demand, shifts)
        assert any(v.constraint == "eidsdal_driver" for v in violations)

    def test_flags_language_coverage_missing(self):
        emp = _make_employee(RoleCapability.cafe, languages=["english"])
        d = date(2026, 7, 1)
        demand = [_make_demand(d, cafe_needed=1, production_needed=0, languages_required=["spanish"])]
        shifts = [_make_shift("1", ShiftRole.cafe)]
        sched = self._base_schedule([
            AssignmentRead(
                id=uuid.uuid4(), schedule_id=uuid.uuid4(),
                employee_id=emp.id, date=d, shift_id="1", is_day_off=False,
            )
        ])
        violations = validate_schedule(sched, [emp], demand, shifts)
        assert any(v.constraint == "language_coverage" for v in violations)
