"""Export page — Phase 6: Excel/PDF download, validation dashboard, coverage heatmap."""
from __future__ import annotations

import calendar
from datetime import date, datetime

import pandas as pd
import streamlit as st

from src.db.database import db_session
from src.demand.forecaster import DailyDemand, generate_monthly_demand
from src.models.cruise_ship import CruiseShipORM, CruiseShipRead, ShipLanguageORM
from src.models.employee import EmployeeORM, EmployeeRead
from src.models.enums import ScheduleStatus
from src.models.schedule import AssignmentORM, AssignmentRead, ScheduleORM, ScheduleRead
from src.models.shift_template import ShiftTemplateORM, ShiftTemplateRead
from src.solver import validate_schedule

st.set_page_config(page_title="Export", page_icon="📤", layout="wide")
st.title("📤 Export & Validation")

SEASON_MONTHS = {5: "May", 6: "June", 7: "July", 8: "August", 9: "September", 10: "October"}


# ── Data loaders ─────────────────────────────────────────────────────────────

@st.cache_data(ttl=60)
def _load_employees() -> list[EmployeeRead]:
    with db_session() as db:
        rows = db.query(EmployeeORM).order_by(EmployeeORM.name).all()
        return [
            EmployeeRead(
                id=r.id, name=r.name, languages=r.languages,
                role_capability=r.role_capability, employment_type=r.employment_type,
                contracted_hours=r.contracted_hours, housing=r.housing,
                driving_licence=r.driving_licence,
                availability_start=r.availability_start,
                availability_end=r.availability_end,
                preferences=r.preferences,
            )
            for r in rows
        ]


@st.cache_data(ttl=60)
def _load_shifts() -> list[ShiftTemplateRead]:
    with db_session() as db:
        rows = db.query(ShiftTemplateORM).order_by(ShiftTemplateORM.id).all()
        return [
            ShiftTemplateRead(id=r.id, role=r.role, label=r.label,
                              start_time=r.start_time, end_time=r.end_time)
            for r in rows
        ]


def _load_schedule(year: int, month: int) -> ScheduleRead | None:
    try:
        with db_session() as db:
            orm = (
                db.query(ScheduleORM)
                .filter_by(year=year, month=month)
                .order_by(ScheduleORM.created_at.desc())
                .first()
            )
            if not orm:
                return None
            assignments = [
                AssignmentRead(
                    id=a.id, schedule_id=a.schedule_id,
                    employee_id=a.employee_id, date=a.date,
                    shift_id=a.shift_id, is_day_off=a.is_day_off, notes=a.notes,
                )
                for a in orm.assignments
            ]
            return ScheduleRead(
                id=orm.id, month=orm.month, year=orm.year,
                status=ScheduleStatus(orm.status),
                created_at=orm.created_at, modified_at=orm.modified_at,
                assignments=assignments,
            )
    except Exception:
        return None


def _load_ships(year: int, month: int) -> tuple[list[CruiseShipRead], dict[str, str]]:
    _, last_day = calendar.monthrange(year, month)
    with db_session() as db:
        rows = db.query(CruiseShipORM).filter(
            CruiseShipORM.date >= date(year, month, 1),
            CruiseShipORM.date <= date(year, month, last_day),
        ).all()
        lang_map = {r.ship_name: r.primary_language for r in db.query(ShipLanguageORM).all()}
        return [
            CruiseShipRead(
                id=r.id, ship_name=r.ship_name, date=r.date,
                arrival_time=r.arrival_time, departure_time=r.departure_time,
                port=r.port, size=r.size, good_ship=r.good_ship,
                extra_language=r.extra_language,
            )
            for r in rows
        ], lang_map


# ── Month selector ────────────────────────────────────────────────────────────

col_year, col_month, col_load = st.columns([1, 1, 2])
with col_year:
    sel_year = st.selectbox("Year", [2026, 2027], index=0)
