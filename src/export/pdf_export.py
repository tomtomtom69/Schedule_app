"""PDF export — Phase 6.

Generates a landscape PDF of the monthly schedule using ReportLab.
Includes:
  - Schedule grid (two halves stacked)
  - Shift legend
  - Summary statistics page
"""
from __future__ import annotations

import calendar
import io
from datetime import date

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from src.demand.forecaster import DailyDemand
from src.models.employee import EmployeeRead
from src.models.schedule import ScheduleRead
from src.models.shift_template import ShiftTemplateRead

# ── Colors ────────────────────────────────────────────────────────────────────
C_DAY_OFF     = colors.HexColor("#FFB3B3")
C_PT_AVAIL    = colors.HexColor("#FFD699")
C_WORKING     = colors.HexColor("#B3FFB3")
C_HEADER_DAY  = colors.HexColor("#D9E1F2")
C_WEEKEND_HDR = colors.HexColor("#C0D0F0")
C_PROD_HDR    = colors.HexColor("#F4B084")
C_CAFE_HDR    = colors.HexColor("#9BC2E6")
C_CRUISE_HDR  = colors.HexColor("#C8E6FA")
C_CRUISE_ROW  = colors.HexColor("#E8F4FD")
C_TITLE       = colors.HexColor("#4A90D9")
C_WHITE       = colors.white
C_GREY        = colors.HexColor("#EEEEEE")
C_OT          = colors.HexColor("#FFB3B3")

PAGE_W, PAGE_H = landscape(A4)
MARGIN = 10 * mm


def _shift_hours(s: ShiftTemplateRead) -> float:
    sm = s.start_time.hour * 60 + s.start_time.minute
    em = s.end_time.hour * 60 + s.end_time.minute
    return (em - sm) / 60.0


def _cell(text: str, bold: bool = False, size: int = 7, align: str = "CENTER") -> Paragraph:
    style = ParagraphStyle(
        "cell",
        fontSize=size,
        fontName="Helvetica-Bold" if bold else "Helvetica",
        alignment=TA_CENTER if align == "CENTER" else TA_LEFT,
        leading=size + 1,
        wordWrap="CJK",
    )
    return Paragraph(str(text), style)


# ── Main export function ─────────────────────────────────────────────────────

def export_schedule_to_pdf(
    schedule: ScheduleRead,
    employees: list[EmployeeRead],
    demand: list[DailyDemand],
    shift_templates: list[ShiftTemplateRead],
) -> bytes:
    """Generate landscape PDF. Returns bytes ready for download."""
    month = schedule.month
    year = schedule.year
    month_name = calendar.month_name[month]
    _, days_in_month = calendar.monthrange(year, month)

    demand_map = {d.date: d for d in demand}
    assign_map: dict[tuple, str] = {
        (a.employee_id, a.date): a.shift_id for a in schedule.assignments
    }

    all_days = [date(year, month, d) for d in range(1, days_in_month + 1)]
    half1 = [d for d in all_days if d.day <= 15]
    half2 = [d for d in all_days if d.day > 15]

    prod_emps = sorted(
        [e for e in employees if e.role_capability in ("production", "both")],
        key=lambda e: (e.housing == "eidsdal", e.name),
    )
    cafe_emps = sorted(
        [e for e in employees if e.role_capability == "cafe"],
        key=lambda e: (e.housing == "eidsdal", e.name),
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "title", fontSize=14, fontName="Helvetica-Bold",
        textColor=C_WHITE, alignment=TA_LEFT,
    )
    subtitle_style = ParagraphStyle(
        "subtitle", fontSize=10, fontName="Helvetica-Bold",
        textColor=C_WHITE, alignment=TA_LEFT,
    )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN,
    )

    story = []

    # ── Title ─────────────────────────────────────────────────────────────────
    title_table = Table(
        [[Paragraph(f"Geiranger Sjokolade — Vaktplan {month_name} {year}", title_style)]],
        colWidths=[PAGE_W - 2 * MARGIN],
    )
    title_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), C_TITLE),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(title_table)
    story.append(Spacer(1, 4 * mm))

    # ── Schedule grid (two halves) ────────────────────────────────────────────
    available_w = PAGE_W - 2 * MARGIN

    for half_idx, days in enumerate([half1, half2]):
        if not days:
            continue
        tbl = _build_grid_table(days, prod_emps, cafe_emps, demand_map, assign_map, available_w)
        story.append(tbl)
        story.append(Spacer(1, 6 * mm))

    # ── Shift legend ──────────────────────────────────────────────────────────
    legend_tbl = _build_legend_table(shift_templates, available_w)
    story.append(legend_tbl)

    # ── Summary page ──────────────────────────────────────────────────────────
    story.append(PageBreak())
    story.append(Paragraph(
        f"Employee Summary — {month_name} {year}",
        ParagraphStyle("sum_title", fontSize=12, fontName="Helvetica-Bold",
                       textColor=C_TITLE, alignment=TA_LEFT),
    ))
    story.append(Spacer(1, 4 * mm))
    summary_tbl = _build_summary_table(schedule, employees, shift_templates, available_w)
    story.append(summary_tbl)

    doc.build(story)
    return buf.getvalue()


