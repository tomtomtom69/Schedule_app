"""Soft constraint weights and objective function — Phase 3."""
from datetime import date

from ortools.sat.python import cp_model

from src.demand.forecaster import DailyDemand
from src.models.employee import EmployeeRead
from src.models.shift_template import ShiftTemplateRead
from src.models.enums import EmploymentType, Housing, ShiftRole

Variables = dict[tuple, cp_model.IntVar]  # (employee_id, date, shift_id) -> BoolVar

WEIGHTS = {
    "language_coverage": 100,   # highest priority soft constraint
    "full_time_preference": 10,
    "eidsdal_grouping": 8,
    "employee_preferences": 5,
    "fair_distribution": 5,
    "minimize_overtime": 3,
    "shift_variety": 2,         # small penalty for same shift on consecutive days
}

_MAX_WEEKLY_MINUTES = 48 * 60
_NORMAL_WEEKLY_MINUTES = 40 * 60


def _time_to_minutes(t) -> int:
    return t.hour * 60 + t.minute


def _shift_duration_minutes(shift: ShiftTemplateRead) -> int:
    return _time_to_minutes(shift.end_time) - _time_to_minutes(shift.start_time)


def _days_by_week(days: list[date]) -> dict[tuple, list[date]]:
    result: dict[tuple, list[date]] = {}
    for d in days:
        key = d.isocalendar()[:2]
        result.setdefault(key, []).append(d)
    return result


def add_soft_constraints(
    model: cp_model.CpModel,
    variables: Variables,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
    demand_map: dict[date, DailyDemand] | None = None,
) -> None:
    """Add all soft constraints and set the model's Maximize objective."""
    obj_vars: list[cp_model.IntVar] = []
    obj_coeffs: list[int] = []

    # Language coverage is high-priority soft (was previously a hard constraint that
    # caused infeasibility when no speaker was available for a given ship day).
    if demand_map:
        prefer_language_coverage(model, variables, employees, shifts, days, demand_map, obj_vars, obj_coeffs)

    prefer_full_time(model, variables, employees, shifts, days, obj_vars, obj_coeffs)
    group_eidsdal_shifts(model, variables, employees, shifts, days, obj_vars, obj_coeffs)
    respect_preferences(model, variables, employees, shifts, days, obj_vars, obj_coeffs)
    minimize_overtime(model, variables, employees, shifts, days, obj_vars, obj_coeffs)
    distribute_hours_fairly(model, variables, employees, shifts, days, obj_vars, obj_coeffs)
    penalize_same_shift_consecutive(model, variables, employees, shifts, days, obj_vars, obj_coeffs)

    if obj_vars:
        model.Maximize(cp_model.LinearExpr.WeightedSum(obj_vars, obj_coeffs))


def prefer_language_coverage(
    model: cp_model.CpModel,
    variables: Variables,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
    demand_map: dict[date, DailyDemand],
    obj_vars: list,
    obj_coeffs: list,
) -> None:
    """Soft language coverage: reward having a speaker on café when a ship language is required.

    For each (day, required_language): if at least one speaker is on a café shift,
    a binary reward var is 1 (gaining weight LANGUAGE_COVERAGE). If no speaker is
    available at all for a given language/day, the constraint is simply skipped so the
    model stays feasible.
    """
    w = WEIGHTS["language_coverage"]
    cafe_shifts = [s for s in shifts if s.role == ShiftRole.cafe]

    for d in days:
        dd = demand_map.get(d)
        if not dd or not dd.languages_required:
            continue
        for lang in dd.languages_required:
            speakers = [
                emp for emp in employees
                if any(l.lower().strip() == lang.lower() for l in emp.languages)
            ]
            lang_vars = [
                variables[(emp.id, d, s.id)]
                for emp in speakers
                for s in cafe_shifts
                if (emp.id, d, s.id) in variables
            ]
            if not lang_vars:
                continue  # no speaker available — skip silently (preflight warns the user)

            # covered = 1 if at least one speaker is scheduled on a café shift
            covered = model.NewBoolVar(f"lang_cov_{lang}_{d}")
            model.Add(sum(lang_vars) >= 1).OnlyEnforceIf(covered)
            model.Add(sum(lang_vars) == 0).OnlyEnforceIf(covered.Not())
            obj_vars.append(covered)
            obj_coeffs.append(w)


