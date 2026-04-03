"""Soft constraint weights and objective function — Phase 3."""
from datetime import date

from ortools.sat.python import cp_model

from src.demand.forecaster import DailyDemand
from src.models.employee import EmployeeRead
from src.models.shift_template import ShiftTemplateRead
from src.models.enums import EmploymentType, Housing, RoleCapability, ShiftRole

Variables = dict[tuple, cp_model.IntVar]  # (employee_id, date, shift_id) -> BoolVar

WEIGHTS = {
    "language_coverage": 100,   # highest priority soft constraint
    # Priority 4 waterfall: fill contracted hours (high weight — outweighs over-coverage)
    # Full-time: +50 per shift up to weekly target (37.5h ÷ 7.5h = 5 shifts/week)
    # Part-time: +25 per shift up to their contracted target
    "contracted_hours": 50,
    # Section 4: staffing priority waterfall — per-assignment day-type rewards
    "good_ship_day": 60,        # reward each assignment on a good-ship day
    "cruise_day": 35,           # reward each assignment on a regular cruise day
    "no_cruise_day": 15,        # base reward for any working assignment
    "part_time_penalty": 10,    # deducted from the day reward for PT assignments
    # Section 5: role priority — keep "both" employees on production
    "both_on_production": 20,   # reward for "both" employee on production shift
    "both_on_cafe": 20,         # penalty for "both" employee on café shift (applied as -w)
    # Other soft constraints
    "eidsdal_grouping": 8,
    "employee_preferences": 5,
    # fair_distribution: penalty per shift of spread between most- and least-worked
    # employee (uses shift COUNT, not minutes — avoids catastrophic scale mismatch
    # with contracted_hours reward).  Weight=5 per shift means max penalty ≈ 5×31=155,
    # well below the contracted_hours reward of 50/shift.
    "fair_distribution": 5,
    "minimize_overtime": 3,
    "shift_variety": 2,         # small penalty for same shift on consecutive days
    # Over-coverage penalty — discourages piling extras on already-staffed days
    # Tier 1: first extra person above daily minimum per role (−3 pts)
    # Tier 2: second extra person above daily minimum per role (additional −5 pts)
    # Effect: spreading 2 extras across 2 days (−3−3=−6) beats concentrating both
    #         on 1 day (−3−5=−8), so the solver naturally spreads over-coverage.
    "over_coverage_t1": 3,
    "over_coverage_t2": 5,
}

# Weekly limits in worked minutes (not raw template duration)
_ADULT_NORMAL_WEEKLY_WORKED_MIN = int(37.5 * 60)   # 2250 min = 37.5h


def _time_to_minutes(t) -> int:
    return t.hour * 60 + t.minute


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
    disable_both_preference: bool = False,
) -> None:
    """Add all soft constraints and set the model's Maximize objective.

    disable_both_preference: when True (fallback Step D+), the production-first
    preference for 'both' employees is omitted so they are assigned freely to
    whichever role helps feasibility without any soft penalty.
    """
    obj_vars: list[cp_model.IntVar] = []
    obj_coeffs: list[int] = []

    # Language coverage is high-priority soft (was previously a hard constraint that
    # caused infeasibility when no speaker was available for a given ship day).
    if demand_map:
        prefer_language_coverage(model, variables, employees, shifts, days, demand_map, obj_vars, obj_coeffs)

    # Section 4: staffing priority waterfall (replaces the flat prefer_full_time)
    if demand_map:
        prefer_high_demand_days(model, variables, employees, shifts, days, demand_map, obj_vars, obj_coeffs)
    else:
        # Fallback if no demand info: flat full-time preference
        prefer_full_time(model, variables, employees, shifts, days, obj_vars, obj_coeffs)

    # Priority 4: fill contracted hours for each employee up to their weekly target.
    # Weight=50/shift (FT) and 25/shift (PT) — high enough to outweigh the
    # over-coverage penalties (−3/−5 day-level) and the fair-distribution penalty
    # (−5/shift).  This is the "waterfall" that ensures full-timers reach ~162h/month.
    reward_contracted_hours(model, variables, employees, shifts, days, obj_vars, obj_coeffs)

    # Section 5: role priority — keep "both" employees on production.
    # Skipped in fallback mode so flex workers can freely fill café gaps.
    if not disable_both_preference:
        prefer_both_on_production(model, variables, employees, shifts, days, obj_vars, obj_coeffs)

    group_eidsdal_shifts(model, variables, employees, shifts, days, obj_vars, obj_coeffs)
    respect_preferences(model, variables, employees, shifts, days, obj_vars, obj_coeffs)
    minimize_overtime(model, variables, employees, shifts, days, obj_vars, obj_coeffs)
    distribute_hours_fairly(model, variables, employees, shifts, days, obj_vars, obj_coeffs)
    penalize_same_shift_consecutive(model, variables, employees, shifts, days, obj_vars, obj_coeffs)
    if demand_map:
        penalize_over_coverage(model, variables, employees, shifts, days, demand_map, obj_vars, obj_coeffs)

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


