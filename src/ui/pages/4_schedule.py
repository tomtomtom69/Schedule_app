"""Schedule Generator page — Phase 4 full implementation."""
from __future__ import annotations

import calendar
import uuid
from datetime import date, datetime
from typing import Any

import streamlit as st

from src.db.database import db_session
from src.demand.forecaster import DailyDemand, generate_monthly_demand
from src.models.cruise_ship import CruiseShipORM, CruiseShipRead
from src.models.employee import EmployeeORM, EmployeeRead
from src.models.enums import ScheduleStatus
from src.models.schedule import AssignmentORM, AssignmentRead, ScheduleORM, ScheduleRead
from src.models.shift_template import ShiftTemplateORM, ShiftTemplateRead
from src.solver import ScheduleGenerator, Violation, validate_schedule
from src.ui.components.chat_panel import render_chat_panel
from src.ui.components.schedule_grid import render_schedule_grid

st.set_page_config(page_title="Schedule", page_icon="📅", layout="wide")
st.title("📅 Schedule Generator")

SEASON_MONTHS = {5: "May", 6: "June", 7: "July", 8: "August", 9: "September", 10: "October"}

# ── Helper: load data from DB ─────────────────────────────────────────────────

@st.cache_data(ttl=60)
def load_employees() -> list[EmployeeRead]:
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
def load_shifts() -> list[ShiftTemplateRead]:
    with db_session() as db:
        rows = db.query(ShiftTemplateORM).order_by(ShiftTemplateORM.id).all()
        return [
            ShiftTemplateRead(
                id=r.id, role=r.role, label=r.label,
                start_time=r.start_time, end_time=r.end_time,
            )
            for r in rows
        ]


@st.cache_data(ttl=60)
def load_ships_for_month(year: int, month: int) -> list[CruiseShipRead]:
    with db_session() as db:
        _, last_day = calendar.monthrange(year, month)
        rows = db.query(CruiseShipORM).filter(
            CruiseShipORM.date >= date(year, month, 1),
            CruiseShipORM.date <= date(year, month, last_day),
        ).all()
        return [
            CruiseShipRead(
                id=r.id, ship_name=r.ship_name, date=r.date,
                arrival_time=r.arrival_time, departure_time=r.departure_time,
                port=r.port, size=r.size, good_ship=r.good_ship,
                extra_language=r.extra_language,
            )
            for r in rows
        ]


def load_saved_schedule(year: int, month: int) -> ScheduleRead | None:
    """Load the most recent saved schedule for year/month from DB."""
    try:
        with db_session() as db:
            orm = (
                db.query(ScheduleORM)
                .filter_by(year=year, month=month)
                .order_by(ScheduleORM.created_at.desc())
                .first()
            )
            if orm is None:
                return None

            assignments = [
                AssignmentRead(
                    id=a.id, schedule_id=a.schedule_id,
                    employee_id=a.employee_id, date=a.date,
                    shift_id=a.shift_id, is_day_off=a.is_day_off,
                    notes=a.notes,
                )
                for a in orm.assignments
            ]
            return ScheduleRead(
                id=orm.id, month=orm.month, year=orm.year,
                status=ScheduleStatus(orm.status),
                created_at=orm.created_at,
                modified_at=orm.modified_at,
                assignments=assignments,
            )
    except Exception:
        return None


def save_schedule_to_db(schedule: ScheduleRead) -> bool:
    """Persist a ScheduleRead to the database."""
    try:
        with db_session() as db:
            # Remove any existing draft for this month
            existing = db.query(ScheduleORM).filter_by(
                year=schedule.year, month=schedule.month,
            ).first()
            if existing:
                db.delete(existing)
                db.flush()

            orm = ScheduleORM(
                id=schedule.id,
                month=schedule.month,
                year=schedule.year,
                status=schedule.status.value,
                created_at=schedule.created_at,
                modified_at=datetime.utcnow(),
            )
            db.add(orm)
            db.flush()

            for a in schedule.assignments:
                db.add(AssignmentORM(
                    id=a.id,
                    schedule_id=schedule.id,
                    employee_id=a.employee_id,
                    date=a.date,
                    shift_id=a.shift_id,
                    is_day_off=a.is_day_off,
                    notes=a.notes,
                ))
        return True
    except Exception as e:
        st.error(f"Database save failed: {e}")
        return False


def approve_schedule_in_db(schedule_id: uuid.UUID) -> bool:
    """Mark a schedule as approved in the DB."""
    try:
        with db_session() as db:
            orm = db.query(ScheduleORM).filter_by(id=schedule_id).first()
            if orm:
                orm.status = ScheduleStatus.approved.value
                orm.modified_at = datetime.utcnow()
            else:
                return False
        return True
    except Exception as e:
        st.error(f"Approve failed: {e}")
        return False


