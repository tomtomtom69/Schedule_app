"""LLM prompt templates — Phase 5.

All prompt strings live here. No prompt content is hardcoded elsewhere.
"""
from __future__ import annotations

import calendar
from datetime import date

from src.demand.forecaster import DailyDemand
from src.models.employee import EmployeeRead
from src.models.schedule import AssignmentRead, ScheduleRead
from src.models.shift_template import ShiftTemplateRead

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a scheduling assistant for Geiranger Sjokolade, a chocolate shop and café \
in Geiranger, Norway. You help the business owner understand and adjust staff schedules.

Context:
- The business has two roles: café staff and production (chocolate manufacturing)
- Staff schedules are heavily influenced by cruise ship arrivals in Geiranger fjord
- Norwegian labour law applies: max 7.5h worked per standard shift (8h on clock including \
0.5h mandatory break), 37.5h worked/week maximum for full-time adults (= 5 standard shifts), \
11h daily rest between shifts, 35h continuous weekly rest (≥1 day off per 7-day window)
- Some employees live in Eidsdal (30 min away) and share company cars \
(2 cars × 5 seats = max 10 Eidsdal workers/day; at least 1 licensed driver required, \
2 drivers if more than 5 Eidsdal workers scheduled)

VALID SHIFT IDs — you MUST only use these. NEVER suggest custom hours or partial shifts:
  Café shifts (role=café):
    1  — VAKT SHOP 1  08:00–16:00  (7.5h worked)
    2  — VAKT SHOP 2  09:30–17:30  (7.5h worked)
    3  — VAKT SHOP 3  11:00–19:00  (7.5h worked)
    4  — VAKT SHOP 4  12:00–20:00  (7.5h worked)
    5  — VAKT SHOP 5  13:00–21:00  (7.5h worked)
    6  — VAKT SHOP 6  10:00–17:00  (6.5h worked — only valid shift for under-15 employees)
  Production shifts (role=production):
    P1 — PROD 1       08:00–16:00  (7.5h worked)
    P2 — PROD 2       09:30–17:30  (7.5h worked)
    P3 — PROD 3       11:00–19:00  (7.5h worked)
    P4 — PROD 4       12:00–20:00  (7.5h worked)
    P5 — PROD 5       13:00–21:00  (7.5h worked)

- "OFF" means the employee has a day off; blank means not available that day
- Each shift = 7.5h worked + 0.5h mandatory break = 8h total on the clock.
  Exception: shift 6 = 6.5h worked + 0.5h break = 7h total (used for under-15 employees)
- Do NOT suggest any shift ID outside the list above
- Café-only employees cannot be assigned to production shifts (P1–P5)
- Production-only employees cannot be assigned to café shifts (1–6)
- Employees with role "both" can work either, but prefer production