def reward_contracted_hours(
    model: cp_model.CpModel,
    variables: Variables,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
    obj_vars: list,
    obj_coeffs: list,
) -> None:
    """Priority 4 waterfall: high-weight reward per shift up to each employee's
    contracted weekly hours target.

    Full-time (37.5h/week ÷ 7.5h/shift = 5 shifts/week): +50 per shift.
    Part-time (e.g. 15h/week ÷ 7.5 = 2 shifts/week): +25 per shift.
    Zero reward beyond the weekly target — no incentive for overtime.

    Weight (50) is chosen so that this constraint dominates over:
    - over_coverage_t1 penalty (−3/day): net = +47 per shift
    - fair_distribution penalty (−5/shift spread): net = +45 per shift
    Both are still positive, so the solver will always fill contracted hours
    before over-coverage or fairness penalties can deter it.
    """
    w = WEIGHTS["contracted_hours"]

    for emp in employees:
        # Derive weekly shift target from contracted_hours.
        # Standard shift = 7.5h worked; full-time = 37.5h → 5 shifts.
        target_per_week = max(1, round(emp.contracted_hours / 7.5))
        weight = w if emp.employment_type == EmploymentType.full_time else w // 2

        for week_key, week_days in _days_by_week(days).items():
            emp_week_vars = [
                variables[(emp.id, d, s.id)]
                for d in week_days
                for s in shifts
                if (emp.id, d, s.id) in variables
            ]
            if not emp_week_vars:
                continue

            suffix = f"{emp.id}_{week_key[0]}w{week_key[1]}"
            n_this_week = model.NewIntVar(0, len(emp_week_vars), f"nwk_{suffix}")
            model.Add(n_this_week == sum(emp_week_vars))

            # Cap reward at target: rewarded = min(n_this_week, target_per_week)
            rewarded = model.NewIntVar(0, target_per_week, f"rwd_{suffix}")
            model.AddMinEquality(rewarded, [n_this_week, model.NewConstant(target_per_week)])

            obj_vars.append(rewarded)
            obj_coeffs.append(weight)


def prefer_high_demand_days(
    model: cp_model.CpModel,
    variables: Variables,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
    demand_map: dict[date, DailyDemand],
    obj_vars: list,
    obj_coeffs: list,
) -> None:
    """Section 4: Staffing Priority Waterfall.

    Assigns per-assignment rewards based on the day's demand level:
    - Good-ship days get the highest reward, so the solver fills them first.
    - Regular cruise days are rewarded next.
    - No-cruise days get the lowest reward.

    Part-time assignments are penalised to prefer full-timers everywhere.

    This naturally implements the waterfall: high-demand days (and their extra
    headcount above the hard minimum) are filled before quiet days absorb
    the remaining contracted hours.
    """
    w_good = WEIGHTS["good_ship_day"]
    w_cruise = WEIGHTS["cruise_day"]
    w_no_cruise = WEIGHTS["no_cruise_day"]
    w_pt_penalty = WEIGHTS["part_time_penalty"]

    for d in days:
        dd = demand_map.get(d)
        if dd and dd.has_good_ship:
            day_reward = w_good
        elif dd and dd.has_cruise:
            day_reward = w_cruise
        else:
            day_reward = w_no_cruise

        for emp in employees:
            coeff = day_reward
            if emp.employment_type == EmploymentType.part_time:
                coeff -= w_pt_penalty
            for s in shifts:
                if (emp.id, d, s.id) in variables:
                    obj_vars.append(variables[(emp.id, d, s.id)])
                    obj_coeffs.append(coeff)


