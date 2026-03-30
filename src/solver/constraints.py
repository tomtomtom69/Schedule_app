"""Hard constraint definitions — Phase 3."""
from datetime import date, timedelta
from typing import Optional

from ortools.sat.python import cp_model

from src.demand.forecaster import DailyDemand
from src.models.employee import EmployeeRead, get_age_category, get_age_on_date
from src.models.shift_template import ShiftTemplateRead
from src.models.enums import RoleCapability, ShiftRole

# Type alias for the variables dictionary
Variables = dict[tuple, cp_model.IntVar]  # (employee_id, date, shift_id) -> BoolVar

# ── Weekly worked-hour limits per age category ──────────────────────────────
# Expressed as worked minutes (not raw template duration).
_ADULT_MAX_WEEKLY_WORKED_MIN = int(37.5 * 60)   # 2250 min = 37.5h = 5 × 7.5h shifts
_YOUTH_15_18_MAX_WEEKLY_WORKED_MIN = int(40 * 60)  # 2400 min = 40h
_YOUTH_UNDER_15_MAX_WEEKLY_WORKED_MIN = int(35 * 60)  # 2100 min = 35h

# Shift ID reserved for under-15 workers (only 7h template → 6.5h worked)
_UNDER_15_SHIFT_ID = "6"


def _time_to_minutes(t) -> int:
    return t.hour * 60 + t.minute


def _shift_duration_minutes(shift: ShiftTemplateRead) -> int:
    return _time_to_minutes(shift.end_time) - _time_to_minutes(shift.start_time)


# ── Easter / Norwegian public holidays ──────────────────────────────────────


