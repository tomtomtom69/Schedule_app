"""End-to-end integration test — Phase 3 solver.

Dataset:
  • 8 employees: mix of café, production, both — 2 Eidsdal (1 driver)
  • August 2026 (31 peak-season days)
  • 5 cruise ship days including 1 good ship, 1 Hellesylt ship, 1 Spanish ship

Output:
  • Assignment grid (employee × day)
  • Staffing count per day vs demand
  • Constraint violations
  • Weekly hours per employee
  • Eidsdal car usage per day

Run from project root:
    python tests/test_solver_integration.py
    OR inside Docker:
    python /app/tests/test_solver_integration.py
"""
import math
import sys
import os
from datetime import date, time

# Ensure project root is on sys.path when running the file directly
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uuid

from src.demand.forecaster import generate_monthly_demand
from src.models.cruise_ship import CruiseShipRead
from src.models.employee import EmployeeRead
from src.models.enums import (
    EmploymentType,
    Housing,
    Port,
    RoleCapability,
    ShiftRole,
    ShipSize,
)
from src.models.shift_template import ShiftTemplateRead
from src.solver.scheduler import ScheduleGenerator
from src.solver.validator import validate_schedule

# ═══════════════════════════════════════════════════════════════════════════════
# Test data
# ═══════════════════════════════════════════════════════════════════════════════

YEAR, MONTH = 2026, 8   # August 2026 — peak season throughout


def _emp(name, role, emp_type=EmploymentType.full_time,
         housing=Housing.geiranger, licence=False, languages=None):
    langs = languages or ["english"]
    if "english" not in [l.lower() for l in langs]:
        langs = ["english"] + langs
    return EmployeeRead(
        id=uuid.uuid4(), name=name,
        languages=langs, role_capability=role,
        employment_type=emp_type,
        contracted_hours=37.5 if emp_type == EmploymentType.full_time else 20.0,
        housing=housing, driving_licence=licence,
        availability_start=date(2026, 5, 1),
        availability_end=date(2026, 10, 15),
    )


EMPLOYEES = [
    # ─── Café ────────────────────────────────────────────────────────────────
    _emp("Alice",   RoleCapability.cafe,       languages=["english", "spanish"]),    # Spanish speaker
    _emp("Bob",     RoleCapability.cafe,       languages=["english", "german"]),  # also German
    _emp("Carol",   RoleCapability.cafe,       emp_type=EmploymentType.part_time),
    # ─── Production ──────────────────────────────────────────────────────────
    _emp("Dave",    RoleCapability.production),
    _emp("Hans",    RoleCapability.production),
    # ─── Both (flex worker) ──────────────────────────────────────────────────
    _emp("Eva",     RoleCapability.both,       languages=["english", "german"]),     # German speaker
    # ─── Eidsdal (2 workers, 1 driver) ───────────────────────────────────────
    _emp("Felix",   RoleCapability.cafe,       housing=Housing.eidsdal, licence=True),   # driver
    _emp("Greta",   RoleCapability.cafe,       housing=Housing.eidsdal, licence=False,
         emp_type=EmploymentType.part_time),
]


def _ship(name, d, port, size=ShipSize.big, good=False, lang=None):
    return CruiseShipRead(
        id=uuid.uuid4(), ship_name=name, date=d,
        arrival_time=time(9, 0), departure_time=time(18, 0),
        port=port, size=size, good_ship=good, extra_language=lang,
    )


SHIPS = [
    _ship("Costa Luminosa",  date(2026, 8,  4), Port.geiranger_4B_SW),            # regular Geiranger
    _ship("MSC Euribia",     date(2026, 8,  7), Port.geiranger_3S,   good=True),  # good ship!
    _ship("AIDAcosma",       date(2026, 8, 12), Port.geiranger_4B_SW, lang="spanish"),  # language req
    _ship("Viking Mars",     date(2026, 8, 15), Port.hellesylt),                   # Hellesylt = 0.5
    _ship("Mein Schiff 1",   date(2026, 8, 20), Port.geiranger_4B_SW, lang="german"),  # language req
]

