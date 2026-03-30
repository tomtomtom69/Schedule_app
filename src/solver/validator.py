"""Post-generation constraint checker — Phase 3."""
from dataclasses import dataclass
from datetime import date, timedelta

from src.demand.forecaster import DailyDemand
from src.models.employee import EmployeeRead
from src.models.schedule import AssignmentRead, ScheduleRead
from src.models.shift_template import ShiftTemplateRead
from src.models.enums import EmploymentType, Housing, RoleCapability, ShiftRole


@dataclass
class Violation:
    severity: str        # "error" (hard constraint) or "warning" (soft constraint)
    constraint: str      # machine-readable constraint name
    employee: str        # employee name or "—" for day-level violations
    date: date | None
    message: str


def _time_to_minutes(t) -> int:
    return t.hour * 60 + t.minute


def _shift_duration_minutes(shift: ShiftTemplateRead) -> int:
    return _time_to_minutes(shift.end_time) - _time_to_minutes(shift.start_time)


def validate_schedule(
    schedule: ScheduleRead,
    employees: list[EmployeeRead],
    demand: list[DailyDemand],
    shift_templates: list[ShiftTemplateRead],
) -> list[Violation]:
    """Run all validation checks and return a list of violations.

    Hard constraint violations have severity="error".
    Soft constraint violations have severity="warning".
    An empty list means the schedule is fully valid.
    """
    violations: list[Violation] = []

    # Build lookup tables
    emp_lookup: dict = {str(emp.id): emp for emp in employees}
    shift_lookup: dict = {s.id: s for s in shift_templates}
    demand_map: dict[date, DailyDemand] = {d.date: d for d in demand}
    all_days: list[date] = sorted(demand_map.keys())

    # Working assignments only (exclude day-off placeholders)
    working: list[AssignmentRead] = [a for a in schedule.assignments if not a.is_day_off]

    # Index: (employee_id, date) → assignment
    assignments_by_emp_day: dict = {}
    for a in working:
        key = (str(a.employee_id), a.date)
        assignments_by_emp_day.setdefault(key, []).append(a)

    # Index: employee_id → sorted list of working assignments
    by_emp: dict[str, list[AssignmentRead]] = {}
    for a in working:
        by_emp.setdefault(str(a.employee_id), []).append(a)
    for emp_id in by_emp:
        by_emp[emp_id].sort(key=lambda x: x.date)

    # Try to load previous month's schedule for cross-month consecutive-day check
    prev_month_working = _load_prev_month_working_dates(schedule.year, schedule.month)

    # ── Hard constraints ─────────────────────────────────────────────────────

    violations += _check_one_shift_per_day(assignments_by_emp_day, emp_lookup)
    violations += _check_shift_duration(working, emp_lookup, shift_lookup)
    violations += _check_role_capability(working, emp_lookup, shift_lookup)
    violations += _check_availability(working, emp_lookup)
    violations += _check_daily_rest(by_emp, emp_lookup, shift_lookup)
    violations += _check_weekly_hours(by_emp, emp_lookup, shift_lookup)
    violations += _check_weekly_rest(by_emp, emp_lookup)
    violations += _check_max_days_per_calendar_week(by_emp, emp_lookup)
    violations += _check_max_consecutive_working_days(by_emp, emp_lookup, prev_month_working)
    violations += _check_two_consecutive_days_off_per_14(by_emp, emp_lookup, all_days)
    violations += _check_daily_staffing(working, demand_map, shift_templates, schedule)
    violations += _check_language_coverage(working, demand_map, emp_lookup, shift_lookup)
    violations += _check_eidsdal_drivers(working, demand_map, emp_lookup, shift_lookup)

    # ── Soft constraints (warnings) ──────────────────────────────────────────

    violations += _check_part_timer_preference(working, emp_lookup)
    violations += _check_overtime(by_emp, emp_lookup, shift_lookup)

    return violations


# ── Hard constraint checks ────────────────────────────────────────────────────


def _check_one_shift_per_day(
    assignments_by_emp_day: dict, emp_lookup: dict
) -> list[Violation]:
    violations = []
    for (emp_id, d), assignments in assignments_by_emp_day.items():
        if len(assignments) > 1:
            emp = emp_lookup.get(emp_id)
            name = emp.name if emp else emp_id
            violations.append(Violation(
                severity="error",
                constraint="one_shift_per_day",
                employee=name,
                date=d,
                message=f"Assigned {len(assignments)} shifts on the same day",
            ))
    return violations