When suggesting a specific schedule change, ALWAYS include a JSON action block \
so the system can apply it automatically. Use this format:
```json
{
  "action": "assign" | "unassign" | "day_off",
  "employee": "<exact employee name>",
  "date": "YYYY-MM-DD",
  "shift": "<shift_id from the list above, or null for day_off/unassign>",
  "reason": "<brief explanation>"
}
```
You may include multiple action blocks in one response if the change involves \
more than one employee or date. If the request is just a question with no \
specific change, skip the JSON block entirely and answer in plain text.
"""

# ── Context builder ───────────────────────────────────────────────────────────

def build_schedule_context(
    schedule: ScheduleRead,
    employees: list[EmployeeRead],
    demand: list[DailyDemand],
    violations: list,
    shift_templates: list[ShiftTemplateRead] | None = None,
) -> str:
    """Build a compact text representation of the current schedule state.

    Designed to stay well within token limits while giving the LLM enough
    context to reason about constraints and suggest changes.
    """
    if not demand:
        return "(No schedule data)"

    month_name = calendar.month_name[schedule.month]
    year = schedule.year

    # Employee lookup
    emp_by_id = {e.id: e for e in employees}

    # Shift worked_hours lookup (template duration minus 0.5h break)
    shift_hours: dict[str, float] = {}
    if shift_templates:
        for s in shift_templates:
            shift_hours[s.id] = s.worked_hours

    # Assignment lookup: {(emp_id, date): shift_id}
    assign_map: dict[tuple, str] = {}
    for a in schedule.assignments:
        assign_map[(a.employee_id, a.date)] = a.shift_id

    # Demand lookup
    demand_map = {d.date: d for d in demand}

    # ── Section 1: Header
    lines = [
        f"=== SCHEDULE: {month_name} {year} ===",
        f"Status: {schedule.status.value.upper()}",
        "",
    ]

    # ── Section 2: Employee roster (compact)
    lines.append("EMPLOYEES:")
    for e in sorted(employees, key=lambda x: (x.role_capability, x.name)):
        langs = [l for l in e.languages if l != "english"]
        lang_str = f", speaks {','.join(langs)}" if langs else ""
        housing = " [Eidsdal🏔]" if e.housing == "eidsdal" else ""
        driver = " [driver🚗]" if e.driving_licence else ""
        lines.append(
            f"  {e.name}: {e.role_capability.value}, {e.employment_type.value} "
            f"({e.contracted_hours}h/wk){lang_str}{housing}{driver} "
            f"avail {e.availability_start}–{e.availability_end}"
        )
    lines.append("")

    # ── Section 3: Day-by-day assignments (compact)
    lines.append("DAILY ASSIGNMENTS (employee=shift, OFF=day off, blank=unavailable):")
    days = sorted(demand_map.keys())
    for d in days:
        dd = demand_map[d]
        ship_str = ""
        if dd.ships_today:
            ship_names = [s.ship_name for s in dd.ships_today]
            good = " ⭐" if dd.has_good_ship else ""
            ship_str = f" | Ships: {', '.join(ship_names)}{good}"
        day_label = f"{d.strftime('%b %d')} ({d.strftime('%a')})"

        assignments_today = []
        for emp in sorted(employees, key=lambda e: e.name):
            if not (emp.availability_start <= d <= emp.availability_end):
                continue
            shift_id = assign_map.get((emp.id, d))
            if shift_id is None:
                continue  # not in schedule at all
            label = shift_id if shift_id != "off" else "OFF"
            assignments_today.append(f"{emp.name}={label}")

        lines.append(
            f"  {day_label} [prod≥{dd.production_needed} café≥{dd.cafe_needed}]{ship_str}"
        )
        lines.append(f"    {', '.join(assignments_today) if assignments_today else '(none)'}")

    lines.append("")

    # ── Section 4: Violations summary
    if violations:
        errors = [v for v in violations if v.severity == "error"]
        warnings = [v for v in violations if v.severity == "warning"]
        lines.append(f"VIOLATIONS ({len(errors)} errors, {len(warnings)} warnings):")
        for v in errors[:10]:
            date_str = f" on {v.date}" if v.date else ""
            lines.append(f"  ❌ {v.employee}{date_str}: {v.message}")
        for v in warnings[:10]:
            date_str = f" on {v.date}" if v.date else ""
            lines.append(f"  ⚠️ {v.employee}{date_str}: {v.message}")
        if len(violations) > 20:
            lines.append(f"  ... and {len(violations) - 20} more")
    else:
        lines.append("VIOLATIONS: None ✅")

    lines.append("")

    # ── Section 5: Per-employee hours summary
    if shift_hours:
        lines.append("HOURS SUMMARY:")
        for emp in sorted(employees, key=lambda e: e.name):
            total = sum(
                shift_hours.get(shift_id, 0)
                for (eid, d), shift_id in assign_map.items()
                if eid == emp.id and shift_id != "off"
            )
            contracted = emp.contracted_hours * 4.33  # approx monthly hours
            flag = " ⚠️ OVER-TARGET" if total > contracted * 1.05 else ""
            lines.append(f"  {emp.name}: {total:.0f}h (contracted ~{contracted:.0f}h/month){flag}")

    return "\n".join(lines)


# ── Prompt builders ───────────────────────────────────────────────────────────

def build_adjustment_prompt(user_request: str, schedule_context: str) -> list[dict]:
    """Build messages list for a natural-language adjustment request."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Here is the current schedule:\n\n{schedule_context}\n\n"
                f"User request: {user_request}"
            ),
        },
    ]


def build_explain_prompt(
    schedule: ScheduleRead,
    schedule_context: str,
    target_date: date | None = None,
    employee_name: str | None = None,
) -> list[dict]:
    """Build messages list to explain a specific schedule decision."""
    if target_date and employee_name:
        question = (
            f"Please explain why {employee_name} is scheduled the way they are "
            f"on {target_date.strftime('%B %d, %Y')}. "
            f"What constraints drove this assignment?"
        )
    elif target_date:
        question = (
            f"Please explain the staffing decisions for {target_date.strftime('%B %d, %Y')}. "
            f"Why are these specific employees assigned to these shifts?"
        )
    elif employee_name:
        question = (
            f"Please explain {employee_name}'s schedule for the month. "
            f"Why are they assigned to these particular shifts and days off?"
        )
    else:
        question = (
            "Please give an overview of the key scheduling decisions this month — "
            "which days are most challenging and why."
        )

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Here is the current schedule:\n\n{schedule_context}\n\n{question}",
        },
    ]


def build_validation_prompt(violations: list, schedule_context: str) -> list[dict]:
    """Build messages list to explain constraint violations in plain language."""
    if not violations:
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Here is the current schedule:\n\n{schedule_context}\n\n"
                    "There are no constraint violations. Please confirm that the schedule "
                    "looks reasonable and flag any potential issues I should be aware of."
                ),
            },
        ]

    violation_text = "\n".join(
        f"- [{v.severity.upper()}] {v.employee}"
        f"{f' on {v.date}' if v.date else ''}: {v.message}"
        for v in violations
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Here is the current schedule:\n\n{schedule_context}\n\n"
                f"The schedule has the following constraint violations:\n{violation_text}\n\n"
                "Please explain what these violations mean in plain language and suggest "
                "how to fix them."
            ),
        },
    ]