def prefer_both_on_production(
    model: cp_model.CpModel,
    variables: Variables,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
    obj_vars: list,
    obj_coeffs: list,
) -> None:
    """Section 5: Role priority — "both" employees default to production.

    Reward "both" employees on production shifts; penalise them on café shifts.
    This soft constraint has higher weight than the shift-variety nudge so it
    consistently keeps flex workers in production unless café is short-staffed.
    """
    w = WEIGHTS["both_on_production"]
    both_emps = [e for e in employees if e.role_capability == RoleCapability.both]
    if not both_emps:
        return

    cafe_sids = {s.id for s in shifts if s.role == ShiftRole.cafe}
    prod_sids = {s.id for s in shifts if s.role == ShiftRole.production}

    for emp in both_emps:
        for d in days:
            for s in shifts:
                if (emp.id, d, s.id) not in variables:
                    continue
                if s.id in prod_sids:
                    obj_vars.append(variables[(emp.id, d, s.id)])
                    obj_coeffs.append(w)
                elif s.id in cafe_sids:
                    obj_vars.append(variables[(emp.id, d, s.id)])
                    obj_coeffs.append(-w)


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
    """Penalise weekly worked hours above the employee's contracted target.

    Uses emp.contracted_hours as the per-week target so part-time employees
    (e.g. 15h/week) are penalised when scheduled beyond their contracted 2
    shifts/week, not just when they exceed the 37.5h hard cap (which they
    can never reach anyway).

    Penalty is applied in worked-minutes above target, capped at 2 full shifts
    (900 min) to keep bounds manageable.  With weight=3 the max penalty per
    week is 3×900=2700 pts — strong enough to deter over-scheduling part-timers
    on quiet days where the net reward is only +5/shift.
    """
    shift_worked = {s.id: s.worked_minutes for s in shifts}
    w = WEIGHTS["minimize_overtime"]
    max_shift_min = max(s.worked_minutes for s in shifts) if shifts else 450

    for emp in employees:
        # Use the employee's own contracted weekly hours as the cap target.
        # Full-time (37.5h) → same as the hard limit, so overtime ≈ 0.
        # Part-time (e.g. 15h) → strongly penalises working beyond 2 shifts/week.
        target_weekly_worked = max(int(emp.contracted_hours * 60), max_shift_min)

        for week_key, week_days in _days_by_week(days).items():
            wk_vars = []
            wk_coeffs = []
            for d in week_days:
                for s in shifts:
                    if (emp.id, d, s.id) in variables:
                        wk_vars.append(variables[(emp.id, d, s.id)])
                        wk_coeffs.append(shift_worked[s.id])
            if not wk_vars:
                continue

            suffix = f"{emp.id}_{week_key[0]}_{week_key[1]}"
            max_wk = _ADULT_NORMAL_WEEKLY_WORKED_MIN  # hard cap — safe upper bound
            weekly_worked = model.NewIntVar(0, max_wk, f"wkm_{suffix}")
            model.Add(
                weekly_worked == cp_model.LinearExpr.WeightedSum(wk_vars, wk_coeffs)
            )

            # Cap overtime at 2 full shifts so CP-SAT bounds stay tight
            ot_cap = 2 * max_shift_min
            overtime = model.NewIntVar(0, ot_cap, f"ot_{suffix}")
            model.Add(overtime >= weekly_worked - target_weekly_worked)
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
    """Minimise the spread between the most- and least-worked employee (by shift count).

    Uses shift COUNT (not worked minutes) so the penalty scale is compatible with
    the contracted_hours reward (weight=50/shift).  Max penalty = 5 × len(days),
    which is far below the contracted_hours reward total, so fairness never prevents
    employees from reaching their contracted targets.
    """
    if len(employees) < 2:
        return

    w = WEIGHTS["fair_distribution"]
    max_shifts = len(days)  # upper bound on shifts any one employee can work
    if max_shifts == 0:
        return

    emp_total_vars: list[cp_model.IntVar] = []
    for emp in employees:
        emp_vars = [
            variables[(emp.id, d, s.id)]
            for d in days
            for s in shifts
            if (emp.id, d, s.id) in variables
        ]
        if emp_vars:
            total = model.NewIntVar(0, max_shifts, f"total_s_{emp.id}")
            model.Add(total == sum(emp_vars))
            emp_total_vars.append(total)

    if len(emp_total_vars) < 2:
        return

    max_s = model.NewIntVar(0, max_shifts, "max_shifts_all")
    min_s = model.NewIntVar(0, max_shifts, "min_shifts_all")
    model.AddMaxEquality(max_s, emp_total_vars)
    model.AddMinEquality(min_s, emp_total_vars)

    spread = model.NewIntVar(0, max_shifts, "shifts_spread")
    model.Add(spread == max_s - min_s)

    obj_vars.append(spread)
    obj_coeffs.append(-w)