def prefer_full_time(
    model: cp_model.CpModel,
    variables: Variables,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
    obj_vars: list,
    obj_coeffs: list,
) -> None:
    """Reward full-time assignments; penalise part-time assignments.

    Full-time assignment: +WEIGHT
    Part-time assignment: -WEIGHT
    """
    w = WEIGHTS["full_time_preference"]
    for emp in employees:
        coeff = w if emp.employment_type == EmploymentType.full_time else -w
        for d in days:
            for s in shifts:
                if (emp.id, d, s.id) in variables:
                    obj_vars.append(variables[(emp.id, d, s.id)])
                    obj_coeffs.append(coeff)


def group_eidsdal_shifts(
    model: cp_model.CpModel,
    variables: Variables,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
    obj_vars: list,
    obj_coeffs: list,
) -> None:
    """Reward Eidsdal employees being assigned to the same shift.

    For each (day, shift): count of Eidsdal workers on that shift is added to
    the objective. Higher count = higher reward, incentivising clustering.
    """
    eidsdal = [emp for emp in employees if emp.housing == Housing.eidsdal]
    if len(eidsdal) < 2:
        return

    w = WEIGHTS["eidsdal_grouping"]
    for d in days:
        for s in shifts:
            eidsdal_shift_vars = [
                variables[(emp.id, d, s.id)]
                for emp in eidsdal
                if (emp.id, d, s.id) in variables
            ]
            if len(eidsdal_shift_vars) >= 2:
                # Create n_grouped IntVar = how many Eidsdal workers are on this shift
                n_grouped = model.NewIntVar(
                    0, len(eidsdal_shift_vars), f"eidsdal_grp_{d}_{s.id}"
                )
                model.Add(n_grouped == sum(eidsdal_shift_vars))
                obj_vars.append(n_grouped)
                obj_coeffs.append(w)


def respect_preferences(
    model: cp_model.CpModel,
    variables: Variables,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
    obj_vars: list,
    obj_coeffs: list,
) -> None:
    """Penalise assignments that violate employee preferences.

    Supported preference keys (in employee.preferences dict):
    - no_monday / no_tuesday / ... / no_sunday: bool — penalise work on that weekday
    - preferred_off: list[str] — ISO date strings of preferred days off
    """
    _WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    w = WEIGHTS["employee_preferences"]

    for emp in employees:
        prefs = emp.preferences or {}

        # Penalise preferred-off weekdays
        for day_name in _WEEKDAYS:
            if prefs.get(f"no_{day_name}"):
                weekday_num = _WEEKDAYS.index(day_name)
                for d in days:
                    if d.weekday() == weekday_num:
                        for s in shifts:
                            if (emp.id, d, s.id) in variables:
                                obj_vars.append(variables[(emp.id, d, s.id)])
                                obj_coeffs.append(-w)

        # Penalise specific preferred-off dates
        for iso_str in prefs.get("preferred_off", []):
            try:
                from datetime import date as date_type
                off_date = date_type.fromisoformat(iso_str)
            except (ValueError, TypeError):
                continue
            for s in shifts:
                if (emp.id, off_date, s.id) in variables:
                    obj_vars.append(variables[(emp.id, off_date, s.id)])
                    obj_coeffs.append(-w)