# ── Grid table builder ────────────────────────────────────────────────────────

def _build_grid_table(
    days: list[date],
    prod_emps: list[EmployeeRead],
    cafe_emps: list[EmployeeRead],
    demand_map: dict,
    assign_map: dict,
    available_w: float,
) -> Table:
    """Build one half-month grid as a ReportLab Table."""
    n = len(days)
    name_w = 28 * mm
    day_w = (available_w - name_w) / n

    rows: list[list] = []
    style_cmds: list[tuple] = []

    def row_idx() -> int:
        return len(rows)

    # ── Day header: weekday names
    hdr_row = [_cell("", bold=True)] + [
        _cell(d.strftime("%a"), bold=True, size=7) for d in days
    ]
    rows.append(hdr_row)
    style_cmds += [
        ("BACKGROUND", (0, row_idx() - 1), (-1, row_idx() - 1), C_HEADER_DAY),
        ("FONTNAME", (0, row_idx() - 1), (-1, row_idx() - 1), "Helvetica-Bold"),
    ]
    for i, d in enumerate(days):
        if d.weekday() >= 5:
            style_cmds.append(("BACKGROUND", (i + 1, row_idx() - 1), (i + 1, row_idx() - 1), C_WEEKEND_HDR))

    # ── Day header: day numbers
    num_row = [_cell("")] + [_cell(str(d.day), bold=True, size=8) for d in days]
    rows.append(num_row)
    style_cmds += [
        ("BACKGROUND", (0, row_idx() - 1), (-1, row_idx() - 1), C_HEADER_DAY),
    ]
    for i, d in enumerate(days):
        if d.weekday() >= 5:
            style_cmds.append(("BACKGROUND", (i + 1, row_idx() - 1), (i + 1, row_idx() - 1), C_WEEKEND_HDR))

    # ── PRODUCTION section
    _add_section_header(rows, style_cmds, "PRODUCTION", n, C_PROD_HDR)
    for emp in prod_emps:
        _add_employee_row(rows, style_cmds, emp, days, assign_map)

    # ── CAFÉ section
    _add_section_header(rows, style_cmds, "CAFÉ", n, C_CAFE_HDR)
    for emp in cafe_emps:
        _add_employee_row(rows, style_cmds, emp, days, assign_map)

    # ── Cruise info
    rows.append([_cell("")] * (n + 1))  # spacer
    _add_cruise_section(rows, style_cmds, days, demand_map, n)

    # ── Global style
    style_cmds += [
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#CCCCCC")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white]),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
    ]

    col_widths = [name_w] + [day_w] * n
    tbl = Table(rows, colWidths=col_widths, repeatRows=2)
    tbl.setStyle(TableStyle(style_cmds))
    return tbl


def _add_section_header(rows, style_cmds, label, n, color):
    ri = len(rows)
    rows.append([_cell(label, bold=True, align="LEFT")] + [_cell("")] * n)
    style_cmds.append(("BACKGROUND", (0, ri), (-1, ri), color))
    style_cmds.append(("SPAN", (0, ri), (-1, ri)))


def _add_employee_row(rows, style_cmds, emp, days, assign_map):
    ri = len(rows)
    is_eidsdal = emp.housing == "eidsdal"
    is_pt = emp.employment_type == "part_time"
    name = emp.name + (" 🏔" if is_eidsdal else "") + (" 🚗" if emp.driving_licence else "")

    cells = [_cell(name, bold=True, align="LEFT")]
    for col_idx, d in enumerate(days):
        is_avail = emp.availability_start <= d <= emp.availability_end
        if not is_avail:
            cells.append(_cell(""))
            continue
        shift_id = assign_map.get((emp.id, d))
        if shift_id is None or shift_id == "off":
            cells.append(_cell(""))
            bg = C_PT_AVAIL if is_pt else C_DAY_OFF
        else:
            cells.append(_cell(shift_id, bold=True))
            bg = C_WORKING
        style_cmds.append(("BACKGROUND", (col_idx + 1, ri), (col_idx + 1, ri), bg))

    rows.append(cells)
    if is_eidsdal:
        style_cmds.append(("BACKGROUND", (0, ri), (0, ri), colors.HexColor("#FFF3E0")))


