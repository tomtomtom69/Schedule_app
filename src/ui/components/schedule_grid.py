"""Schedule grid component — Phase 4.

Renders the monthly schedule as an HTML table matching the Excel spreadsheet layout:
  - Rows grouped by PRODUCTION then CAFÉ
  - Columns are days of the month
  - Shift legend below the main grid
  - Cruise info rows at the bottom
  - Color-coded cells
"""
from __future__ import annotations

from datetime import date
from typing import Any

from src.demand.forecaster import DailyDemand
from src.models.employee import EmployeeRead
from src.models.schedule import AssignmentRead, ScheduleRead
from src.models.shift_template import ShiftTemplateRead

# ── Cell colors ─────────────────────────────────────────────────────────────
COLOR_DAY_OFF = "#FFB3B3"        # red — confirmed day off / FT unscheduled
COLOR_WORKING = "#B3FFB3"        # light green — FT assigned shift
COLOR_PT_AVAILABLE = "#FFD699"   # orange — part-time available (scheduled or not)
COLOR_NOT_AVAILABLE = "#F0F0F0"  # light grey — outside availability window
COLOR_HEADER = "#D0D0D0"         # section header rows

# Keep backward-compat alias
COLOR_PT_UNSCHEDULED = COLOR_PT_AVAILABLE


def _build_lookup(schedule: ScheduleRead) -> dict[tuple, str]:
    """Return {(employee_id, date): shift_id} for quick lookup."""
    return {(a.employee_id, a.date): a.shift_id for a in schedule.assignments}


def _cell_color(shift_id: str | None, is_part_time: bool, is_available: bool) -> str:
    if not is_available:
        return COLOR_NOT_AVAILABLE
    if shift_id == "off":
        return COLOR_DAY_OFF          # explicit day off — red for everyone
    if shift_id is None:
        # Available but unscheduled
        return COLOR_PT_AVAILABLE if is_part_time else COLOR_DAY_OFF
    # Assigned to a real shift
    return COLOR_PT_AVAILABLE if is_part_time else COLOR_WORKING


def _employee_row_html(
    emp: EmployeeRead,
    days: list[date],
    lookup: dict[tuple, str],
    eidsdal_ids: set,
) -> str:
    is_eidsdal = emp.id in eidsdal_ids
    is_pt = emp.employment_type == "part_time"

    name_display = emp.name
    if is_eidsdal:
        name_display += " 🏔"
    if emp.driving_licence:
        name_display += " 🚗"

    name_bg = "#FFF3E0" if is_eidsdal else "#F8F8F8"
    cells = (
        f"<td style='padding:2px 8px; background:{name_bg}; "
        f"font-weight:bold; white-space:nowrap; border:1px solid #ccc;'>"
        f"{name_display}</td>"
    )

    for d in days:
        is_available = emp.availability_start <= d <= emp.availability_end
        shift_id = lookup.get((emp.id, d)) if is_available else None

        color = _cell_color(shift_id, is_pt, is_available)
        label = shift_id if (shift_id and shift_id != "off") else ""

        cells += (
            f"<td style='background:{color}; padding:2px 4px; text-align:center; "
            f"border:1px solid #ccc; min-width:30px; font-size:11px;'>{label}</td>"
        )

    return f"<tr>{cells}</tr>"


def _section_header_html(label: str, num_cols: int) -> str:
    return (
        f"<tr><td colspan='{num_cols}' style='background:{COLOR_HEADER}; "
        f"font-weight:bold; font-size:13px; padding:5px 8px; "
        f"border-top:2px solid #888;'>{label}</td></tr>"
    )


def _cruise_info_rows_html(days: list[date], demand: list[DailyDemand], num_cols: int) -> str:
    demand_map = {d.date: d for d in demand}
    rows_html = _section_header_html("CRUISE INFO", num_cols)

    labels = ["Ships", "Port", "Count", "Arrival", "Depart"]
    for lbl in labels:
        cells = (
            f"<td style='padding:2px 8px; font-weight:bold; white-space:nowrap; "
            f"background:#C8E6FA; border:1px solid #ccc; font-size:11px;'>{lbl}</td>"
        )
        for d in days:
            dd = demand_map.get(d)
            style = (
                "background:#E8F4FD; padding:2px 4px; text-align:center; "
                "border:1px solid #ccc; font-size:10px; white-space:nowrap;"
            )
            content = ""
            if dd and dd.ships_today:
                if lbl == "Ships":
                    content = "<br>".join(s.ship_name[:14] for s in dd.ships_today)
                    if dd.has_good_ship:
                        content += " ⭐"
                elif lbl == "Port":
                    content = "<br>".join(str(s.port).split("_")[0] for s in dd.ships_today)
                elif lbl == "Count":
                    content = str(len(dd.ships_today))
                elif lbl == "Arrival":
                    content = "<br>".join(str(s.arrival_time)[:5] for s in dd.ships_today)
                elif lbl == "Depart":
                    content = "<br>".join(str(s.departure_time)[:5] for s in dd.ships_today)
            cells += f"<td style='{style}'>{content}</td>"
        rows_html += f"<tr>{cells}</tr>"

    return rows_html