def _check_shift_duration(
    working: list[AssignmentRead], emp_lookup: dict, shift_lookup: dict
) -> list[Violation]:
    violations = []
    max_minutes = 10 * 60
    for a in working:
        shift = shift_lookup.get(a.shift_id)
        if shift is None:
            continue
        duration = _shift_duration_minutes(shift)
        if duration > max_minutes:
            emp = emp_lookup.get(str(a.employee_id))
            violations.append(Violation(
                severity="error",
                constraint="max_shift_duration",
                employee=emp.name if emp else str(a.employee_id),
                date=a.date,
                message=f"Shift {a.shift_id} is {duration // 60}h {duration % 60}m (max 10h)",
            ))
    return violations


def _check_role_capability(
    working: list[AssignmentRead], emp_lookup: dict, shift_lookup: dict
) -> list[Violation]:
    violations = []
    for a in working:
        emp = emp_lookup.get(str(a.employee_id))
        shift = shift_lookup.get(a.shift_id)
        if emp is None or shift is None:
            continue
        cap = emp.role_capability
        role = shift.role
        if cap == RoleCapability.cafe and role != ShiftRole.cafe:
            violations.append(Violation(
                severity="error",
                constraint="role_capability",
                employee=emp.name,
                date=a.date,
                message=f"Café-only employee assigned to {role.value} shift {a.shift_id}",
            ))
        elif cap == RoleCapability.production and role != ShiftRole.production:
            violations.append(Violation(
                severity="error",
                constraint="role_capability",
                employee=emp.name,
                date=a.date,
                message=f"Production-only employee assigned to {role.value} shift {a.shift_id}",
            ))
    return violations


def _check_availability(
    working: list[AssignmentRead], emp_lookup: dict
) -> list[Violation]:
    violations = []
    for a in working:
        emp = emp_lookup.get(str(a.employee_id))
        if emp is None:
            continue
        if not (emp.availability_start <= a.date <= emp.availability_end):
            violations.append(Violation(
                severity="error",
                constraint="availability",
                employee=emp.name,
                date=a.date,
                message=(
                    f"Assigned outside availability "
                    f"({emp.availability_start} – {emp.availability_end})"
                ),
            ))
    return violations


def _check_daily_rest(
    by_emp: dict[str, list[AssignmentRead]], emp_lookup: dict, shift_lookup: dict
) -> list[Violation]:
    """Min 11h between end of one shift and start of the next (on consecutive days)."""
    violations = []
    min_rest_minutes = 11 * 60

    for emp_id, assignments in by_emp.items():
        emp = emp_lookup.get(emp_id)
        name = emp.name if emp else emp_id
        for i in range(len(assignments) - 1):
            a1 = assignments[i]
            a2 = assignments[i + 1]
            if (a2.date - a1.date).days != 1:
                continue  # not consecutive days
            s1 = shift_lookup.get(a1.shift_id)
            s2 = shift_lookup.get(a2.shift_id)
            if s1 is None or s2 is None:
                continue
            s1_end = _time_to_minutes(s1.end_time)
            s2_start = _time_to_minutes(s2.start_time)
            rest = (24 * 60 - s1_end) + s2_start
            if rest < min_rest_minutes:
                h, m = rest // 60, rest % 60
                violations.append(Violation(
                    severity="error",
                    constraint="daily_rest",
                    employee=name,
                    date=a2.date,
                    message=f"Only {h}h {m}m rest between shifts (min 11h)",
                ))
    return violations