SHIP_LANGUAGE_MAP = {
    # Costa Luminosa omitted — no language guide required for this ship
    "MSC Euribia":    "spanish",   # good ship, Spanish speakers expected
    "Mein Schiff 1":  "german",    # German speakers required
    "Viking Mars":    "english",   # filtered out (everyone speaks English)
}

# All shift templates (from seed data)
SHIFTS = [
    ShiftTemplateRead(id="1",  role=ShiftRole.cafe,       label="VAKT SHOP 1", start_time=time(8, 0),  end_time=time(16, 0)),
    ShiftTemplateRead(id="2",  role=ShiftRole.cafe,       label="VAKT SHOP 2", start_time=time(9, 30), end_time=time(17, 30)),
    ShiftTemplateRead(id="3",  role=ShiftRole.cafe,       label="VAKT SHOP 3", start_time=time(11, 0), end_time=time(19, 0)),
    ShiftTemplateRead(id="4",  role=ShiftRole.cafe,       label="VAKT SHOP 4", start_time=time(12, 0), end_time=time(20, 0)),
    ShiftTemplateRead(id="5",  role=ShiftRole.cafe,       label="VAKT SHOP 5", start_time=time(13, 0), end_time=time(21, 0)),
    ShiftTemplateRead(id="6",  role=ShiftRole.cafe,       label="VAKT SHOP 6", start_time=time(10, 0), end_time=time(17, 0)),
    ShiftTemplateRead(id="P1", role=ShiftRole.production, label="PROD 1",      start_time=time(8, 0),  end_time=time(16, 0)),
    ShiftTemplateRead(id="P2", role=ShiftRole.production, label="PROD 2",      start_time=time(9, 30), end_time=time(17, 30)),
    ShiftTemplateRead(id="P3", role=ShiftRole.production, label="PROD 3",      start_time=time(11, 0), end_time=time(19, 0)),
    ShiftTemplateRead(id="P4", role=ShiftRole.production, label="PROD 4",      start_time=time(12, 0), end_time=time(20, 0)),
    ShiftTemplateRead(id="P5", role=ShiftRole.production, label="PROD 5",      start_time=time(13, 0), end_time=time(21, 0)),
]

SHIFT_LOOKUP = {s.id: s for s in SHIFTS}


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _shift_minutes(shift_id: str) -> int:
    s = SHIFT_LOOKUP.get(shift_id)
    if not s:
        return 0
    return (s.end_time.hour * 60 + s.end_time.minute) - (s.start_time.hour * 60 + s.start_time.minute)


def _sep(ch="═", width=80):
    print(ch * width)