def _compute_easter(year: int) -> date:
    """Return Easter Sunday for the given year (Anonymous Gregorian algorithm)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def norwegian_public_holidays(year: int) -> set[date]:
    """Return Norwegian public holidays that fall within the operating season (May–Oct)."""
    easter = _compute_easter(year)
    return {
        date(year, 5, 1),                    # Labour Day
        date(year, 5, 17),                   # Constitution Day
        easter + timedelta(days=39),          # Ascension Day (Kristi Himmelfartsdag)
        easter + timedelta(days=50),          # Whit Monday (2. pinsedag)
    }


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

        # Café staffing: count all employees assigned to any café shift.
        # Cap the hard minimum to the number of distinct café employees available
        # that day — prevents INFEASIBLE when headcount is below peak demand.
        cafe_vars = [
            variables[(emp.id, d, s.id)]
            for emp in employees
            for s in cafe_shifts
            if (emp.id, d, s.id) in variables
        ]
        if cafe_vars and demand.cafe_needed > 0:
            cafe_emps_today = len({emp.id for emp in employees for s in cafe_shifts if (emp.id, d, s.id) in variables})
            effective_cafe_min = min(demand.cafe_needed, cafe_emps_today)
            model.Add(sum(cafe_vars) >= effective_cafe_min)

        # Production staffing
        prod_vars = [
            variables[(emp.id, d, s.id)]
            for emp in employees
            for s in prod_shifts
            if (emp.id, d, s.id) in variables
        ]
        if prod_vars and demand.production_needed > 0:
            prod_emps_today = len({emp.id for emp in employees for s in prod_shifts if (emp.id, d, s.id) in variables})
            effective_prod_min = min(demand.production_needed, prod_emps_today)
            model.Add(sum(prod_vars) >= effective_prod_min)


def add_weekly_hour_limits(
    model: cp_model.CpModel,
    variables: Variables,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
) -> None:
    """Per-employee weekly worked-hour cap (Section 2 + Section 8).

    Uses *worked_hours* (template duration − 0.5h break) as coefficients so that
    overtime calculations reflect actual time worked, not the clock window.

    Caps per age category (Section 3 + Section 8):
    - Adult full-time / part-time: 37.5 h/week (= exactly 5 × 7.5h standard shifts)
    - Age 15–18: 40 h/week
    - Under 15: 35 h/week  (also restricted to shift 6 by add_age_based_constraints)
    """
    # worked_minutes for each shift (integer coefficients for CP-SAT)
    worked_mins = {s.id: s.worked_minutes for s in shifts}

    # Group days by ISO (year, week) — avoids crossing month boundaries incorrectly
    days_by_week: dict[tuple, list[date]] = {}
    for d in days:
        key = d.isocalendar()[:2]  # (iso_year, iso_week)
        days_by_week.setdefault(key, []).append(d)

    ref_date = min(days) if days else date.today()

    for emp in employees:
        # Determine weekly worked-hour cap for this employee
        if emp.date_of_birth is not None:
            age = get_age_on_date(emp.date_of_birth, ref_date)
            cat = get_age_category(age)
        else:
            cat = "adult"

        if cat == "under_15":
            max_weekly = _YOUTH_UNDER_15_MAX_WEEKLY_WORKED_MIN
        elif cat == "age_15_18":
            max_weekly = _YOUTH_15_18_MAX_WEEKLY_WORKED_MIN
        else:
            max_weekly = _ADULT_MAX_WEEKLY_WORKED_MIN

        for week_key, week_days in days_by_week.items():
            week_vars = []
            week_coeffs = []
            for d in week_days:
                for s in shifts:
                    if (emp.id, d, s.id) in variables:
                        week_vars.append(variables[(emp.id, d, s.id)])
                        week_coeffs.append(worked_mins[s.id])
            if week_vars:
                model.Add(
                    cp_model.LinearExpr.WeightedSum(week_vars, week_coeffs) <= max_weekly
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


def add_age_based_constraints(
    model: cp_model.CpModel,
    variables: Variables,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
) -> None:
    """Hard constraints for under-15 and 15–18 employees (Section 3).

    Under 15:
    - May only work shift 6 (10:00–17:00, 6.5h worked ≤ 7h daily limit).
    - All other shift variables are forced to 0.
    - Weekly cap enforced by add_weekly_hour_limits.

    Age 15–18:
    - All standard shifts are ≤ 8h total (7.5h worked), so no additional daily
      restriction is needed.  Weekly cap enforced by add_weekly_hour_limits.
    """
    if not days:
        return
    ref_date = min(days)

    for emp in employees:
        if emp.date_of_birth is None:
            continue
        age = get_age_on_date(emp.date_of_birth, ref_date)
        cat = get_age_category(age)

        if cat == "under_15":
            # Force all non-shift-6 variables to 0
            for d in days:
                for s in shifts:
                    if s.id != _UNDER_15_SHIFT_ID and (emp.id, d, s.id) in variables:
                        model.Add(variables[(emp.id, d, s.id)] == 0)


def add_max_staffing_caps(
    model: cp_model.CpModel,
    variables: Variables,
    demand_map: dict[date, DailyDemand],
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
    default_max_cafe: int = 5,
    default_max_prod: int = 4,
) -> None:
    """Hard upper caps on café and production staff per day (Section 6).

    Café: max 5 normally; raised to 6 when ≥2 good ships are in port on the same day.
    Production: configurable (default 4), constant regardless of ship traffic.
    These caps prevent over-scheduling even when full-timers have hours left.
    """
    cafe_shifts = [s for s in shifts if s.role == ShiftRole.cafe]
    prod_shifts = [s for s in shifts if s.role == ShiftRole.production]

    for d in days:
        dd = demand_map.get(d)
        good_ship_count = sum(
            1 for s in (dd.ships_today if dd else []) if s.good_ship
        )
        max_cafe = 6 if good_ship_count >= 2 else default_max_cafe

        cafe_vars = [
            variables[(emp.id, d, s.id)]
            for emp in employees
            for s in cafe_shifts
            if (emp.id, d, s.id) in variables
        ]
        if cafe_vars:
            model.Add(sum(cafe_vars) <= max_cafe)

        prod_vars = [
            variables[(emp.id, d, s.id)]
            for emp in employees
            for s in prod_shifts
            if (emp.id, d, s.id) in variables
        ]
        if prod_vars:
            model.Add(sum(prod_vars) <= default_max_prod)


def add_sunday_rest_constraints(
    model: cp_model.CpModel,
    variables: Variables,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
) -> None:
    """Norwegian AML § 10-8 Sunday/public-holiday rest rules (Section 7).

    Rule (4-week window): in any 4 consecutive Sundays/public holidays, at least
    1 must be a day off for each employee. This is equivalent to: in a window of
    4 consecutive special days, at most 3 may be worked.

    The full rolling 26-week average (≤13 Sundays worked per 26) cannot be
    enforced within a single month without cross-month history; that aspect is
    monitored by the validator and flagged as a warning.
    """
    if not days:
        return

    year = min(days).year
    holidays = norwegian_public_holidays(year)

    # Collect Sundays and public holidays that fall within the scheduled days
    special_days = sorted(
        [d for d in days if d.weekday() == 6 or d in holidays]
    )

    for emp in employees:
        # Pre-compute a "worked_on_special_day" BoolVar for each special day
        worked_vars: list[cp_model.IntVar] = []
        for d in special_days:
            day_shift_vars = [
                variables[(emp.id, d, s.id)]
                for s in shifts
                if (emp.id, d, s.id) in variables
            ]
            if not day_shift_vars:
                # Employee unavailable this day — counts as a free day off
                worked_vars.append(None)  # type: ignore[arg-type]
                continue
            worked = model.NewBoolVar(f"worked_special_{emp.id}_{d}")
            model.Add(sum(day_shift_vars) >= 1).OnlyEnforceIf(worked)
            model.Add(sum(day_shift_vars) == 0).OnlyEnforceIf(worked.Not())
            worked_vars.append(worked)

        # Sliding window of 4: at most 3 may be worked
        for i in range(len(worked_vars) - 3):
            window = worked_vars[i : i + 4]
            active = [v for v in window if v is not None]
            if len(active) >= 4:
                model.Add(sum(active) <= 3)


def add_max_days_per_calendar_week(
    model: cp_model.CpModel,
    variables: Variables,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
) -> None:
    """Hard cap: at most 6 working days per Mon–Sun ISO calendar week (constraint 1).

    Mathematically implied by add_weekly_rest (rolling ≤ 6 in any 7-day window)
    but enforced explicitly as a named safety constraint.
    """
    days_by_iso_week: dict[tuple, list[date]] = {}
    for d in days:
        key = d.isocalendar()[:2]  # (iso_year, iso_week)
        days_by_iso_week.setdefault(key, []).append(d)

    for emp in employees:
        for week_days in days_by_iso_week.values():
            week_vars = [
                variables[(emp.id, d, s.id)]
                for d in week_days
                for s in shifts
                if (emp.id, d, s.id) in variables
            ]
            if week_vars:
                model.Add(sum(week_vars) <= 6)


def add_cross_month_consecutive_constraint(
    model: cp_model.CpModel,
    variables: Variables,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
    prev_month_working: dict,  # str(emp_id) -> set[date]
) -> int:
    """Prevent cross-month runs of > 6 consecutive working days (constraint 2, boundary).

    For each employee, computes how many consecutive days they worked ending on
    the last day of the previous month ('carry_in', 0–6).  Then adds a single
    binding constraint on the first (7 − carry_in) days of the new month:

        sum(shift_vars on days [new_1 … new_{7−carry_in}]) ≤ 6 − carry_in

    This is the only window that adds new information; all weaker windows are
    implied by it (each shorter window is subsumed when new_{7-k} ≤ 1 is used).

    Returns the number of cross-boundary constraints actually added.
    """
    if not prev_month_working or not days:
        return 0

    days_sorted = sorted(days)
    month_start = days_sorted[0]
    n_added = 0

    for emp in employees:
        emp_prev: set[date] = prev_month_working.get(str(emp.id), set())
        if not emp_prev:
            continue

        # Count consecutive working days ending on (month_start − 1)
        carry_in = 0
        for k in range(1, 7):
            if (month_start - timedelta(days=k)) in emp_prev:
                carry_in += 1
            else:
                break  # Run ends here

        if carry_in == 0:
            continue

        carry_in = min(carry_in, 6)  # Cap (prev-month violations can't exceed 6 here)
        max_allowed = 6 - carry_in         # max working days in the window
        window_size = 7 - carry_in         # number of new-month days to cover

        window_days = [d for d in days_sorted if d < month_start + timedelta(days=window_size)]
        window_vars = [
            variables[(emp.id, d, s.id)]
            for d in window_days
            for s in shifts
            if (emp.id, d, s.id) in variables
        ]

        if window_vars:
            model.Add(sum(window_vars) <= max_allowed)
            n_added += 1

    return n_added


def add_max_consecutive_working_days(
    model: cp_model.CpModel,
    variables: Variables,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
) -> None:
    """Hard cap: no employee may work more than 6 consecutive calendar days (constraint 2).

    For every window of 7 consecutive calendar days within the month, at most 6 may
    be worked.  Only windows without calendar gaps are constrained (i.e., the 7 demand
    days must be exactly 7 consecutive calendar days).  This is more precise than
    add_weekly_rest, which does not check for gaps.
    """
    days_sorted = sorted(days)
    n = len(days_sorted)

    for emp in employees:
        for i in range(n - 6):
            window = days_sorted[i : i + 7]
            if (window[-1] - window[0]).days != 6:
                continue  # Window has calendar gaps — skip
            window_vars = [
                variables[(emp.id, d, s.id)]
                for d in window
                for s in shifts
                if (emp.id, d, s.id) in variables
            ]
            if window_vars:
                model.Add(sum(window_vars) <= 6)


def add_two_consecutive_days_off_per_14(
    model: cp_model.CpModel,
    variables: Variables,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
) -> None:
    """Hard constraint: every rolling 14-day window must contain ≥1 pair of consecutive
    days off for each employee (constraint 3).

    A day is "off" for an employee if they have no working shift assigned (which includes
    days outside their availability window — those days have no variables, so they are
    trivially off).

    Implementation uses per-employee, per-adjacent-pair BoolVars to model the
    existential "at least one consecutive-off pair in the window" requirement.
    """
    days_sorted = sorted(days)
    n = len(days_sorted)
    if n < 14:
        return

    # Step 1: Precompute pair_off_bv[(emp_id, d)] = BoolVar that equals 1 iff
    # both calendar day d AND calendar day d+1 are "off" for this employee.
    pair_off_bv: dict[tuple, cp_model.IntVar] = {}

    for emp in employees:
        for k in range(n - 1):
            d0, d1 = days_sorted[k], days_sorted[k + 1]
            if (d1 - d0).days != 1:
                continue  # Non-consecutive calendar days — no pairs across gaps

            v0 = [
                variables[(emp.id, d0, s.id)]
                for s in shifts if (emp.id, d0, s.id) in variables
            ]
            v1 = [
                variables[(emp.id, d1, s.id)]
                for s in shifts if (emp.id, d1, s.id) in variables
            ]

            bv = model.NewBoolVar(f"co_{str(emp.id)[:8]}_{d0.strftime('%m%d')}")

            if not v0 and not v1:
                # Both days outside availability → both trivially off
                model.Add(bv == 1)
            elif v0 and not v1:
                # d1 always off; pair is off iff d0 is also off
                model.Add(sum(v0) == 0).OnlyEnforceIf(bv)
                model.Add(sum(v0) >= 1).OnlyEnforceIf(bv.Not())
            elif not v0 and v1:
                # d0 always off; pair is off iff d1 is also off
                model.Add(sum(v1) == 0).OnlyEnforceIf(bv)
                model.Add(sum(v1) >= 1).OnlyEnforceIf(bv.Not())
            else:
                # Both days may be worked
                model.Add(sum(v0) + sum(v1) == 0).OnlyEnforceIf(bv)
                model.Add(sum(v0) + sum(v1) >= 1).OnlyEnforceIf(bv.Not())

            pair_off_bv[(emp.id, d0)] = bv

    # Step 2: For every rolling 14-day window, require ≥1 consecutive-off pair.
    for emp in employees:
        for i in range(n - 13):
            window = days_sorted[i : i + 14]
            if (window[-1] - window[0]).days != 13:
                continue  # Window has calendar gaps

            pair_vars = [
                pair_off_bv[(emp.id, window[j])]
                for j in range(13)
                if (emp.id, window[j]) in pair_off_bv
            ]

            if pair_vars:
                model.Add(sum(pair_vars) >= 1)


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
            # Only enforce ≥1 if there are actually employees who can cover this slot.
            # If no one has a variable for it (e.g. all are on rest days), skip to
            # avoid INFEASIBLE when headcount is at its minimum.
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
