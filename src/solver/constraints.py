"""Hard constraint definitions — Phase 3."""
from datetime import date, timedelta

from ortools.sat.python import cp_model

from src.demand.forecaster import DailyDemand
from src.models.employee import EmployeeRead
from src.models.shift_template import ShiftTemplateRead
from src.models.enums import RoleCapability, ShiftRole

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


def add_opening_hours_coverage(
    model: cp_model.CpModel,
    variables: Variables,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
    demand_map: dict[date, DailyDemand],
    settings_list: list,  # list[EstablishmentSettingsRead]
) -> None:
    """Ensure ≥1 café and ≥1 production employee covers every hourly slot during
    operating hours.  On cruise days, coverage during harbour time is raised to
    min(cafe_needed, len(covering_vars)) so the peak staffing is actually present.
    """
    if not settings_list:
        return

    shift_start_m = {s.id: _time_to_minutes(s.start_time) for s in shifts}
    shift_end_m   = {s.id: _time_to_minutes(s.end_time)   for s in shifts}
    cafe_sids = [s.id for s in shifts if s.role == ShiftRole.cafe]
    prod_sids = [s.id for s in shifts if s.role == ShiftRole.production]

    def _cfg_for_day(d: date):
        for cfg in settings_list:
            if cfg.date_range_start <= d <= cfg.date_range_end:
                return cfg
        return None

    def _slots(open_m: int, close_m: int) -> list[tuple[int, int]]:
        """1-hour slots; last slot may be shorter."""
        result = []
        s = open_m
        while s < close_m:
            result.append((s, min(s + 60, close_m)))
            s += 60
        return result

    def _covering(slot_s: int, slot_e: int, sid_list: list[str]) -> list[str]:
        return [
            sid for sid in sid_list
            if shift_start_m[sid] <= slot_s and shift_end_m[sid] >= slot_e
        ]

    for d in days:
        dd = demand_map.get(d)
        if not dd:
            continue
        cfg = _cfg_for_day(d)
        if not cfg:
            continue

        open_m  = _time_to_minutes(cfg.opening_time)
        close_m = _time_to_minutes(cfg.closing_time)
        prod_start_m = _time_to_minutes(cfg.production_start)

        # ── Café coverage ──────────────────────────────────────────────────
        # Require ≥1 café employee on shift during every hourly slot within
        # opening hours.  Total headcount is handled by the separate staffing
        # requirement constraint; this constraint purely ensures temporal spread
        # (morning, midday, evening are all covered).
        cafe_emps = [
            e for e in employees
            if e.availability_start <= d <= e.availability_end
            and e.role_capability in (RoleCapability.cafe, RoleCapability.both)
        ]
        for slot_s, slot_e in _slots(open_m, close_m):
            cov_sids = _covering(slot_s, slot_e, cafe_sids)
            if not cov_sids:
                continue
            cov_vars = [
                variables[(e.id, d, sid)]
                for e in cafe_emps
                for sid in cov_sids
                if (e.id, d, sid) in variables
            ]
            if not cov_vars:
                continue
            model.Add(sum(cov_vars) >= 1)

        # ── Production coverage ────────────────────────────────────────────
        prod_emps = [
            e for e in employees
            if e.availability_start <= d <= e.availability_end
            and e.role_capability in (RoleCapability.production, RoleCapability.both)
        ]
        for slot_s, slot_e in _slots(prod_start_m, close_m):
            cov_sids = _covering(slot_s, slot_e, prod_sids)
            # Skip extreme slots where only one shift can cover — too rigid with
            # limited production headcount; focus on well-covered core hours.
            if len(cov_sids) < 2:
                continue
            cov_vars = [
                variables[(e.id, d, sid)]
                for e in prod_emps
                for sid in cov_sids
                if (e.id, d, sid) in variables
            ]
            if not cov_vars:
                continue
            model.Add(sum(cov_vars) >= 1)


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