def _check_weekly_hours(
    by_emp: dict[str, list[AssignmentRead]], emp_lookup: dict, shift_lookup: dict
) -> list[Violation]:
    """Max weekly worked hours per employee (Section 2 + Section 8).

    Uses worked_hours (template duration − 0.5h break) to match what the solver
    enforces. Limit is 37.5h for adults, 40h for age 15–18, 35h for under-15.
    """
    from src.models.employee import get_age_on_date, get_age_category
    violations = []

    for emp_id, assignments in by_emp.items():
        emp = emp_lookup.get(emp_id)
        name = emp.name if emp else emp_id

        # Determine weekly limit for this employee
        if emp and emp.date_of_birth:
            ref_date = min(a.date for a in assignments)
            age = get_age_on_date(emp.date_of_birth, ref_date)
            cat = get_age_category(age)
        else:
            cat = "adult"

        if cat == "under_15":
            max_worked_h = 35.0
        elif cat == "age_15_18":
            max_worked_h = 40.0
        else:
            max_worked_h = 37.5

        by_week: dict[tuple, float] = {}
        for a in assignments:
            week_key = a.date.isocalendar()[:2]
            shift = shift_lookup.get(a.shift_id)
            if shift:
                by_week[week_key] = by_week.get(week_key, 0.0) + shift.worked_hours
        for week_key, total_h in by_week.items():
            if total_h > max_worked_h + 0.01:  # tiny float tolerance
                violations.append(Violation(
                    severity="error",
                    constraint="weekly_hour_limit",
                    employee=name,
                    date=None,
                    message=(
                        f"Week {week_key[1]}/{week_key[0]}: "
                        f"{total_h:.1f}h worked (max {max_worked_h}h)"
                    ),
                ))
    return violations


def _check_weekly_rest(
    by_emp: dict[str, list[AssignmentRead]], emp_lookup: dict
) -> list[Violation]:
    """At least 1 day off per 7-day rolling window (35h continuous rest)."""
    violations = []
    for emp_id, assignments in by_emp.items():
        emp = emp_lookup.get(emp_id)
        name = emp.name if emp else emp_id
        working_days = sorted({a.date for a in assignments})
        n = len(working_days)
        for i in range(n - 6):
            window = working_days[i : i + 7]
            # Check if all 7 calendar days are worked
            first = window[0]
            all_7 = [first + timedelta(days=j) for j in range(7)]
            worked_set = set(window)
            days_off = [d for d in all_7 if d not in worked_set]
            if not days_off:
                violations.append(Violation(
                    severity="error",
                    constraint="weekly_rest",
                    employee=name,
                    date=first,
                    message=f"No day off in 7-day window starting {first} (min 35h rest required)",
                ))
    return violations


def _check_max_days_per_calendar_week(
    by_emp: dict[str, list[AssignmentRead]], emp_lookup: dict
) -> list[Violation]:
    """Max 6 working days per Mon–Sun ISO calendar week (constraint 1)."""
    violations = []
    for emp_id, assignments in by_emp.items():
        emp = emp_lookup.get(emp_id)
        name = emp.name if emp else emp_id
        by_week: dict[tuple, int] = {}
        for a in assignments:
            wk = a.date.isocalendar()[:2]
            by_week[wk] = by_week.get(wk, 0) + 1
        for wk, count in by_week.items():
            if count > 6:
                violations.append(Violation(
                    severity="error",
                    constraint="max_days_per_calendar_week",
                    employee=name,
                    date=None,
                    message=(
                        f"Week {wk[1]}/{wk[0]}: {count} working days "
                        "(max 6 per Mon–Sun week)"
                    ),
                ))
    return violations


def _load_prev_month_working_dates(year: int, month: int) -> dict[str, set[date]]:
    """Try to load the previous month's working dates per employee from DB.

    Returns a dict keyed by str(employee_id) → set of worked dates.
    Returns {} silently on any failure (DB unavailable, no previous schedule).
    """
    try:
        from src.db.database import db_session
        from src.models.schedule import ScheduleORM

        prev_month = month - 1
        prev_year = year
        if prev_month == 0:
            prev_month = 12
            prev_year -= 1

        with db_session() as db:
            orm = (
                db.query(ScheduleORM)
                .filter_by(year=prev_year, month=prev_month)
                .order_by(ScheduleORM.created_at.desc())
                .first()
            )
            if not orm:
                return {}
            result: dict[str, set[date]] = {}
            for a in orm.assignments:
                if not a.is_day_off:
                    result.setdefault(str(a.employee_id), set()).add(a.date)
            return result
    except Exception:
        return {}