def minimize_overtime(
    model: cp_model.CpModel,
    variables: Variables,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
    obj_vars: list,
    obj_coeffs: list,
) -> None:
    """Penalise weekly hours > 40h for any employee (prefer normal schedule over overtime)."""
    shift_dur = {s.id: _shift_duration_minutes(s) for s in shifts}
    w = WEIGHTS["minimize_overtime"]

    for emp in employees:
        for week_key, week_days in _days_by_week(days).items():
            wk_vars = []
            wk_coeffs = []
            for d in week_days:
                for s in shifts:
                    if (emp.id, d, s.id) in variables:
                        wk_vars.append(variables[(emp.id, d, s.id)])
                        wk_coeffs.append(shift_dur[s.id])
            if not wk_vars:
                continue

            suffix = f"{emp.id}_{week_key[0]}_{week_key[1]}"
            weekly_minutes = model.NewIntVar(0, _MAX_WEEKLY_MINUTES, f"wkm_{suffix}")
            model.Add(
                weekly_minutes
                == cp_model.LinearExpr.WeightedSum(wk_vars, wk_coeffs)
            )

            overtime = model.NewIntVar(0, (_MAX_WEEKLY_MINUTES - _NORMAL_WEEKLY_MINUTES), f"ot_{suffix}")
            model.Add(overtime >= weekly_minutes - _NORMAL_WEEKLY_MINUTES)
            model.Add(overtime >= 0)

            obj_vars.append(overtime)
            obj_coeffs.append(-w)


def distribute_hours_fairly(
    model: cp_model.CpModel,
    variables: Variables,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
    obj_vars: list,
    obj_coeffs: list,
) -> None:
    """Minimise the spread between the most- and least-worked employee.

    Creates total_hours IntVar per employee, then penalises (max - min) spread.
    """
    if len(employees) < 2:
        return

    shift_dur = {s.id: _shift_duration_minutes(s) for s in shifts}
    w = WEIGHTS["fair_distribution"]
    max_total_minutes = len(days) * max(shift_dur.values(), default=0)
    if max_total_minutes == 0:
        return

    emp_total_vars: list[cp_model.IntVar] = []
    for emp in employees:
        emp_vars = []
        emp_coeffs = []
        for d in days:
            for s in shifts:
                if (emp.id, d, s.id) in variables:
                    emp_vars.append(variables[(emp.id, d, s.id)])
                    emp_coeffs.append(shift_dur[s.id])
        if emp_vars:
            total = model.NewIntVar(0, max_total_minutes, f"total_h_{emp.id}")
            model.Add(total == cp_model.LinearExpr.WeightedSum(emp_vars, emp_coeffs))
            emp_total_vars.append(total)

    if len(emp_total_vars) < 2:
        return

    max_h = model.NewIntVar(0, max_total_minutes, "max_hours")
    min_h = model.NewIntVar(0, max_total_minutes, "min_hours")
    model.AddMaxEquality(max_h, emp_total_vars)
    model.AddMinEquality(min_h, emp_total_vars)

    spread = model.NewIntVar(0, max_total_minutes, "hours_spread")
    model.Add(spread == max_h - min_h)

    obj_vars.append(spread)
    obj_coeffs.append(-w)


def penalize_same_shift_consecutive(
    model: cp_model.CpModel,
    variables: Variables,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
    obj_vars: list,
    obj_coeffs: list,
) -> None:
    """Penalise an employee being on the exact same shift on two consecutive days.

    This discourages the solver from taking the lazy path of assigning everyone
    to the same shift (e.g. shift 5) every day.
    """
    w = WEIGHTS["shift_variety"]
    sorted_days = sorted(days)

    for emp in employees:
        for i in range(len(sorted_days) - 1):
            d1, d2 = sorted_days[i], sorted_days[i + 1]
            if (d2 - d1).days != 1:
                continue  # Not consecutive calendar days — skip
            for s in shifts:
                v1 = variables.get((emp.id, d1, s.id))
                v2 = variables.get((emp.id, d2, s.id))
                if v1 is None or v2 is None:
                    continue
                # both_same = 1 iff employee works the identical shift on both days
                both_same = model.NewBoolVar(f"sameshift_{emp.id}_{d1}_{s.id}")
                model.AddBoolAnd([v1, v2]).OnlyEnforceIf(both_same)
                model.AddBoolOr([v1.Not(), v2.Not()]).OnlyEnforceIf(both_same.Not())
                obj_vars.append(both_same)
                obj_coeffs.append(-w)