with col_month:
    sel_month = st.selectbox("Month", list(SEASON_MONTHS.keys()),
                             format_func=lambda m: SEASON_MONTHS[m], index=3)
with col_load:
    st.write("")
    load_clicked = st.button("📂 Load Schedule", type="primary")

if load_clicked or "export_schedule" not in st.session_state:
    schedule = _load_schedule(sel_year, sel_month)
    if schedule:
        st.session_state["export_schedule"] = schedule
        st.session_state["export_year"] = sel_year
        st.session_state["export_month"] = sel_month
    else:
        st.session_state.pop("export_schedule", None)

schedule: ScheduleRead | None = st.session_state.get("export_schedule")
cached_year = st.session_state.get("export_year")
cached_month = st.session_state.get("export_month")

if not schedule or cached_year != sel_year or cached_month != sel_month:
    if load_clicked:
        st.warning(f"No saved schedule found for {SEASON_MONTHS[sel_month]} {sel_year}. "
                   "Generate one on the Schedule page first.")
    else:
        st.info("Select a month and click **Load Schedule**.")
    st.stop()

# ── Load supporting data ──────────────────────────────────────────────────────
try:
    employees = _load_employees()
    shifts = _load_shifts()
    ships, lang_map = _load_ships(sel_year, sel_month)
    demand = generate_monthly_demand(sel_year, sel_month, ships, lang_map)
except Exception as e:
    st.error(f"Failed to load data: {e}")
    st.stop()

st.success(
    f"Loaded: **{SEASON_MONTHS[sel_month]} {sel_year}** — "
    f"status: {schedule.status.value.upper()} | "
    f"created: {schedule.created_at.strftime('%Y-%m-%d %H:%M')}"
)

tab_export, tab_validation, tab_summary, tab_heatmap = st.tabs(
    ["📥 Download", "⚠️ Validation", "📊 Employee Summary", "🌡️ Coverage Heatmap"]
)

# ── Tab 1: Download ───────────────────────────────────────────────────────────
with tab_export:
    col1, col2 = st.columns(2)

    with col1:
        st.subheader("📊 Excel Export")
        st.caption("Matches the Vaktlista spreadsheet format — two half-month blocks with colors.")
        if st.button("Generate Excel", key="gen_excel"):
            with st.spinner("Building Excel workbook…"):
                try:
                    from src.export.excel_export import export_schedule_to_excel
                    xlsx_bytes = export_schedule_to_excel(schedule, employees, demand, shifts)
                    fname = f"Vaktplan_{SEASON_MONTHS[sel_month]}_{sel_year}.xlsx"
                    st.download_button(
                        label="📥 Download .xlsx",
                        data=xlsx_bytes,
                        file_name=fname,
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        type="primary",
                    )
                    st.success(f"Ready: {fname} ({len(xlsx_bytes) // 1024} KB)")
                except Exception as e:
                    st.error(f"Excel export failed: {e}")

    with col2:
        st.subheader("📄 PDF Export")
        st.caption("Landscape A4 — schedule grid + shift legend + summary page.")
        if st.button("Generate PDF", key="gen_pdf"):
            with st.spinner("Building PDF…"):
                try:
                    from src.export.pdf_export import export_schedule_to_pdf
                    pdf_bytes = export_schedule_to_pdf(schedule, employees, demand, shifts)
                    fname = f"Vaktplan_{SEASON_MONTHS[sel_month]}_{sel_year}.pdf"
                    st.download_button(
                        label="📥 Download .pdf",
                        data=pdf_bytes,
                        file_name=fname,
                        mime="application/pdf",
                        type="primary",
                    )
                    st.success(f"Ready: {fname} ({len(pdf_bytes) // 1024} KB)")
                except Exception as e:
                    st.error(f"PDF export failed: {e}")

    st.divider()
    st.subheader("🗑️ Data Management")
    col_del, col_backup = st.columns(2)

    with col_del:
        if st.button("Delete This Schedule", type="secondary"):
            if st.session_state.get("confirm_del_sched") == f"{sel_year}-{sel_month}":
                try:
                    with db_session() as db:
                        orm = db.query(ScheduleORM).filter_by(
                            year=sel_year, month=sel_month
                        ).first()
                        if orm:
                            db.delete(orm)
                    st.session_state.pop("export_schedule", None)
                    st.session_state.pop("confirm_del_sched", None)
                    st.success("Schedule deleted.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Delete failed: {e}")
            else:
                st.session_state["confirm_del_sched"] = f"{sel_year}-{sel_month}"
                st.warning("Click again to confirm deletion.")

    with col_backup:
        if st.button("Export All Data (JSON backup)", type="secondary"):
            import json
            try:
                backup = {
                    "schedule": {
                        "id": str(schedule.id),
                        "year": schedule.year,
                        "month": schedule.month,
                        "status": schedule.status.value,
                        "assignments": [
                            {
                                "employee_id": str(a.employee_id),
                                "date": str(a.date),
                                "shift_id": a.shift_id,
                                "is_day_off": a.is_day_off,
                            }
                            for a in schedule.assignments
                        ],
                    },
                    "employees": [
                        {
                            "name": e.name,
                            "role": e.role_capability.value if hasattr(e.role_capability, 'value') else e.role_capability,
                            "type": e.employment_type.value if hasattr(e.employment_type, 'value') else e.employment_type,
                            "hours": e.contracted_hours,
                        }
                        for e in employees
                    ],
                }
                json_str = json.dumps(backup, indent=2, default=str)
                st.download_button(
                    "📥 Download JSON",
                    data=json_str.encode(),
                    file_name=f"backup_{sel_year}_{sel_month:02d}.json",
                    mime="application/json",
                )
            except Exception as e:
                st.error(f"Backup failed: {e}")


