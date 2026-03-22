"""Schedule Editor page — interactive pivot-table editor for manual schedule adjustments."""
from __future__ import annotations

import calendar
import uuid
from datetime import date, datetime

import pandas as pd
import streamlit as st

from src.db.database import db_session
from src.demand.forecaster import DailyDemand, generate_monthly_demand
from src.models.cruise_ship import CruiseShipORM, CruiseShipRead
from src.models.employee import EmployeeORM, EmployeeRead
from src.models.enums import ScheduleStatus
from src.models.schedule import AssignmentORM, AssignmentRead, ScheduleORM, ScheduleRead
from src.models.shift_template import ShiftTemplateORM, ShiftTemplateRead
from src.solver import validate_schedule
from src.ui.components.chat_panel import render_chat_panel

st.set_page_config(page_title="Schedule Editor", page_icon="✏️", layout="wide")
st.title("✏️ Schedule Editor")
st.caption("Click any cell to change an assignment. Shift codes: 1–6 (café), P1–P5 (production), off = day off, leave blank = remove.")

SEASON_MONTHS = {5: "May", 6: "June", 7: "July", 8: "August", 9: "September", 10: "October"}


# ── Data loaders ──────────────────────────────────────────────────────────────

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


def _load_ships(year: int, month: int) -> list[CruiseShipRead]:
    _, last_day = calendar.monthrange(year, month)
    with db_session() as db:
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


def _load_saved_schedule(year: int, month: int) -> ScheduleRead | None:
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


def _save_schedule(schedule: ScheduleRead) -> bool:
    try:
        with db_session() as db:
            existing = db.query(ScheduleORM).filter_by(
                year=schedule.year, month=schedule.month
            ).first()
            if existing:
                db.delete(existing)
                db.flush()
            orm = ScheduleORM(
                id=schedule.id, month=schedule.month, year=schedule.year,
                status=schedule.status.value,
                created_at=schedule.created_at,
                modified_at=datetime.utcnow(),
            )
            db.add(orm)
            db.flush()
            for a in schedule.assignments:
                db.add(AssignmentORM(
                    id=a.id, schedule_id=schedule.id,
                    employee_id=a.employee_id, date=a.date,
                    shift_id=a.shift_id, is_day_off=a.is_day_off, notes=a.notes,
                ))
        return True
    except Exception as e:
        st.error(f"Save failed: {e}")
        return False


# ── Session state initialisation (must happen before any widget) ──────────────

if "editor_year" not in st.session_state:
    st.session_state["editor_year"] = 2026
if "editor_month" not in st.session_state:
    st.session_state["editor_month"] = 8

# Handle pending navigation from "Use current session schedule" button
if "_pending_editor_year" in st.session_state:
    st.session_state["editor_year"] = st.session_state.pop("_pending_editor_year")
    st.session_state["editor_month"] = st.session_state.pop("_pending_editor_month")

# ── Layout ────────────────────────────────────────────────────────────────────
_chat_expanded = st.session_state.get("chat_expanded", False)

if _chat_expanded:
    if st.button(
        "📅  Return to Schedule View  —  click here to go back to the full schedule grid",
        type="primary",
        use_container_width=True,
        key="return_schedule_banner",
    ):
        st.session_state["chat_expanded"] = False
        st.rerun()

main_col, chat_col = st.columns([2, 3] if _chat_expanded else [3, 1])