def _header(title: str):
    _sep()
    print(f"  {title}")
    _sep("─")


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    print()
    _sep("═")
    print("  GEIRANGER SJOKOLADE — Schedule Integration Test")
    print(f"  August {YEAR}  |  {len(EMPLOYEES)} employees  |  {len(SHIPS)} cruise ship days")
    _sep("═")

    # ── Section 1: Employees ────────────────────────────────────────────────
    _header("EMPLOYEES")
    print(f"  {'Name':<10} {'Role':<12} {'Type':<10} {'Housing':<10} {'Licence':<8} {'Languages'}")
    print("  " + "─" * 68)
    for e in EMPLOYEES:
        print(f"  {e.name:<10} {e.role_capability.value:<12} {e.employment_type.value:<10} "
              f"{e.housing.value:<10} {'Yes' if e.driving_licence else 'No':<8} "
              f"{', '.join(e.languages)}")

    # ── Section 2: Cruise ships ─────────────────────────────────────────────
    _header("CRUISE SHIPS (August 2026)")
    print(f"  {'Date':<12} {'Ship':<18} {'Port':<18} {'Size':<8} {'Good':<6} {'Language'}")
    print("  " + "─" * 70)
    for s in SHIPS:
        lang = s.extra_language or SHIP_LANGUAGE_MAP.get(s.ship_name, "—")
        print(f"  {s.date.strftime('%b %d (%a)'):<12} {s.ship_name:<18} "
              f"{s.port.value:<18} {s.size.value:<8} {'Yes' if s.good_ship else 'No':<6} {lang}")

    # ── Section 3: Generate demand ──────────────────────────────────────────
    print()
    print("  Generating monthly demand …")
    demand = generate_monthly_demand(YEAR, MONTH, SHIPS, SHIP_LANGUAGE_MAP)
    print(f"  → {len(demand)} days in season (all 31 August days are peak)")

    _header("DAILY DEMAND PROFILE")
    print(f"  {'Date':<14} {'Season':<6} {'Prod':<6} {'Café':<6} {'Languages'}")
    print("  " + "─" * 52)
    for d in demand:
        langs = ", ".join(d.languages_required) if d.languages_required else "—"
        ship_flag = "🚢 " if d.has_cruise else "  "
        good_flag = "⭐" if d.has_good_ship else "  "
        helly = "(Hellesylt)" if d.hellesylt_ship_count > 0 else ""
        print(f"  {d.date.strftime('%b %d (%a)'):<14} {d.season.value:<6} "
              f"{d.production_needed:<6} {d.cafe_needed:<6} {langs:<16} "
              f"{ship_flag}{good_flag}{helly}")

    # ── Section 4: Solve ────────────────────────────────────────────────────
    print()
    print("  Building CP-SAT model and solving …")
    gen = ScheduleGenerator(EMPLOYEES, demand, SHIFTS)
    gen.build_model()
    schedule = gen.solve()

    if schedule is None:
        print()
        print("  ✗ SOLVER RETURNED NO FEASIBLE SOLUTION")
        print("    Check employee count vs peak demand requirements.")
        return

    print(f"  ✓ Feasible schedule found — {len(schedule.assignments)} total assignments")

    # Build indexes
    working = [a for a in schedule.assignments if not a.is_day_off]
    emp_lookup = {str(e.id): e for e in EMPLOYEES}
    demand_map = {d.date: d for d in demand}

    by_emp_date = {}   # emp_name → {day → shift_id}
    for a in working:
        name = emp_lookup[str(a.employee_id)].name
        by_emp_date.setdefault(name, {})[a.date] = a.shift_id

    days_in_month = sorted(demand_map.keys())

    # ── Section 5: Assignment grid ──────────────────────────────────────────
    _header("ASSIGNMENT GRID (August 2026)")

    # Header row: day numbers
    col_w = 4
    name_w = 10
    ship_days = {s.date for s in SHIPS}

    header_days = "".join(f"{d.day:>{col_w}}" for d in days_in_month)
    print(f"  {'Employee':<{name_w}} {header_days}")

    # Sub-header: weekday initials + ship marker
    wday_row = "".join(
        f"{'SMTWTFS'[d.weekday() if d.weekday() < 6 else 6]:>{col_w}}"
        for d in days_in_month
    )
    ship_row = "".join(
        f"{'🚢' if d in ship_days else '  ':>{col_w - 1}} "
        for d in days_in_month
    )
    print(f"  {'':<{name_w}} {wday_row}")
    print(f"  {'':<{name_w}} {ship_row}")
    print("  " + "─" * (name_w + 1 + col_w * len(days_in_month)))

    # Group employees: production first, then café
    prod_emps = [e for e in EMPLOYEES if e.role_capability == RoleCapability.production]
    cafe_emps = [e for e in EMPLOYEES if e.role_capability == RoleCapability.cafe]
    both_emps = [e for e in EMPLOYEES if e.role_capability == RoleCapability.both]
    ordered_emps = prod_emps + both_emps + cafe_emps

    for emp in ordered_emps:
        row = ""
        for d in days_in_month:
            sid = by_emp_date.get(emp.name, {}).get(d)
            cell = sid if sid else "·"
            row += f"{cell:>{col_w}}"
        cap_label = {"cafe": "C", "production": "P", "both": "B"}[emp.role_capability.value]
        eid_label = "E" if emp.housing == Housing.eidsdal else " "
        ft_label = "FT" if emp.employment_type == EmploymentType.full_time else "PT"
        print(f"  {emp.name:<{name_w}} {row}  [{cap_label}/{ft_label}{eid_label}]")

    print()
    print("  Legend: shift IDs 1-6=café, P1-P5=production, ·=day off")
    print("          [C=café P=production B=both / FT=full-time PT=part-time E=Eidsdal]")

    # ── Section 6: Staffing counts vs demand ────────────────────────────────
    _header("STAFFING COUNTS vs DEMAND")
    print(f"  {'Date':<14} {'P Need':<8} {'P Have':<8} {'C Need':<8} {'C Have':<8} {'Lang OK':<10} {'Status'}")
    print("  " + "─" * 68)

    by_date = {}
    for a in working:
        by_date.setdefault(a.date, []).append(a)

    all_ok = True
    for d in days_in_month:
        day_assigns = by_date.get(d, [])
        p_have = sum(1 for a in day_assigns if SHIFT_LOOKUP.get(a.shift_id) and SHIFT_LOOKUP[a.shift_id].role == ShiftRole.production)
        c_have = sum(1 for a in day_assigns if SHIFT_LOOKUP.get(a.shift_id) and SHIFT_LOOKUP[a.shift_id].role == ShiftRole.cafe)
        dem = demand_map[d]
        p_need = dem.production_needed
        c_need = dem.cafe_needed

        # Language check
        lang_ok = True
        for lang in dem.languages_required:
            covered = any(
                any(l.lower() == lang for l in emp_lookup[str(a.employee_id)].languages)
                and SHIFT_LOOKUP.get(a.shift_id)
                and SHIFT_LOOKUP[a.shift_id].role == ShiftRole.cafe
                for a in day_assigns
                if str(a.employee_id) in emp_lookup
            )
            if not covered:
                lang_ok = False

        p_ok = p_have >= p_need
        c_ok = c_have >= c_need
        ok = p_ok and c_ok and lang_ok
        if not ok:
            all_ok = False

        status = "✓" if ok else "✗ FAIL"
        langs = ", ".join(dem.languages_required) if dem.languages_required else "—"
        lang_str = ("✓" if lang_ok else "✗") + f" {langs}"
        ship_marker = " 🚢" if d in ship_days else "   "

        print(f"  {d.strftime('%b %d (%a)'):<14} {p_need:<8} {p_have:<8} "
              f"{c_need:<8} {c_have:<8} {lang_str:<20} {status}{ship_marker}")

    print()
    if all_ok:
        print("  ✓ All days meet staffing and language requirements")
    else:
        print("  ✗ Some days have unmet requirements (see ✗ rows above)")

    # ── Section 7: Constraint violations ────────────────────────────────────
    _header("CONSTRAINT VIOLATIONS")
    violations = validate_schedule(schedule, EMPLOYEES, demand, SHIFTS)
    errors = [v for v in violations if v.severity == "error"]
    warnings = [v for v in violations if v.severity == "warning"]

    if not violations:
        print("  ✓ No violations found — schedule is fully valid")
    else:
        if errors:
            print(f"  ERRORS ({len(errors)}):")
            for v in errors:
                date_str = f" [{v.date}]" if v.date else ""
                print(f"    ✗ [{v.constraint}] {v.employee}{date_str}: {v.message}")
        else:
            print("  ✓ No hard constraint errors")
        if warnings:
            print(f"\n  WARNINGS ({len(warnings)}):")
            for v in warnings:
                date_str = f" [{v.date}]" if v.date else ""
                print(f"    ⚠  [{v.constraint}] {v.employee}{date_str}: {v.message}")
        else:
            print("  ✓ No soft constraint warnings")

    # ── Section 8: Weekly hours per employee ────────────────────────────────
    _header("WEEKLY HOURS PER EMPLOYEE")

    # Determine ISO weeks in August 2026
    all_weeks = sorted({d.isocalendar()[:2] for d in days_in_month})
    week_labels = [f"W{w[1]}" for w in all_weeks]

    col = 7
    print(f"  {'Employee':<10} {'Role':<5} {'Type':<4} " +
          " ".join(f"{lbl:>{col}}" for lbl in week_labels) + f"  {'Total':>{col}}  {'Avg/wk':>{col}}")
    print("  " + "─" * (10 + 5 + 4 + col * len(all_weeks) + col + col + 10))

    for emp in ordered_emps:
        week_hours: dict[tuple, float] = {}
        total_minutes = 0
        for a in working:
            if str(a.employee_id) != str(emp.id):
                continue
            wk = a.date.isocalendar()[:2]
            mins = _shift_minutes(a.shift_id)
            week_hours[wk] = week_hours.get(wk, 0) + mins
            total_minutes += mins

        row = ""
        for wk in all_weeks:
            mins = week_hours.get(wk, 0)
            h = mins / 60
            flag = "!" if mins > 40 * 60 else " "
            row += f"  {h:>5.1f}{flag}"

        total_h = total_minutes / 60
        avg_h = total_h / len(all_weeks) if all_weeks else 0
        cap = emp.role_capability.value[0].upper()
        ft = "FT" if emp.employment_type == EmploymentType.full_time else "PT"
        print(f"  {emp.name:<10} {cap:<5} {ft:<4} {row}  {total_h:>{col}.1f}  {avg_h:>{col}.1f}")

    print()
    print("  Note: ! = week exceeds 40h normal limit (overtime zone, max 48h)")

    # ── Section 9: Eidsdal car usage ────────────────────────────────────────
    _header("EIDSDAL CAR USAGE PER DAY")
    eidsdal_emps = [e for e in EMPLOYEES if e.housing == Housing.eidsdal]
    eidsdal_ids = {str(e.id) for e in eidsdal_emps}
    driver_ids = {str(e.id) for e in eidsdal_emps if e.driving_licence}

    print(f"  Eidsdal employees: {', '.join(e.name for e in eidsdal_emps)}")
    print(f"  Drivers:           {', '.join(e.name for e in eidsdal_emps if e.driving_licence) or 'none'}")
    print()
    print(f"  {'Date':<14} {'Workers':<10} {'Cars':<6} {'Drivers':<10} {'Seats used':<12} {'OK'}")
    print("  " + "─" * 58)

    any_eidsdal_day = False
    for d in days_in_month:
        day_working = [a for a in working if a.date == d and str(a.employee_id) in eidsdal_ids]
        if not day_working:
            continue
        any_eidsdal_day = True
        n_workers = len(day_working)
        n_drivers = sum(1 for a in day_working if str(a.employee_id) in driver_ids)
        n_cars = math.ceil(n_workers / 5)
        drivers_needed = n_cars
        ok = n_drivers >= drivers_needed and n_workers <= 10
        names = ", ".join(emp_lookup[str(a.employee_id)].name for a in day_working)
        seats = f"{n_workers}/{n_cars * 5}"
        print(f"  {d.strftime('%b %d (%a)'):<14} {names:<10} {n_cars:<6} "
              f"{n_drivers}/{drivers_needed} needed  {seats:<12} {'✓' if ok else '✗ FAIL'}")

    if not any_eidsdal_day:
        print("  (No Eidsdal employees scheduled this month)")

    # ── Summary ─────────────────────────────────────────────────────────────
    _sep()
    total_working = len(working)
    total_off = len([a for a in schedule.assignments if a.is_day_off])
    error_count = len(errors)
    print(f"  SUMMARY: {total_working} working assignments, {total_off} day-off records")
    print(f"           {len(errors)} hard errors, {len(warnings)} warnings")
    print(f"           Solver status: {'FEASIBLE ✓' if schedule else 'INFEASIBLE ✗'}")
    _sep()
    print()


if __name__ == "__main__":
    main()