# ── Tab 2: Validation Dashboard ───────────────────────────────────────────────
with tab_validation:
    st.subheader("Constraint Violations")

    try:
        violations = validate_schedule(schedule, employees, demand, shifts)
    except Exception as e:
        st.error(f"Validation failed: {e}")
        violations = []

    errors = [v for v in violations if v.severity == "error"]
    warnings_list = [v for v in violations if v.severity == "warning"]

    if not violations:
        st.success("✅ No constraint violations. Schedule is fully compliant.")
    else:
        col_e, col_w = st.columns(2)
        col_e.metric("Hard Errors", len(errors), delta=None)
        col_w.metric("Warnings", len(warnings_list), delta=None)

    if errors:
        st.error(f"🔴 {len(errors)} hard constraint violation(s) — must be resolved before approving")
        for v in errors:
            date_str = f" — {v.date}" if v.date else ""
            with st.expander(f"🔴 {v.constraint}: **{v.employee}**{date_str}"):
                st.write(v.message)
                fix_key = f"fix_{v.constraint}_{v.employee}_{v.date}"
                if st.button("💡 Get fix suggestion", key=fix_key):
                    advisor = st.session_state.get("advisor")
                    if advisor:
                        with st.spinner("Asking LLM…"):
                            result = advisor.chat(
                                f"Fix this constraint violation: [{v.constraint}] "
                                f"{v.employee}{f' on {v.date}' if v.date else ''}: {v.message}"
                            )
                        st.info(result["text"])
                        for action in result.get("actions", []):
                            st.caption(
                                f"Suggested: {action['action']} {action['employee']} "
                                f"on {action['date']}"
                                + (f" → {action['shift']}" if action.get('shift') else "")
                            )
                    else:
                        st.warning("Open the Schedule page to enable LLM suggestions.")

    if warnings_list:
        st.warning(f"🟡 {len(warnings_list)} warning(s) — review recommended")
        for v in warnings_list:
            date_str = f" — {v.date}" if v.date else ""
            with st.expander(f"🟡 {v.constraint}: **{v.employee}**{date_str}"):
                st.write(v.message)


