"""Hard constraint definitions — Phase 3."""
from datetime import date, timedelta

from ortools.sat.python import cp_model

from src.demand.forecaster import DailyDemand
from src.models.employee import EmployeeRead
from src.models.shift_template import ShiftTemplateRead
from src.models.enums import ShiftRole

# Type alias for the variables dictionary
Variables = dict[tuple, cp_model.IntVar]  # (employee_id, date, shift_id) -> BoolVar


def _time_to_minutes(t) -> int:
    return t.hour * 60 + t.minute


def _shift_duration_minutes(shift: ShiftTemplateRead) -> int:
    return _time_to_minutes(shift.end_time) - _time_to_minutes(shift.start_time)


def add_one_shift_per_day(
    model: cp_model.CpModel,
    variables: Variables,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
) -> None:
    """Each employee works at most one shift per day."""
    for emp in employees:
        for d in days:
            day_vars = [
                variables[(emp.id, d, s.id)]
                for s in shifts
                if (emp.id, d, s.id) in variables
            ]
            if len(day_vars) > 1:
                model.Add(sum(day_vars) <= 1)


def add_daily_staffing_requirements(
    model: cp_model.CpModel,
    variables: Variables,
    demand_map: dict[date, DailyDemand],
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
) -> None:
    """For each day, ensure enough staff per role matches demand."""
    cafe_shifts = [s for s in shifts if s.role == ShiftRole.cafe]
    prod_shifts = [s for s in shifts if s.role == ShiftRole.production]

    for d in days:
        if d not in demand_map:
            continue
        demand = demand_map[d]

        # Café staffing: count all employees assigned to any café shift
        cafe_vars = [
            variables[(emp.id, d, s.id)]
            for emp in employees
            for s in cafe_shifts
            if (emp.id, d, s.id) in variables
        ]
        if cafe_vars and demand.cafe_needed > 0:
            model.Add(sum(cafe_vars) >= demand.cafe_needed)

        # Production staffing
        prod_vars = [
            variables[(emp.id, d, s.id)]
            for emp in employees
            for s in prod_shifts
            if (emp.id, d, s.id) in variables
        ]
        if prod_vars and demand.production_needed > 0:
            model.Add(sum(prod_vars) >= demand.production_needed)


def add_weekly_hour_limits(
    model: cp_model.CpModel,
    variables: Variables,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
) -> None:
    """For each employee, for each calendar week: total shift hours ≤ 48h (Norwegian law)."""
    shift_durations = {s.id: _shift_duration_minutes(s) for s in shifts}
    max_weekly_minutes = 48 * 60

    # Group days by ISO (year, week) — avoids crossing month boundaries incorrectly
    days_by_week: dict[tuple, list[date]] = {}
    for d in days:
        key = d.isocalendar()[:2]  # (iso_year, iso_week)
        days_by_week.setdefault(key, []).append(d)

    for emp in employees:
        for week_key, week_days in days_by_week.items():
            week_vars = []
            week_coeffs = []
            for d in week_days:
                for s in shifts:
                    if (emp.id, d, s.id) in variables:
                        week_vars.append(variables[(emp.id, d, s.id)])
                        week_coeffs.append(shift_durations[s.id])
            if week_vars:
                model.Add(
                    cp_model.LinearExpr.WeightedSum(week_vars, week_coeffs) <= max_weekly_minutes
                )


def add_daily_rest(
    model: cp_model.CpModel,
    variables: Variables,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
) -> None:
    """Min 11h between end of one shift and start of next (consecutive days).

    For each (s1 on day d, s2 on day d+1): if rest < 11h, forbid both being assigned.
    """
    min_rest_minutes = 11 * 60
    day_set = set(days)

    for emp in employees:
        for d in days:
            d_next = d + timedelta(days=1)
            if d_next not in day_set:
                continue
            for s1 in shifts:
                if (emp.id, d, s1.id) not in variables:
                    continue
                s1_end = _time_to_minutes(s1.end_time)
                for s2 in shifts:
                    if (emp.id, d_next, s2.id) not in variables:
                        continue
                    s2_start = _time_to_minutes(s2.start_time)
                    # Minutes from end of s1 to start of s2 on the next day
                    rest = (24 * 60 - s1_end) + s2_start
                    if rest < min_rest_minutes:
                        model.Add(
                            variables[(emp.id, d, s1.id)]
                            + variables[(emp.id, d_next, s2.id)]
                            <= 1
                        )


def add_weekly_rest(
    model: cp_model.CpModel,
    variables: Variables,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
) -> None:
    """At least 1 full day off per 7-day rolling window (satisfies 35h continuous rest)."""
    days_sorted = sorted(days)
    n = len(days_sorted)

    for emp in employees:
        for i in range(n - 6):
            window = days_sorted[i : i + 7]
            window_vars = [
                variables[(emp.id, d, s.id)]
                for d in window
                for s in shifts
                if (emp.id, d, s.id) in variables
            ]
            if window_vars:
                model.Add(sum(window_vars) <= 6)


def add_language_requirements(
    model: cp_model.CpModel,
    variables: Variables,
    demand_map: dict[date, DailyDemand],
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
) -> None:
    """For each day with required languages: at least 1 café shift must have a speaker."""
    cafe_shifts = [s for s in shifts if s.role == ShiftRole.cafe]

    for d in days:
        if d not in demand_map:
            continue
        demand = demand_map[d]

        for lang in demand.languages_required:
            # Find employees who speak this language
            speakers = [
                emp for emp in employees
                if any(l.lower().strip() == lang.lower() for l in emp.languages)
            ]
            if not speakers:
                continue  # No speaker available — infeasible, but handled externally

            lang_vars = [
                variables[(emp.id, d, s.id)]
                for emp in speakers
                for s in cafe_shifts
                if (emp.id, d, s.id) in variables
            ]
            if lang_vars:
                model.Add(sum(lang_vars) >= 1)


def add_role_capability(
    model: cp_model.CpModel,
    variables: Variables,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
) -> None:
    """Role capability is enforced at variable-creation time (no incompatible vars created).

    This function serves as documentation of that invariant.
    """
    pass  # Handled in ScheduleGenerator._create_variables


def add_availability(
    model: cp_model.CpModel,
    variables: Variables,
    employees: list[EmployeeRead],
    days: list[date],
) -> None:
    """Availability is enforced at variable-creation time (no out-of-range vars created).

    This function serves as documentation of that invariant.
    """
    pass  # Handled in ScheduleGenerator._create_variables