def _legend_html(shifts: list[ShiftTemplateRead]) -> str:
    """Compact vertical legend for the right pane (one shift per row)."""
    rows = ""
    for s in sorted(shifts, key=lambda x: (x.role, x.id)):
        start = str(s.start_time)[:5]
        end = str(s.end_time)[:5]
        bg = "#E8F0FF" if s.role == "cafe" else "#F0E8FF"
        badge_bg = "#4A90D9" if s.role == "cafe" else "#7B68EE"
        rows += (
            f"<tr><td style='background:{bg}; padding:2px 8px; "
            f"white-space:nowrap; font-size:11px; border:1px solid #ddd;'>"
            f"<span style='background:{badge_bg};color:white;border-radius:3px;"
            f"padding:0 3px;font-size:9px;'>{'C' if s.role == 'cafe' else 'P'}</span> "
            f"<b>{s.id}</b> {start}–{end}</td></tr>"
        )
    return f"<table style='border-collapse:collapse; margin-top:2px;'>{rows}</table>"


def build_schedule_html(
    schedule: ScheduleRead,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    demand: list[DailyDemand],
) -> str:
    """Build a full HTML representation of the schedule."""
    days = sorted({d.date for d in demand})
    if not days:
        return "<p>No days in demand profile.</p>"

    lookup = _build_lookup(schedule)
    eidsdal_ids = {e.id for e in employees if e.housing == "eidsdal"}

    # Separate employees by role
    prod_emps = sorted(
        [e for e in employees if e.role_capability in ("production", "both")],
        key=lambda e: (e.housing == "eidsdal", e.name),
    )
    cafe_emps = sorted(
        [e for e in employees if e.role_capability == "cafe"],
        key=lambda e: (e.housing == "eidsdal", e.name),
    )

    num_cols = len(days) + 1  # +1 for name column

    # Header row
    th = "<th style='padding:4px 8px; text-align:left; background:#4A90D9; color:white; white-space:nowrap;'>Employee</th>"
    for d in days:
        is_weekend = d.weekday() >= 5
        bg = "#2E6DB4" if is_weekend else "#4A90D9"
        th += (
            f"<th style='padding:3px 2px; text-align:center; background:{bg}; "
            f"color:white; font-size:10px; min-width:28px; border:1px solid #3A7BC8;'>"
            f"{d.strftime('%a')}<br>{d.day}</th>"
        )
    header_row = f"<tr>{th}</tr>"

    # Build table body
    tbody = ""
    if prod_emps:
        tbody += _section_header_html("PRODUCTION", num_cols)
        for emp in prod_emps:
            tbody += _employee_row_html(emp, days, lookup, eidsdal_ids)
    if cafe_emps:
        tbody += _section_header_html("CAFÉ", num_cols)
        for emp in cafe_emps:
            tbody += _employee_row_html(emp, days, lookup, eidsdal_ids)

    tbody += _cruise_info_rows_html(days, demand, num_cols)

    table_style = (
        "border-collapse:collapse; font-family:'Segoe UI',monospace; "
        "font-size:12px; width:100%;"
    )
    main_table = (
        f"<table style='{table_style}'>"
        f"<thead>{header_row}</thead>"
        f"<tbody>{tbody}</tbody>"
        f"</table>"
    )

    legend = _legend_html(shifts)
    color_key = (
        "<div style='margin-top:8px; font-size:11px;'>"
        "<b>Colors:</b><br>"
        f"<span style='background:{COLOR_WORKING};padding:1px 6px;border-radius:3px;'>FT Working</span> "
        f"<span style='background:{COLOR_DAY_OFF};padding:1px 6px;border-radius:3px;'>Day Off / Unscheduled</span> "
        f"<span style='background:{COLOR_PT_AVAILABLE};padding:1px 6px;border-radius:3px;'>PT Available (code = booked)</span> "
        f"<span style='background:{COLOR_NOT_AVAILABLE};padding:1px 6px;border-radius:3px;'>Not in Season</span>"
        "<br><span>🏔 Eidsdal &nbsp; 🚗 Driver &nbsp; ⭐ Good Ship</span>"
        "</div>"
    )

    right_pane = (
        f"<div style='flex-shrink:0; width:175px;'>"
        f"<div style='font-weight:bold; font-size:12px; margin-bottom:2px;'>Shift Legend</div>"
        f"{legend}"
        f"</div>"
    )

    return (
        f"<div style='display:flex; gap:12px; align-items:flex-start; width:100%;'>"
        f"<div style='flex:1; min-width:0; overflow-x:auto;'>"
        f"{main_table}"
        f"{color_key}"
        f"</div>"
        f"{right_pane}"
        f"</div>"
    )


def render_schedule_grid(
    schedule: ScheduleRead,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
    demand: list[DailyDemand],
    height: int = 600,
) -> None:
    """Render the schedule grid in Streamlit."""
    import streamlit.components.v1 as components
    html = build_schedule_html(schedule, employees, shifts, demand)
    components.html(html, height=height, scrolling=True)