# ── Tab 3: Employee Summary ───────────────────────────────────────────────────
with tab_summary:
    st.subheader("Employee Hours Summary")

    shift_h = {}
    for s in shifts:
        sm = s.start_time.hour * 60 + s.start_time.minute
        em = s.end_time.hour * 60 + s.end_time.minute
        shift_h[s.id] = (em - sm) / 60.0

    assign_map = {(a.employee_id, a.date): a.shift_id for a in schedule.assignments}

    rows_data = []
    for emp in sorted(employees, key=lambda e: (e.role_capability, e.name)):
        emp_assigns = {d: sid for (eid, d), sid in assign_map.items() if eid == emp.id}
        total_h = sum(shift_h.get(sid, 0) for sid in emp_assigns.values() if sid != "off")
        days_worked = sum(1 for sid in emp_assigns.values() if sid != "off")
        days_off = sum(1 for sid in emp_assigns.values() if sid == "off")
        contracted = emp.contracted_hours * 4.33
        overtime = max(0.0, total_h - contracted)

        # Max consecutive days
        worked_dates = sorted(d for d, sid in emp_assigns.items() if sid != "off")
        max_consec = 0
        cur_consec = 0
        prev = None
        for d in worked_dates:
            if prev and (d - prev).days == 1:
                cur_consec += 1
            else:
                cur_consec = 1
            max_consec = max(max_consec, cur_consec)
            prev = d

        # Weekly breakdown (ISO weeks)
        weekly: dict[int, float] = {}
        for d, sid in emp_assigns.items():
            if sid != "off":
                wk = d.isocalendar()[1]
                weekly[wk] = weekly.get(wk, 0) + shift_h.get(sid, 0)
        max_week_h = max(weekly.values()) if weekly else 0
        weekly_str = " | ".join(f"W{wk}:{h:.0f}h" for wk, h in sorted(weekly.items()))

        rows_data.append({
            "Employee": emp.name,
            "Role": emp.role_capability.value if hasattr(emp.role_capability, 'value') else emp.role_capability,
            "Type": emp.employment_type.value if hasattr(emp.employment_type, 'value') else emp.employment_type,
            "Days Worked": days_worked,
            "Days Off": days_off,
            "Total Hours": round(total_h, 1),
            "Contracted (est.)": round(contracted, 0),
            "Overtime": round(overtime, 1) if overtime > 0 else 0,
            "Max Consecutive": max_consec,
            "Peak Week Hours": round(max_week_h, 1),
            "Weekly Breakdown": weekly_str,
        })

    df = pd.DataFrame(rows_data)

    # Color overtime rows
    def highlight_ot(row):
        color = "background-color: #FFB3B3" if row["Overtime"] > 0 else ""
        return [color] * len(row)

    st.dataframe(
        df.style.apply(highlight_ot, axis=1),
        use_container_width=True,
        hide_index=True,
    )

    # Aggregate metrics
    st.divider()
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total hours scheduled", f"{sum(r['Total Hours'] for r in rows_data):.0f}h")
    col2.metric("Employees with overtime", sum(1 for r in rows_data if r["Overtime"] > 0))
    col3.metric("Avg hours per employee", f"{sum(r['Total Hours'] for r in rows_data) / max(len(rows_data), 1):.0f}h")
    col4.metric("Max consecutive days", max((r["Max Consecutive"] for r in rows_data), default=0))