with main_col:

    # ── Month / load controls ─────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns([1, 1, 1, 1])
    with c1:
        sel_year = st.selectbox("Year", [2026, 2027], index=0, key="editor_year")
    with c2:
        sel_month = st.selectbox(
            "Month", list(SEASON_MONTHS.keys()),
            format_func=lambda m: SEASON_MONTHS[m], index=3, key="editor_month",
        )
    with c3:
        st.write("")
        load_btn = st.button("📂 Load from DB", use_container_width=True)
    with c4:
        st.write("")
        use_current = st.button("↩ Use current session schedule", use_container_width=True)

    # ── Load data ─────────────────────────────────────────────────────────────
    try:
        employees = _load_employees()
        shifts = _load_shifts()
        ships = _load_ships(sel_year, sel_month)
    except Exception as e:
        st.error(f"Failed to load data: {e}")
        st.stop()

    demand = generate_monthly_demand(sel_year, sel_month, ships)

    if load_btn:
        sched = _load_saved_schedule(sel_year, sel_month)
        if sched:
            st.session_state["editor_schedule"] = sched
            st.success(f"Loaded {SEASON_MONTHS[sel_month]} {sel_year} (status: {sched.status.value})")
        else:
            st.warning(f"No saved schedule for {SEASON_MONTHS[sel_month]} {sel_year}.")

    if use_current:
        sched = st.session_state.get("current_schedule")
        if sched:
            st.session_state["editor_schedule"] = sched
            # Use pending keys + rerun so we don't assign to widget keys after they've rendered
            st.session_state["_pending_editor_year"] = sched.year
            st.session_state["_pending_editor_month"] = sched.month
            st.rerun()
        else:
            st.warning("No schedule in session. Generate one on the Schedule page first.")

    schedule: ScheduleRead | None = st.session_state.get("editor_schedule")

    if not schedule:
        st.info("Load a schedule using the buttons above, or generate one on the **Schedule** page first.")
        st.stop()

    if (
        st.session_state.get("editor_year") != sel_year
        or st.session_state.get("editor_month") != sel_month
    ):
        st.info("Month changed — click **Load from DB** to load the schedule for the selected month.")
        st.stop()

    st.divider()

    # Flash banner when LLM Apply updates the grid
    if "_apply_flash" in st.session_state:
        st.success(f"✅ {st.session_state.pop('_apply_flash')} — schedule grid updated below")

    st.subheader(f"Editing: {SEASON_MONTHS[schedule.month]} {schedule.year}  — Status: {schedule.status.value.upper()}")

    # ── Build pivot DataFrame ─────────────────────────────────────────────────
    days = sorted({d.date for d in demand})
    if not days:
        st.error("No demand data for this month. Upload cruise ships first.")
        st.stop()

    assign_map = {(a.employee_id, a.date): a for a in schedule.assignments}
    emp_by_name = {e.name: e for e in employees}

    # Sort employees: production/both first, then café; within each group by name
    sorted_emps = sorted(
        employees,
        key=lambda e: (
            0 if e.role_capability in ("production", "both") else 1,
            e.name,
        ),
    )

    # Valid shift options per role
    cafe_shifts = [s.id for s in shifts if s.role == "cafe"]
    prod_shifts = [s.id for s in shifts if s.role == "production"]
    all_shift_ids = [s.id for s in shifts]
    CELL_OPTIONS = [""] + all_shift_ids + ["off"]

    # Build rows
    col_keys = [str(d) for d in days]
    rows_list = []
    for emp in sorted_emps:
        rc = emp.role_capability.value if hasattr(emp.role_capability, 'value') else emp.role_capability
        row: dict = {
            "Employee": emp.name,
            "Role": rc,
        }
        for d in days:
            key = str(d)
            is_avail = emp.availability_start <= d <= emp.availability_end
            if not is_avail:
                row[key] = "—"
            else:
                a = assign_map.get((emp.id, d))
                row[key] = (a.shift_id if a else "")
        rows_list.append(row)

    df = pd.DataFrame(rows_list)

    # ── Column config ─────────────────────────────────────────────────────────
    col_cfg: dict = {
        "Employee": st.column_config.TextColumn("Employee", disabled=True, width="medium"),
        "Role": st.column_config.TextColumn("Role", disabled=True, width="small"),
    }
    for d in days:
        key = str(d)
        col_cfg[key] = st.column_config.SelectboxColumn(
            label=f"{d.strftime('%a')}\n{d.day}",
            options=CELL_OPTIONS + ["—"],
            required=False,
            width="small",
        )

    # ── Render editable grid ──────────────────────────────────────────────────
    st.caption(
        f"**{len(days)} days · {len(sorted_emps)} employees** — "
        "Select a cell to change assignment. "
        f"Valid codes: {', '.join(all_shift_ids)}, off, or blank to remove."
    )

    edited_df = st.data_editor(
        df,
        column_config=col_cfg,
        use_container_width=True,
        hide_index=True,
        num_rows="fixed",
        key="schedule_pivot_editor",
        height=min(50 + len(sorted_emps) * 35, 600),
    )

    # ── Action buttons ────────────────────────────────────────────────────────
    act1, act2, act3, act4 = st.columns(4)

    apply_clicked = act1.button("✅ Apply Changes", type="primary", use_container_width=True)
    save_clicked = act2.button("💾 Save Draft", use_container_width=True)
    approve_clicked = act3.button("🏁 Approve", use_container_width=True)
    validate_clicked = act4.button("🔍 Validate", use_container_width=True)

    if apply_clicked:
        # Convert edited_df back to assignments
        new_assignments: list[AssignmentRead] = []
        for _, row in edited_df.iterrows():
            emp_name = row["Employee"]
            emp = emp_by_name.get(emp_name)
            if not emp:
                continue
            for d in days:
                key = str(d)
                val = row.get(key, "")
                if val == "—" or val is None or val == "":
                    continue  # unavailable or removed
                is_off = (val == "off")
                # Preserve existing assignment ID if present
                existing_a = assign_map.get((emp.id, d))
                a_id = existing_a.id if existing_a else uuid.uuid4()
                new_assignments.append(AssignmentRead(
                    id=a_id,
                    schedule_id=schedule.id,
                    employee_id=emp.id,
                    date=d,
                    shift_id=val,
                    is_day_off=is_off,
                    notes=existing_a.notes if existing_a else None,
                ))

        updated = ScheduleRead(
            id=schedule.id, month=schedule.month, year=schedule.year,
            status=schedule.status, created_at=schedule.created_at,
            modified_at=datetime.utcnow(),
            assignments=new_assignments,
        )
        st.session_state["editor_schedule"] = updated
        st.session_state["current_schedule"] = updated  # sync with generator page
        st.success(f"Applied — {len(new_assignments)} assignments.")
        st.rerun()

    if save_clicked:
        if _save_schedule(schedule):
            st.success("Saved as draft.")

    if approve_clicked:
        if _save_schedule(schedule):
            try:
                with db_session() as db:
                    orm = db.query(ScheduleORM).filter_by(id=schedule.id).first()
                    if orm:
                        orm.status = ScheduleStatus.approved.value
                        orm.modified_at = datetime.utcnow()
                approved = ScheduleRead(
                    id=schedule.id, month=schedule.month, year=schedule.year,
                    status=ScheduleStatus.approved,
                    created_at=schedule.created_at, modified_at=datetime.utcnow(),
                    assignments=schedule.assignments,
                )
                st.session_state["editor_schedule"] = approved
                st.session_state["current_schedule"] = approved
                st.success("Schedule approved! ✅")
                st.rerun()
            except Exception as e:
                st.error(f"Approve failed: {e}")

    # ── Validation panel ──────────────────────────────────────────────────────
    if validate_clicked or st.session_state.get("editor_show_violations"):
        st.session_state["editor_show_violations"] = True
        st.divider()
        st.subheader("Constraint Violations")
        try:
            viols = validate_schedule(schedule, employees, demand, shifts)
            errors = [v for v in viols if v.severity == "error"]
            warns = [v for v in viols if v.severity == "warning"]

            c_e, c_w = st.columns(2)
            c_e.metric("Hard Errors", len(errors))
            c_w.metric("Warnings", len(warns))

            for v in errors:
                d_str = f" ({v.date})" if v.date else ""
                st.error(f"🔴 **{v.employee}**{d_str}: {v.message}")
            for v in warns:
                d_str = f" ({v.date})" if v.date else ""
                st.warning(f"🟡 **{v.employee}**{d_str}: {v.message}")
            if not viols:
                st.success("No constraint violations. ✅")
        except Exception as e:
            st.error(f"Validation error: {e}")

# ── Chat panel ────────────────────────────────────────────────────────────────
with chat_col:
    render_chat_panel(
        schedule=st.session_state.get("editor_schedule"),
        employees=employees if "employees" in locals() else None,
        demand=demand if "demand" in locals() else None,
        shift_templates=shifts if "shifts" in locals() else None,
    )