# ── Helper: weekly hours per employee ────────────────────────────────────────

def compute_weekly_hours(
    schedule: ScheduleRead,
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
) -> dict[uuid.UUID, float]:
    """Return total hours worked per employee for the whole schedule."""
    shift_hours = {}
    for s in shifts:
        start_m = s.start_time.hour * 60 + s.start_time.minute
        end_m = s.end_time.hour * 60 + s.end_time.minute
        shift_hours[s.id] = (end_m - start_m) / 60.0

    totals: dict[uuid.UUID, float] = {e.id: 0.0 for e in employees}
    for a in schedule.assignments:
        if not a.is_day_off and a.shift_id in shift_hours:
            totals[a.employee_id] = totals.get(a.employee_id, 0.0) + shift_hours[a.shift_id]
    return totals


# ── Layout: two columns (main + chat) ────────────────────────────────────────

main_col, chat_col = st.columns([3, 1])

with main_col:

    # ── Month/Year selector ───────────────────────────────────────────────────
    st.subheader("Select Month")
    col_year, col_month, col_gen, col_load = st.columns([1, 1, 1, 1])

    with col_year:
        sel_year = st.selectbox("Year", options=[2026, 2027], index=0, key="sched_year")
    with col_month:
        sel_month = st.selectbox(
            "Month",
            options=list(SEASON_MONTHS.keys()),
            format_func=lambda m: SEASON_MONTHS[m],
            index=3,  # August default
            key="sched_month",
        )

    # ── Load data ─────────────────────────────────────────────────────────────
    try:
        employees = load_employees()
        shifts = load_shifts()
        ships = load_ships_for_month(sel_year, sel_month)
    except Exception as e:
        st.error(f"Failed to load data from database: {e}")
        st.stop()

    if not employees:
        st.warning("No employees found. Go to the Employees page to add them.")
        st.stop()
    if not shifts:
        st.warning("No shift templates found. Go to Settings to configure shifts.")
        st.stop()

    with col_gen:
        st.write("")  # spacing
        generate_clicked = st.button("🔄 Generate Schedule", type="primary", use_container_width=True)
    with col_load:
        st.write("")
        load_clicked = st.button("📂 Load Saved", use_container_width=True)

    # ── Generate ──────────────────────────────────────────────────────────────
    if generate_clicked:
        with st.spinner(f"Generating {SEASON_MONTHS[sel_month]} {sel_year} schedule (up to 60s)…"):
            try:
                demand = generate_monthly_demand(sel_year, sel_month, ships)
                if not demand:
                    st.error("No demand generated — check that the selected month is within the operating season (May–October).")
                else:
                    gen = ScheduleGenerator(employees, demand, shifts)
                    gen.build_model()
                    result = gen.solve()
                    info = gen.solve_info

                    # ── Always show solver diagnostics panel ───────────────
                    with st.expander("🔍 Solver diagnostics", expanded=(result is None)):
                        cols = st.columns(4)
                        cols[0].metric("Status", info.status_name)
                        cols[1].metric("Variables", f"{info.num_variables:,}")
                        cols[2].metric("Available employees", info.num_employees_available)
                        cols[3].metric("Working shifts", info.num_working_assignments)
                        if info.wall_time > 0:
                            st.caption(f"Solver wall time: {info.wall_time:.2f}s  |  "
                                       f"Days in month: {info.num_days}  |  "
                                       f"Objective: {info.objective_value:.0f}")
                        if info.warnings:
                            st.markdown("**⚠️ Warnings:**")
                            for w in info.warnings:
                                st.warning(w)
                        if info.diagnostics:
                            st.markdown("**❌ Issues:**")
                            for d_msg in info.diagnostics:
                                st.error(d_msg)

                    if result is None:
                        st.error(
                            f"**Schedule generation failed** (solver status: `{info.status_name}`).  \n"
                            "See the diagnostics panel above for details."
                        )
                    else:
                        st.session_state["current_schedule"] = result
                        st.session_state["current_demand"] = demand
                        st.session_state["schedule_year"] = sel_year
                        st.session_state["schedule_month"] = sel_month
                        n_working = sum(1 for a in result.assignments if not a.is_day_off)
                        st.success(
                            f"✅ Schedule generated!  \n"
                            f"**{n_working}** working assignments across **{len(demand)}** days  \n"
                            f"Status: `{info.status_name}` — wall time: {info.wall_time:.1f}s"
                        )
            except Exception as e:
                st.error(f"Generation failed: {e}")
                import traceback
                st.code(traceback.format_exc(), language="text")

    # ── Load saved ────────────────────────────────────────────────────────────
    if load_clicked:
        saved = load_saved_schedule(sel_year, sel_month)
        if saved:
            demand = generate_monthly_demand(sel_year, sel_month, ships)
            st.session_state["current_schedule"] = saved
            st.session_state["current_demand"] = demand
            st.session_state["schedule_year"] = sel_year
            st.session_state["schedule_month"] = sel_month
            st.success(f"Loaded saved schedule (status: {saved.status.value}).")
        else:
            st.warning(f"No saved schedule found for {SEASON_MONTHS[sel_month]} {sel_year}.")

    # ── Display schedule if in session state ──────────────────────────────────
    schedule: ScheduleRead | None = st.session_state.get("current_schedule")
    demand_list: list[DailyDemand] = st.session_state.get("current_demand", [])

    if schedule and demand_list:
        # Check month matches selector
        if (
            st.session_state.get("schedule_year") != sel_year
            or st.session_state.get("schedule_month") != sel_month
        ):
            st.info("Month changed. Click 'Generate Schedule' or 'Load Saved' for the selected month.")
        else:
            st.divider()

            # ── Action buttons ────────────────────────────────────────────────
            act1, act2, act3, act4 = st.columns(4)
            status_label = schedule.status.value.upper()

            with act1:
                if st.button("💾 Save Draft", use_container_width=True):
                    if save_schedule_to_db(schedule):
                        st.success("Schedule saved as draft.")

            with act2:
                if schedule.status != ScheduleStatus.approved:
                    if st.button("✅ Approve", use_container_width=True, type="primary"):
                        if save_schedule_to_db(schedule):
                            if approve_schedule_in_db(schedule.id):
                                # Update session state status
                                updated = ScheduleRead(
                                    id=schedule.id, month=schedule.month, year=schedule.year,
                                    status=ScheduleStatus.approved,
                                    created_at=schedule.created_at,
                                    modified_at=datetime.utcnow(),
                                    assignments=schedule.assignments,
                                )
                                st.session_state["current_schedule"] = updated
                                st.success("Schedule approved!")
                                st.rerun()
                else:
                    st.info("✅ Approved")

            with act3:
                if st.button("📊 Export →", use_container_width=True):
                    st.info("Use the Export page in the sidebar to download the schedule.")

            with act4:
                if st.button("🔄 Regenerate", use_container_width=True):
                    # Clear current and regenerate
                    st.session_state.pop("current_schedule", None)
                    st.session_state.pop("current_demand", None)
                    st.rerun()

            st.caption(
                f"Schedule ID: {str(schedule.id)[:8]}… | "
                f"Status: {status_label} | "
                f"Created: {schedule.created_at.strftime('%Y-%m-%d %H:%M')}"
            )

            # ── Schedule Grid ─────────────────────────────────────────────────
            st.subheader(f"📅 {SEASON_MONTHS[sel_month]} {sel_year} — Schedule Grid")

            render_schedule_grid(schedule, employees, shifts, demand_list, height=600)

            # ── Edit Assignment ───────────────────────────────────────────────
            with st.expander("✏️ Edit Assignment"):
                emp_options = {e.name: e for e in employees}
                shift_options = ["off"] + [s.id for s in shifts]

                edit_emp = st.selectbox("Employee", list(emp_options.keys()), key="edit_emp")
                edit_day = st.date_input(
                    "Date",
                    value=date(sel_year, sel_month, 1),
                    min_value=date(sel_year, sel_month, 1),
                    key="edit_day",
                )
                edit_shift = st.selectbox("Shift (or 'off')", shift_options, key="edit_shift")

                if st.button("Apply Change"):
                    emp = emp_options[edit_emp]
                    # Update assignment in session state
                    updated_assignments = []
                    found = False
                    for a in schedule.assignments:
                        if a.employee_id == emp.id and a.date == edit_day:
                            updated_assignments.append(AssignmentRead(
                                id=a.id, schedule_id=a.schedule_id,
                                employee_id=a.employee_id, date=a.date,
                                shift_id=edit_shift,
                                is_day_off=(edit_shift == "off"),
                                notes=a.notes,
                            ))
                            found = True
                        else:
                            updated_assignments.append(a)

                    if not found:
                        # New assignment
                        updated_assignments.append(AssignmentRead(
                            id=uuid.uuid4(),
                            schedule_id=schedule.id,
                            employee_id=emp.id,
                            date=edit_day,
                            shift_id=edit_shift,
                            is_day_off=(edit_shift == "off"),
                        ))

                    st.session_state["current_schedule"] = ScheduleRead(
                        id=schedule.id, month=schedule.month, year=schedule.year,
                        status=schedule.status, created_at=schedule.created_at,
                        modified_at=datetime.utcnow(),
                        assignments=updated_assignments,
                    )
                    st.success(f"Updated {edit_emp} on {edit_day} → {edit_shift}")
                    st.rerun()

            # ── Summary Panel ─────────────────────────────────────────────────
            st.subheader("📊 Summary")

            # Hours per employee
            hours = compute_weekly_hours(schedule, employees, shifts)
            emp_map = {e.id: e for e in employees}

            summary_data = []
            for emp in sorted(employees, key=lambda e: (e.role_capability, e.name)):
                total_hours = hours.get(emp.id, 0.0)
                days_worked = sum(
                    1 for a in schedule.assignments
                    if a.employee_id == emp.id and not a.is_day_off
                )
                contracted_monthly = emp.contracted_hours * 4.33  # weekly × 4.33 weeks
                overtime = max(0, total_hours - contracted_monthly)
                summary_data.append({
                    "Employee": emp.name,
                    "Role": emp.role_capability.value if hasattr(emp.role_capability, 'value') else emp.role_capability,
                    "Type": emp.employment_type.value if hasattr(emp.employment_type, 'value') else emp.employment_type,
                    "Days Worked": days_worked,
                    "Total Hours": f"{total_hours:.1f}h",
                    "Contracted (est.)": f"{contracted_monthly:.0f}h",
                    "Overtime": f"{overtime:.1f}h" if overtime > 0 else "—",
                    "⚠️": "🔴 OT" if overtime > 0 else "",
                })

            import pandas as pd
            df_summary = pd.DataFrame(summary_data)
            st.dataframe(df_summary, use_container_width=True, hide_index=True)

            # Coverage gaps
            st.subheader("Coverage Check")
            coverage_issues = []
            demand_map = {d.date: d for d in demand_list}
            for d, dd in sorted(demand_map.items()):
                working_today = [
                    a for a in schedule.assignments
                    if a.date == d and not a.is_day_off
                ]
                prod_workers = []
                cafe_workers = []
                for a in working_today:
                    emp = emp_map.get(a.employee_id)
                    if emp:
                        shift = next((s for s in shifts if s.id == a.shift_id), None)
                        if shift:
                            if shift.role == "production":
                                prod_workers.append(emp)
                            elif shift.role == "cafe":
                                cafe_workers.append(emp)
                            elif emp.role_capability == "both":
                                prod_workers.append(emp)

                prod_gap = dd.production_needed - len(prod_workers)
                cafe_gap = dd.cafe_needed - len(cafe_workers)

                if prod_gap > 0:
                    coverage_issues.append(f"🔴 {d}: Production short by {prod_gap} (need {dd.production_needed}, have {len(prod_workers)})")
                if cafe_gap > 0:
                    coverage_issues.append(f"🔴 {d}: Café short by {cafe_gap} (need {dd.cafe_needed}, have {len(cafe_workers)})")

            if coverage_issues:
                st.error(f"{len(coverage_issues)} coverage gaps:")
                for issue in coverage_issues:
                    st.write(issue)
            else:
                st.success("All days meet staffing requirements.")

            # Constraint violations
            st.subheader("Constraint Violations")
            try:
                violations = validate_schedule(schedule, employees, demand_list, shifts)
                errors = [v for v in violations if v.severity == "error"]
                warnings = [v for v in violations if v.severity == "warning"]

                if errors:
                    st.error(f"{len(errors)} hard constraint violation(s):")
                    for v in errors:
                        date_str = f" ({v.date})" if v.date else ""
                        st.write(f"🔴 **{v.employee}**{date_str}: {v.message}")
                else:
                    st.success("No hard constraint violations.")

                if warnings:
                    st.warning(f"{len(warnings)} soft constraint warning(s):")
                    for v in warnings:
                        date_str = f" ({v.date})" if v.date else ""
                        st.write(f"🟡 **{v.employee}**{date_str}: {v.message}")
            except Exception as e:
                st.error(f"Validation error: {e}")

    else:
        st.info(
            "No schedule loaded. Select a month and click **Generate Schedule** "
            "or **Load Saved** if a schedule exists for that month."
        )

# ── Chat panel ────────────────────────────────────────────────────────────────
with chat_col:
    render_chat_panel(
        schedule=st.session_state.get("current_schedule"),
        employees=employees if "employees" in locals() else None,
        demand=st.session_state.get("current_demand"),
        shift_templates=shifts if "shifts" in locals() else None,
    )