def penalize_over_coverage(
    model: cp_model.CpModel,
    variables: Variables,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    days: list[date],
    demand_map: dict[date, DailyDemand],
    obj_vars: list,
    obj_coeffs: list,
) -> None:
    """Tiered penalty for assigning staff above the daily minimum per role.

    Tier 1 (first extra): −over_coverage_t1 pts per day per role.
    Tier 2 (second extra): additional −over_coverage_t2 pts per day per role.

    The tiered structure means spreading 2 extras across 2 different days
    costs (t1 + t1 = 6) while concentrating both on 1 day costs (t1 + t2 = 8),
    so the solver naturally prefers spreading over-coverage rather than piling
    extras onto a single already-staffed day.

    Days/roles where min_needed == 0 are skipped — no penalty for adding extra
    staff there (those assignments are free-form contracted-hours fills).
    """
    w1 = WEIGHTS.get("over_coverage_t1", 3)
    w2 = WEIGHTS.get("over_coverage_t2", 5)
    if w1 == 0 and w2 == 0:
        return

    cafe_shifts = [s for s in shifts if s.role == ShiftRole.cafe]
    prod_shifts = [s for s in shifts if s.role == ShiftRole.production]

    for d in days:
        dd = demand_map.get(d)
        if dd is None:
            continue

        for role_shifts, min_needed, role_label in [
            (cafe_shifts, dd.cafe_needed, "c"),
            (prod_shifts, dd.production_needed, "p"),
        ]:
            if min_needed <= 0:
                continue  # No demand for this role/day — no over-coverage concept

            role_vars = [
                variables[(emp.id, d, s.id)]
                for emp in employees
                for s in role_shifts
                if (emp.id, d, s.id) in variables
            ]
            if not role_vars:
                continue

            tag = f"{d.strftime('%m%d')}_{role_label}"
            sum_vars = sum(role_vars)  # LinearExpr-compatible sum of BoolVars

            # Tier 1: first extra person above minimum
            if w1 > 0:
                ov1 = model.NewBoolVar(f"ov1_{tag}")
                model.Add(sum_vars >= min_needed + 1).OnlyEnforceIf(ov1)
                model.Add(sum_vars <= min_needed).OnlyEnforceIf(ov1.Not())
                obj_vars.append(ov1)
                obj_coeffs.append(-w1)

            # Tier 2: second extra person above minimum (implies tier 1)
            if w2 > 0:
                ov2 = model.NewBoolVar(f"ov2_{tag}")
                model.Add(sum_vars >= min_needed + 2).OnlyEnforceIf(ov2)
                model.Add(sum_vars <= min_needed + 1).OnlyEnforceIf(ov2.Not())
                obj_vars.append(ov2)
                obj_coeffs.append(-w2)


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