def _check_max_consecutive_working_days(
    by_emp: dict[str, list[AssignmentRead]],
    emp_lookup: dict,
    prev_month_working: dict[str, set[date]] | None = None,
) -> list[Violation]:
    """Max 6 consecutive calendar working days (constraint 2).

    If prev_month_working is provided, the last 6 days of the previous month are
    prepended so that month-boundary runs are detected.
    """
    violations = []
    for emp_id, assignments in by_emp.items():
        emp = emp_lookup.get(emp_id)
        name = emp.name if emp else emp_id
        current_dates = sorted({a.date for a in assignments})
        if not current_dates:
            continue

        # Prepend up to 6 days from the previous month if available
        if prev_month_working and emp_id in prev_month_working:
            prev_dates = sorted(prev_month_working[emp_id])
            combined = sorted(set(prev_dates[-6:]) | set(current_dates))
        else:
            combined = current_dates

        # Scan for runs > 6
        run_start = combined[0]
        run_len = 1
        for i in range(1, len(combined)):
            if (combined[i] - combined[i - 1]).days == 1:
                run_len += 1
            else:
                if run_len > 6:
                    violations.append(Violation(
                        severity="error",
                        constraint="max_consecutive_working_days",
                        employee=name,
                        date=run_start,
                        message=f"{run_len} consecutive working days from {run_start} (max 6)",
                    ))
                run_start = combined[i]
                run_len = 1
        if run_len > 6:
            violations.append(Violation(
                severity="error",
                constraint="max_consecutive_working_days",
                employee=name,
                date=run_start,
                message=f"{run_len} consecutive working days from {run_start} (max 6)",
            ))
    return violations


def _check_two_consecutive_days_off_per_14(
    by_emp: dict[str, list[AssignmentRead]],
    emp_lookup: dict,
    all_days: list[date],
) -> list[Violation]:
    """Every rolling 14-day window must contain ≥1 consecutive off pair (constraint 3).

    A day is "off" if no working assignment exists for that day.
    Windows that include calendar gaps (non-consecutive demand days) are skipped.
    When a violation window is found, advances by 14 days to avoid flooding.
    """
    violations = []
    all_days_sorted = sorted(all_days)
    n = len(all_days_sorted)
    if n < 14:
        return violations

    for emp_id, assignments in by_emp.items():
        emp = emp_lookup.get(emp_id)
        name = emp.name if emp else emp_id
        working_dates: set[date] = {a.date for a in assignments}

        i = 0
        while i <= n - 14:
            window = all_days_sorted[i : i + 14]
            if (window[-1] - window[0]).days != 13:
                i += 1
                continue  # Window has calendar gaps

            has_consecutive_off = any(
                window[j] not in working_dates and window[j + 1] not in working_dates
                for j in range(13)
            )

            if not has_consecutive_off:
                violations.append(Violation(
                    severity="error",
                    constraint="two_consecutive_days_off_per_14",
                    employee=name,
                    date=window[0],
                    message=(
                        f"No 2 consecutive days off in 14-day window "
                        f"{window[0]} – {window[13]}"
                    ),
                ))
                i += 14  # Skip to next non-overlapping window to avoid flooding
            else:
                i += 1

    return violations


def _check_daily_staffing(
    working: list[AssignmentRead],
    demand_map: dict[date, DailyDemand],
    shift_templates: list[ShiftTemplateRead],
    schedule: ScheduleRead,
) -> list[Violation]:
    """Check that each day meets the cafe_needed and production_needed counts."""
    violations = []
    shift_lookup = {s.id: s for s in shift_templates}

    # Group by date
    by_date: dict[date, list[AssignmentRead]] = {}
    for a in working:
        by_date.setdefault(a.date, []).append(a)

    for d, demand in demand_map.items():
        day_assignments = by_date.get(d, [])
        cafe_count = sum(
            1 for a in day_assignments
            if shift_lookup.get(a.shift_id) and shift_lookup[a.shift_id].role == ShiftRole.cafe
        )
        prod_count = sum(
            1 for a in day_assignments
            if shift_lookup.get(a.shift_id) and shift_lookup[a.shift_id].role == ShiftRole.production
        )
        if cafe_count < demand.cafe_needed:
            violations.append(Violation(
                severity="error",
                constraint="daily_staffing",
                employee="—",
                date=d,
                message=f"Café: {cafe_count} assigned, {demand.cafe_needed} needed",
            ))
        if prod_count < demand.production_needed:
            violations.append(Violation(
                severity="error",
                constraint="daily_staffing",
                employee="—",
                date=d,
                message=f"Production: {prod_count} assigned, {demand.production_needed} needed",
            ))
    return violations