def _add_cruise_section(rows, style_cmds, days, demand_map, n):
    ri = len(rows)
    rows.append([_cell("CRUISE INFO", bold=True, align="LEFT")] + [_cell("")] * n)
    style_cmds.append(("BACKGROUND", (0, ri), (-1, ri), C_CRUISE_HDR))

    for lbl, field in [("Ships", "ships"), ("Arrival", "arrival")]:
        ri = len(rows)
        row_cells = [_cell(lbl, bold=True, align="LEFT")]
        for d in days:
            dd = demand_map.get(d)
            if dd and dd.ships_today:
                if field == "ships":
                    val = ", ".join(s.ship_name[:12] for s in dd.ships_today)
                    if dd.has_good_ship:
                        val += " ⭐"
                else:
                    val = ", ".join(str(s.arrival_time)[:5] for s in dd.ships_today)
                row_cells.append(_cell(val, size=6))
                style_cmds.append(("BACKGROUND", (len(row_cells) - 1, ri), (len(row_cells) - 1, ri), C_CRUISE_ROW))
            else:
                row_cells.append(_cell(""))
        rows.append(row_cells)
        style_cmds.append(("BACKGROUND", (0, ri), (0, ri), C_CRUISE_HDR))


# ── Legend table ──────────────────────────────────────────────────────────────

def _build_legend_table(shifts: list[ShiftTemplateRead], available_w: float) -> Table:
    col_w = available_w / 4
    cafe_shifts = sorted([s for s in shifts if s.role == "cafe"], key=lambda x: x.id)
    prod_shifts = sorted([s for s in shifts if s.role == "production"], key=lambda x: x.id)

    legend_style = ParagraphStyle("leg", fontSize=8, fontName="Helvetica")

    def shift_cell(s: ShiftTemplateRead) -> Paragraph:
        start = str(s.start_time)[:5]
        end = str(s.end_time)[:5]
        return Paragraph(
            f"<b>{s.id}</b> — {s.label}  {start}–{end}  ({_shift_hours(s):.1f}h)",
            legend_style,
        )

    header_row = [
        _cell("CAFÉ SHIFTS", bold=True),
        _cell(""),
        _cell("PRODUCTION SHIFTS", bold=True),
        _cell(""),
    ]

    max_rows = max(len(cafe_shifts), len(prod_shifts))
    data = [header_row]
    for i in range(max_rows):
        c1 = shift_cell(cafe_shifts[i]) if i < len(cafe_shifts) else _cell("")
        c3 = shift_cell(prod_shifts[i]) if i < len(prod_shifts) else _cell("")
        data.append([c1, _cell(""), c3, _cell("")])

    tbl = Table(data, colWidths=[col_w] * 4)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (1, 0), C_CAFE_HDR),
        ("BACKGROUND", (2, 0), (3, 0), C_PROD_HDR),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#CCCCCC")),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return tbl


# ── Summary table ─────────────────────────────────────────────────────────────

def _build_summary_table(
    schedule: ScheduleRead,
    employees: list[EmployeeRead],
    shift_templates: list[ShiftTemplateRead],
    available_w: float,
) -> Table:
    shift_h = {s.id: _shift_hours(s) for s in shift_templates}
    assign_map: dict[tuple, str] = {
        (a.employee_id, a.date): a.shift_id for a in schedule.assignments
    }

    headers = ["Employee", "Role", "Type", "Days Worked", "Days Off",
               "Total Hours", "Contracted (est.)", "Overtime"]
    col_widths = [30 * mm, 22 * mm, 20 * mm, 18 * mm, 16 * mm,
                  20 * mm, 25 * mm, 18 * mm]

    data = [[_cell(h, bold=True) for h in headers]]
    style_cmds: list[tuple] = [
        ("BACKGROUND", (0, 0), (-1, 0), C_HEADER_DAY),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#CCCCCC")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]

    for emp in sorted(employees, key=lambda e: (e.role_capability, e.name)):
        total_h = sum(
            shift_h.get(sid, 0)
            for (eid, _), sid in assign_map.items()
            if eid == emp.id and sid != "off"
        )
        days_worked = sum(1 for (eid, _), sid in assign_map.items() if eid == emp.id and sid != "off")
        days_off = sum(1 for (eid, _), sid in assign_map.items() if eid == emp.id and sid == "off")
        contracted = emp.contracted_hours * 4.33
        overtime = max(0.0, total_h - contracted)

        ri = len(data)
        row = [
            _cell(emp.name, bold=(overtime > 0), align="LEFT"),
            _cell(emp.role_capability),
            _cell(emp.employment_type),
            _cell(str(days_worked)),
            _cell(str(days_off)),
            _cell(f"{total_h:.1f}h"),
            _cell(f"{contracted:.0f}h"),
            _cell(f"{overtime:.1f}h" if overtime > 0 else "—"),
        ]
        data.append(row)
        if overtime > 0:
            style_cmds.append(("BACKGROUND", (0, ri), (-1, ri), C_OT))

    tbl = Table(data, colWidths=col_widths)
    tbl.setStyle(TableStyle(style_cmds))
    return tbl
