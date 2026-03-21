"""Eidsdal carpooling logic — Phase 3."""
from datetime import date

from ortools.sat.python import cp_model

from src.models.employee import EmployeeRead
from src.models.shift_template import ShiftTemplateRead
from src.models.enums import Housing

Variables = dict[tuple, cp_model.IntVar]  # (employee_id, date, shift_id) -> BoolVar

EIDSDAL_CARS = 2
SEATS_PER_CAR = 5
MAX_EIDSDAL_WORKERS = EIDSDAL_CARS * SEATS_PER_CAR  # 10


def _eidsdal_employees(employees: list[EmployeeRead]) -> list[EmployeeRead]:
    return [emp for emp in employees if emp.housing == Housing.eidsdal]


def _eidsdal_drivers(employees: list[EmployeeRead]) -> list[EmployeeRead]:
    return [emp for emp in _eidsdal_employees(employees) if emp.driving_licence]


def add_eidsdal_transport_constraints(
    model: cp_model.CpModel,
    variables: Variables,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
) -> None:
    """Add both Eidsdal transport constraints: capacity cap and driver requirement."""
    eidsdal = _eidsdal_employees(employees)
    if not eidsdal:
        return

    _add_capacity_constraint(model, variables, eidsdal, shifts, days)
    add_driver_requirement(model, variables, employees, shifts, days)


def _add_capacity_constraint(
    model: cp_model.CpModel,
    variables: Variables,
    eidsdal_employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
) -> None:
    """At most MAX_EIDSDAL_WORKERS (10) Eidsdal employees may work on any single day."""
    for d in days:
        day_vars = [
            variables[(emp.id, d, s.id)]
            for emp in eidsdal_employees
            for s in shifts
            if (emp.id, d, s.id) in variables
        ]
        if day_vars:
            model.Add(sum(day_vars) <= MAX_EIDSDAL_WORKERS)


def add_driver_requirement(
    model: cp_model.CpModel,
    variables: Variables,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
) -> None:
    """Ensure enough licensed drivers among Eidsdal workers each day.

    - 1-5 Eidsdal workers: ≥ 1 driver required
    - 6-10 Eidsdal workers: ≥ 2 drivers required

    Implemented with auxiliary BoolVars:
    - eidsdal_any: True if ≥1 Eidsdal worker is scheduled
    - car2_needed: True if ≥6 Eidsdal workers are scheduled
    """
    eidsdal = _eidsdal_employees(employees)
    drivers = _eidsdal_drivers(employees)
    if not eidsdal:
        return

    for di, d in enumerate(days):
        eidsdal_vars = [
            variables[(emp.id, d, s.id)]
            for emp in eidsdal
            for s in shifts
            if (emp.id, d, s.id) in variables
        ]
        driver_vars = [
            variables[(emp.id, d, s.id)]
            for emp in drivers
            for s in shifts
            if (emp.id, d, s.id) in variables
        ]
        if not eidsdal_vars:
            continue

        # ── Car 1: ≥1 driver if any Eidsdal worker works ────────────────────
        eidsdal_any = model.NewBoolVar(f"eidsdal_any_d{di}")
        # eidsdal_any = 1 iff sum(eidsdal_vars) >= 1
        model.Add(sum(eidsdal_vars) >= 1).OnlyEnforceIf(eidsdal_any)
        model.Add(sum(eidsdal_vars) == 0).OnlyEnforceIf(eidsdal_any.Not())

        if driver_vars:
            model.Add(sum(driver_vars) >= 1).OnlyEnforceIf(eidsdal_any)

        # ── Car 2: ≥2 drivers if >5 Eidsdal workers work ───────────────────
        if len(eidsdal_vars) > 5 and len(driver_vars) >= 2:
            car2_needed = model.NewBoolVar(f"car2_needed_d{di}")
            model.Add(sum(eidsdal_vars) >= 6).OnlyEnforceIf(car2_needed)
            model.Add(sum(eidsdal_vars) <= 5).OnlyEnforceIf(car2_needed.Not())
            model.Add(sum(driver_vars) >= 2).OnlyEnforceIf(car2_needed)