def _check_language_coverage(
    working: list[AssignmentRead],
    demand_map: dict[date, DailyDemand],
    emp_lookup: dict,
    shift_lookup: dict,
) -> list[Violation]:
    """For each day with required languages, at least 1 café employee must speak it."""
    violations = []
    by_date: dict[date, list[AssignmentRead]] = {}
    for a in working:
        by_date.setdefault(a.date, []).append(a)

    for d, demand in demand_map.items():
        for lang in demand.languages_required:
            cafe_speakers = 0
            for a in by_date.get(d, []):
                shift = shift_lookup.get(a.shift_id)
                emp = emp_lookup.get(str(a.employee_id))
                if shift and shift.role == ShiftRole.cafe and emp:
                    if any(l.lower().strip() == lang.lower() for l in emp.languages):
                        cafe_speakers += 1
            if cafe_speakers == 0:
                violations.append(Violation(
                    severity="error",
                    constraint="language_coverage",
                    employee="—",
                    date=d,
                    message=f"No {lang.title()} speaker on café shift (required by ship)",
                ))
    return violations


def _check_eidsdal_drivers(
    working: list[AssignmentRead],
    demand_map: dict[date, DailyDemand],
    emp_lookup: dict,
    shift_lookup: dict,
) -> list[Violation]:
    """For each day: verify Eidsdal driver requirement and capacity cap."""
    violations = []
    by_date: dict[date, list[AssignmentRead]] = {}
    for a in working:
        by_date.setdefault(a.date, []).append(a)

    for d in demand_map:
        eidsdal_workers = []
        eidsdal_drivers = []
        for a in by_date.get(d, []):
            emp = emp_lookup.get(str(a.employee_id))
            if emp and emp.housing == Housing.eidsdal:
                eidsdal_workers.append(emp)
                if emp.driving_licence:
                    eidsdal_drivers.append(emp)

        n = len(eidsdal_workers)
        if n == 0:
            continue
        if n > 10:
            violations.append(Violation(
                severity="error",
                constraint="eidsdal_capacity",
                employee="—",
                date=d,
                message=f"{n} Eidsdal workers scheduled (max 10 / 2 cars × 5 seats)",
            ))
        if len(eidsdal_drivers) < 1:
            violations.append(Violation(
                severity="error",
                constraint="eidsdal_driver",
                employee="—",
                date=d,
                message=f"{n} Eidsdal workers scheduled but no licensed driver",
            ))
        elif n > 5 and len(eidsdal_drivers) < 2:
            violations.append(Violation(
                severity="error",
                constraint="eidsdal_driver",
                employee="—",
                date=d,
                message=f"{n} Eidsdal workers (>5) but only 1 driver (need 2 for 2nd car)",
            ))
    return violations


# ── Soft constraint checks (warnings) ────────────────────────────────────────


def _check_part_timer_preference(
    working: list[AssignmentRead], emp_lookup: dict
) -> list[Violation]:
    """Warn when part-time employees are used — suggests full-timers may be under-utilised."""
    violations = []
    part_timer_days: dict[str, list[date]] = {}
    for a in working:
        emp = emp_lookup.get(str(a.employee_id))
        if emp and emp.employment_type == EmploymentType.part_time:
            part_timer_days.setdefault(emp.name, []).append(a.date)

    for name, days in part_timer_days.items():
        if len(days) > 5:  # Only flag excessive part-time usage
            violations.append(Violation(
                severity="warning",
                constraint="full_time_preference",
                employee=name,
                date=None,
                message=f"Part-timer scheduled {len(days)} days — check if full-timers are available",
            ))
    return violations


def _check_overtime(
    by_emp: dict[str, list[AssignmentRead]], emp_lookup: dict, shift_lookup: dict
) -> list[Violation]:
    """Warn when a full-time employee's weekly worked hours exceed 37.5h (Section 8)."""
    violations = []
    for emp_id, assignments in by_emp.items():
        emp = emp_lookup.get(emp_id)
        name = emp.name if emp else emp_id
        by_week: dict[tuple, float] = {}
        for a in assignments:
            wk = a.date.isocalendar()[:2]
            shift = shift_lookup.get(a.shift_id)
            if shift:
                by_week[wk] = by_week.get(wk, 0.0) + shift.worked_hours
        for wk, total_h in by_week.items():
            if total_h > 37.5 + 0.01:
                violations.append(Violation(
                    severity="warning",
                    constraint="overtime",
                    employee=name,
                    date=None,
                    message=f"Week {wk[1]}/{wk[0]}: {total_h:.1f}h worked (>37.5h planned target)",
                ))
    return violations