# ── Tab 4: Coverage Heatmap ───────────────────────────────────────────────────
with tab_heatmap:
    st.subheader("Daily Staffing vs Demand")
    st.caption("Green = fully staffed · Yellow = slightly short · Red = significantly short · Blue = overstaffed")

    if not demand:
        st.info("No demand data available for this month.")
    else:
        demand_map = {d.date: d for d in demand}
        emp_map = {e.id: e for e in employees}
        shift_role_map = {s.id: s.role for s in shifts}

        # Build day-level coverage
        coverage_rows = []
        for d in sorted(demand_map.keys()):
            dd = demand_map[d]
            working = [
                a for a in schedule.assignments
                if a.date == d and not a.is_day_off
            ]

            prod_count = 0
            cafe_count = 0
            for a in working:
                emp = emp_map.get(a.employee_id)
                if not emp:
                    continue
                role = shift_role_map.get(a.shift_id, "")
                if role == "production" or (emp.role_capability == "both" and role == "production"):
                    prod_count += 1
                elif role == "cafe":
                    cafe_count += 1
                elif emp.role_capability == "both":
                    prod_count += 1  # "both" defaults to production

            prod_gap = dd.production_needed - prod_count
            cafe_gap = dd.cafe_needed - cafe_count
            total_gap = prod_gap + cafe_gap

            coverage_rows.append({
                "date": d,
                "day": f"{d.strftime('%a')} {d.day}",
                "prod_needed": dd.production_needed,
                "prod_actual": prod_count,
                "prod_gap": prod_gap,
                "cafe_needed": dd.cafe_needed,
                "cafe_actual": cafe_count,
                "cafe_gap": cafe_gap,
                "total_gap": total_gap,
                "has_ship": dd.has_cruise,
                "ship_names": ", ".join(s.ship_name for s in dd.ships_today),
            })

        # Render as HTML heatmap grid
        cells_html = ""
        for cr in coverage_rows:
            gap = cr["total_gap"]
            if gap <= -1:
                bg = "#B3D4FF"  # overstaffed — blue
                icon = "↑"
            elif gap == 0:
                bg = "#B3FFB3"  # perfect — green
                icon = "✓"
            elif gap == 1:
                bg = "#FFD699"  # slightly short — yellow
                icon = "~"
            else:
                bg = "#FFB3B3"  # significantly short — red
                icon = f"-{gap}"

            ship_badge = "🚢" if cr["has_ship"] else ""
            tooltip = (
                f"Prod: {cr['prod_actual']}/{cr['prod_needed']} | "
                f"Café: {cr['cafe_actual']}/{cr['cafe_needed']}"
                + (f" | {cr['ship_names']}" if cr["ship_names"] else "")
            )
            cells_html += (
                f"<div title='{tooltip}' style='display:inline-block; "
                f"background:{bg}; border:1px solid #ccc; border-radius:4px; "
                f"margin:2px; padding:4px 6px; min-width:46px; text-align:center; "
                f"font-family:monospace; font-size:11px;'>"
                f"<div style='font-weight:bold;'>{cr['day']}</div>"
                f"<div>{icon} {ship_badge}</div>"
                f"</div>"
            )

        st.markdown(
            f"<div style='display:flex; flex-wrap:wrap;'>{cells_html}</div>",
            unsafe_allow_html=True,
        )

        st.divider()

        # Detailed table
        with st.expander("Detailed coverage table"):
            table_data = [
                {
                    "Date": cr["date"].strftime("%a %b %d"),
                    "Prod Need": cr["prod_needed"],
                    "Prod Actual": cr["prod_actual"],
                    "Prod Gap": cr["prod_gap"],
                    "Café Need": cr["cafe_needed"],
                    "Café Actual": cr["cafe_actual"],
                    "Café Gap": cr["cafe_gap"],
                    "Ships": cr["ship_names"] or "—",
                }
                for cr in coverage_rows
            ]
            df_cov = pd.DataFrame(table_data)

            def color_gap(val):
                if isinstance(val, (int, float)):
                    if val > 1:
                        return "background-color: #FFB3B3"
                    elif val == 1:
                        return "background-color: #FFD699"
                    elif val < 0:
                        return "background-color: #B3D4FF"
                return ""

            st.dataframe(
                df_cov.style.map(color_gap, subset=["Prod Gap", "Café Gap"]),
                use_container_width=True,
                hide_index=True,
            )
