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
from src.solver import ScheduleGenerator, Violation, validate_schedule, run_fallback_solve
from src.solver.fallback import (
    is_skeleton_from_json,
    relaxation_notes_from_json,
    staffing_gaps_from_json,
)
from src.models.closed_day import ClosedDayORM
from src.ui.components.chat_panel import render_chat_panel
from src.ui.components.schedule_grid import render_schedule_grid
from src.ui.components.sidebar import render_shift_legend

st.set_page_config(page_title="Schedule", page_icon="📅", layout="wide")
render_shift_legend()
st.title("📅 Schedule Generator")

SEASON_MONTHS = {5: "May", 6: "June", 7: "July", 8: "August", 9: "September", 10: "October"}


# ── Cross-month consistency helpers ──────────────────────────────────────────

def _next_month_name_if_exists(year: int, month: int) -> str | None:
    """Return 'June 2026' if a non-archived schedule for month+1 exists in DB, else None."""
    try:
        next_m = month % 12 + 1
        next_y = year + (1 if month == 12 else 0)
        if next_m not in SEASON_MONTHS:
            return None
        with db_session() as db:
            exists = (
                db.query(ScheduleORM)
                .filter(
                    ScheduleORM.year == next_y,
                    ScheduleORM.month == next_m,
                    ScheduleORM.status != ScheduleStatus.archived.value,
                )
                .first()
            )
            return f"{SEASON_MONTHS[next_m]} {next_y}" if exists else None
    except Exception:
        return None


def _stale_prev_month_warning(schedule: "ScheduleRead") -> str | None:
    """Return a warning string if the previous month's APPROVED schedule was modified
    after this schedule was created. Uses approved version for cross-month accuracy.
    """
    try:
        prev_m = schedule.month - 1
        prev_y = schedule.year
        if prev_m == 0:
            prev_m = 12
            prev_y -= 1
        if prev_m not in SEASON_MONTHS:
            return None
        with db_session() as db:
            # Use approved version for cross-month check; fall back to latest non-archived
            prev_orm = (
                db.query(ScheduleORM)
                .filter_by(year=prev_y, month=prev_m, status=ScheduleStatus.approved.value)
                .order_by(ScheduleORM.version.desc())
                .first()
            )
            if prev_orm is None:
                prev_orm = (
                    db.query(ScheduleORM)
                    .filter(
                        ScheduleORM.year == prev_y,
                        ScheduleORM.month == prev_m,
                        ScheduleORM.status != ScheduleStatus.archived.value,
                    )
                    .order_by(ScheduleORM.version.desc())
                    .first()
                )
            if prev_orm is None or prev_orm.modified_at is None:
                return None
            if prev_orm.modified_at > schedule.created_at:
                curr_name = f"{SEASON_MONTHS[schedule.month]} {schedule.year}"
                prev_name = f"{SEASON_MONTHS[prev_m]} {prev_y}"
                return (
                    f"The **{prev_name}** schedule was modified after this **{curr_name}** "
                    f"schedule was generated. Consider regenerating **{curr_name}** to ensure "
                    "the consecutive-day constraints and cross-month carry-in are up to date."
                )
        return None
    except Exception:
        return None


# ── Closed days helpers ───────────────────────────────────────────────────────

def load_closed_days(year: int, month: int) -> set[date]:
    """Load closed dates for the given month from DB."""
    try:
        _, last_day = calendar.monthrange(year, month)
        with db_session() as db:
            rows = db.query(ClosedDayORM).filter(
                ClosedDayORM.year == year,
                ClosedDayORM.date >= date(year, month, 1),
                ClosedDayORM.date <= date(year, month, last_day),
            ).all()
        return {r.date for r in rows}
    except Exception:
        return set()


def save_closed_days(year: int, month: int, closed: set[date]) -> None:
    """Replace the closed-day set for year/month in DB."""
    _, last_day = calendar.monthrange(year, month)
    with db_session() as db:
        db.query(ClosedDayORM).filter(
            ClosedDayORM.year == year,
            ClosedDayORM.date >= date(year, month, 1),
            ClosedDayORM.date <= date(year, month, last_day),
        ).delete(synchronize_session=False)
        for d in closed:
            db.add(ClosedDayORM(date=d, year=year))


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


def _orm_to_schedule_read(orm: ScheduleORM) -> ScheduleRead:
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
        version=getattr(orm, "version", 1),
        created_at=orm.created_at,
        modified_at=orm.modified_at,
        assignments=assignments,
        is_fallback=getattr(orm, "is_fallback", False),
        fallback_notes=getattr(orm, "fallback_notes", None),
    )


def load_saved_schedule(year: int, month: int) -> ScheduleRead | None:
    """Load the latest non-archived schedule for year/month."""
    try:
        with db_session() as db:
            orm = (
                db.query(ScheduleORM)
                .filter(
                    ScheduleORM.year == year,
                    ScheduleORM.month == month,
                    ScheduleORM.status != ScheduleStatus.archived.value,
                )
                .order_by(ScheduleORM.version.desc())
                .first()
            )
            return _orm_to_schedule_read(orm) if orm else None
    except Exception:
        return None


def load_schedule_by_id(schedule_id: uuid.UUID) -> ScheduleRead | None:
    """Load a specific schedule by ID."""
    try:
        with db_session() as db:
            orm = db.query(ScheduleORM).filter_by(id=schedule_id).first()
            return _orm_to_schedule_read(orm) if orm else None
    except Exception:
        return None


def load_all_version_meta(year: int, month: int) -> list[dict]:
    """Return lightweight version metadata (no assignments) for all versions of a month."""
    try:
        with db_session() as db:
            rows = (
                db.query(
                    ScheduleORM.id, ScheduleORM.version, ScheduleORM.status,
                    ScheduleORM.created_at, ScheduleORM.modified_at,
                )
                .filter_by(year=year, month=month)
                .order_by(ScheduleORM.version.desc())
                .all()
            )
            return [
                {
                    "id": str(r.id),
                    "version": r.version,
                    "status": r.status,
                    "created_at": r.created_at,
                    "modified_at": r.modified_at,
                }
                for r in rows
            ]
    except Exception:
        return []


def archive_existing_schedules(year: int, month: int) -> int:
    """Set all non-archived schedules for (year, month) to archived.
    Returns the next version number to use.
    """
    try:
        with db_session() as db:
            rows = (
                db.query(ScheduleORM)
                .filter_by(year=year, month=month)
                .all()
            )
            max_ver = 0
            for r in rows:
                max_ver = max(max_ver, getattr(r, "version", 1))
                if r.status != ScheduleStatus.archived.value:
                    r.status = ScheduleStatus.archived.value
                    r.modified_at = datetime.utcnow()
        return max_ver + 1
    except Exception:
        return 1


def save_schedule_to_db(schedule: ScheduleRead) -> bool:
    """Persist a ScheduleRead to the database.
    Updates the existing record by ID if it already exists; otherwise inserts.
    """
    try:
        with db_session() as db:
            existing = db.query(ScheduleORM).filter_by(id=schedule.id).first()
            if existing:
                existing.status = schedule.status.value
                existing.modified_at = datetime.utcnow()
                existing.is_fallback = getattr(schedule, "is_fallback", False)
                existing.fallback_notes = getattr(schedule, "fallback_notes", None)
                db.query(AssignmentORM).filter_by(schedule_id=schedule.id).delete()
                db.flush()
            else:
                orm = ScheduleORM(
                    id=schedule.id,
                    month=schedule.month,
                    year=schedule.year,
                    status=schedule.status.value,
                    version=getattr(schedule, "version", 1),
                    created_at=schedule.created_at,
                    modified_at=datetime.utcnow(),
                    is_fallback=getattr(schedule, "is_fallback", False),
                    fallback_notes=getattr(schedule, "fallback_notes", None),
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


def restore_archived_schedule(schedule: ScheduleRead) -> ScheduleRead | None:
    """Copy an archived schedule as a new draft with the next version number."""
    try:
        next_ver = archive_existing_schedules.__wrapped__ if False else None  # unused
        with db_session() as db:
            from sqlalchemy import func
            max_ver = (
                db.query(func.max(ScheduleORM.version))
                .filter_by(year=schedule.year, month=schedule.month)
                .scalar()
            ) or 0
        new_id = uuid.uuid4()
        new_assignments = [
            AssignmentRead(
                id=uuid.uuid4(),
                schedule_id=new_id,
                employee_id=a.employee_id,
                date=a.date,
                shift_id=a.shift_id,
                is_day_off=a.is_day_off,
                notes=a.notes,
            )
            for a in schedule.assignments
        ]
        new_schedule = ScheduleRead(
            id=new_id,
            month=schedule.month,
            year=schedule.year,
            status=ScheduleStatus.draft,
            version=max_ver + 1,
            created_at=datetime.utcnow(),
            assignments=new_assignments,
            is_fallback=schedule.is_fallback,
            fallback_notes=schedule.fallback_notes,
        )
        if save_schedule_to_db(new_schedule):
            return new_schedule
        return None
    except Exception as e:
        st.error(f"Restore failed: {e}")
        return None


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


# ── Session state init (must be before widgets) ───────────────────────────────
if "grid_expanded" not in st.session_state:
    st.session_state["grid_expanded"] = False

# ── Layout: two columns (main + chat) ────────────────────────────────────────
# Column ratio widens the chat panel when the user has toggled expanded mode
_chat_expanded = st.session_state.get("chat_expanded", False)

# Full-width return banner shown above everything when in expanded chat mode
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

    # ── Month/Year selector ───────────────────────────────────────────────────
    st.subheader("Select Month")
    col_year, col_month, col_gen, col_load = st.columns([1, 1, 1.2, 1])

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

    # ── Pre-generate availability check ───────────────────────────────────────
    _, _last_day = calendar.monthrange(sel_year, sel_month)
    _month_start = date(sel_year, sel_month, 1)
    _month_end = date(sel_year, sel_month, _last_day)
    _avail_emps = [
        e for e in employees
        if e.availability_start <= _month_end and e.availability_end >= _month_start
    ]
    if not _avail_emps:
        _emp_years = sorted({e.availability_start.year for e in employees})
        st.error(
            f"⛔ **No employees are available in {SEASON_MONTHS[sel_month]} {sel_year}.** "
            f"Employee availability covers: {_emp_years}. "
            "Update availability dates on the Employees page before generating."
        )
    elif not ships:
        st.warning(
            f"⚠️ **No cruise ship arrivals for {SEASON_MONTHS[sel_month]} {sel_year}.** "
            "Upload ship data on the Cruise Ships page — generating without ships may produce an empty schedule."
        )

    # ── Closed Days calendar ──────────────────────────────────────────────────
    # Initialise per-month session state key
    _closed_key = f"closed_days_{sel_year}_{sel_month}"
    if _closed_key not in st.session_state:
        st.session_state[_closed_key] = load_closed_days(sel_year, sel_month)

    with st.expander(
        f"🔒 Closed Days  ({len(st.session_state[_closed_key])} selected)",
        expanded=False,
    ):
        st.caption(
            "Mark days when the café is closed. Closed days are skipped by the solver "
            "and shown as grey columns in the schedule grid."
        )
        _, days_in_month_closed = calendar.monthrange(sel_year, sel_month)
        # Build a checkbox grid week by week (Mon–Sun)
        month_days = [date(sel_year, sel_month, d) for d in range(1, days_in_month_closed + 1)]
        # Pad start of first week
        first_weekday = month_days[0].weekday()  # 0=Mon
        padded = [None] * first_weekday + month_days
        # Pad end to full weeks
        while len(padded) % 7:
            padded.append(None)

        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        header_cols = st.columns(7)
        for i, dn in enumerate(day_names):
            header_cols[i].markdown(f"**{dn}**")

        _pending_closed: set[date] = set()
        for week_start in range(0, len(padded), 7):
            week = padded[week_start:week_start + 7]
            cols = st.columns(7)
            for i, d in enumerate(week):
                if d is None:
                    cols[i].write("")
                else:
                    checked = cols[i].checkbox(
                        str(d.day),
                        value=(d in st.session_state[_closed_key]),
                        key=f"closed_{d.isoformat()}",
                    )
                    if checked:
                        _pending_closed.add(d)

        if st.button("💾 Save Closed Days", key="save_closed_btn"):
            save_closed_days(sel_year, sel_month, _pending_closed)
            st.session_state[_closed_key] = _pending_closed
            st.success(f"Saved {len(_pending_closed)} closed day(s) for {SEASON_MONTHS[sel_month]} {sel_year}.")
            st.rerun()

    _closed_days: set[date] = st.session_state[_closed_key]

    # ── Open / closed day summary ─────────────────────────────────────────────
    _, _days_in_month_total = calendar.monthrange(sel_year, sel_month)
    _season_open_days = []
    try:
        from src.demand.seasonal_rules import get_season
        for _day_n in range(1, _days_in_month_total + 1):
            _d = date(sel_year, sel_month, _day_n)
            if _d in _closed_days:
                continue
            try:
                get_season(_d)
                _season_open_days.append(_d)
            except ValueError:
                pass
    except Exception:
        pass

    _n_closed = len(_closed_days)
    _n_open = len(_season_open_days)
    if _n_closed > 0:
        st.info(
            f"**{SEASON_MONTHS[sel_month]} {sel_year}:** "
            f"{_n_closed} closed day(s), **{_n_open} open day(s)** for scheduling."
        )

    with col_gen:
        st.write("")  # spacing
        generate_clicked = st.button("🔄 Generate New Schedule", type="primary", use_container_width=True)
    with col_load:
        st.write("")
        load_clicked = st.button("📂 Load Saved", use_container_width=True)

    # ── Version selector ──────────────────────────────────────────────────────
    _all_versions = load_all_version_meta(sel_year, sel_month)
    if _all_versions:
        _ver_labels = []
        for _v in _all_versions:
            _dt = (_v["modified_at"] or _v["created_at"])
            _dt_str = _dt.strftime("%b %d") if _dt else "?"
            _ver_labels.append(
                f"v{_v['version']} — {_v['status'].upper()} ({_dt_str})"
            )
        _ver_ids = [_v["id"] for _v in _all_versions]

        _cur_sched = st.session_state.get("current_schedule")
        _default_idx = 0
        if _cur_sched and _cur_sched.year == sel_year and _cur_sched.month == sel_month:
            try:
                _default_idx = _ver_ids.index(str(_cur_sched.id))
            except ValueError:
                _default_idx = 0

        _ver_key = f"_ver_sel_{sel_year}_{sel_month}"
        if len(_all_versions) > 1:
            _sel_ver_label = st.selectbox(
                "Version", _ver_labels, index=_default_idx, key=_ver_key,
                help="Select which version of this month's schedule to view.",
            )
            _sel_ver_idx = _ver_labels.index(_sel_ver_label)
        else:
            _sel_ver_idx = 0
            _sel_ver_status = _all_versions[0]["status"].upper()
            _badge = {"APPROVED": "🟢", "DRAFT": "🟡", "ARCHIVED": "⬛"}.get(_sel_ver_status, "🔵")
            st.caption(f"**{_badge} {_ver_labels[0]}**")

        _selected_ver_id = uuid.UUID(_ver_ids[_sel_ver_idx])

        # Load selected version if it differs from what's in session state
        _cur = st.session_state.get("current_schedule")
        if (
            _cur is None
            or _cur.year != sel_year
            or _cur.month != sel_month
            or _cur.id != _selected_ver_id
        ):
            _loaded_ver = load_schedule_by_id(_selected_ver_id)
            if _loaded_ver:
                st.session_state["current_schedule"] = _loaded_ver
                st.session_state["current_demand"] = generate_monthly_demand(
                    sel_year, sel_month, ships, closed_days=_closed_days
                )
                st.session_state["schedule_year"] = sel_year
                st.session_state["schedule_month"] = sel_month
                for _k in ("_inf_info", "_inf_demand", "_inf_year", "_inf_month", "_fallback_result"):
                    st.session_state.pop(_k, None)
                st.rerun()

    # ── Helper: render SolveInfo diagnostics ─────────────────────────────────
    def _render_solver_diagnostics(info) -> None:
        cols = st.columns(4)
        cols[0].metric("Status", info.status_name)
        cols[1].metric("Variables", f"{info.num_variables:,}")
        cols[2].metric("Available employees", info.num_employees_available)
        cols[3].metric("Working shifts", info.num_working_assignments)
        if info.wall_time > 0:
            st.caption(
                f"Solver wall time: {info.wall_time:.2f}s  |  "
                f"Days in month: {info.num_days}  |  "
                f"Objective: {info.objective_value:.0f}"
            )
        if info.warnings:
            st.markdown("**⚠️ Warnings:**")
            for w in info.warnings:
                st.warning(w)
        if info.diagnostics:
            st.markdown("**❌ Issues:**")
            for d_msg in info.diagnostics:
                st.error(d_msg)

    # ── Generate: check for existing schedule and prompt confirmation ────────
    if generate_clicked:
        if not _avail_emps:
            st.error("Cannot generate: no employees are available for this month. Fix availability dates first.")
        elif not ships:
            st.warning("Generating without ship data — demand will be minimal (base staffing only).")

    if generate_clicked and _avail_emps:
        _active_existing = load_saved_schedule(sel_year, sel_month)
        if _active_existing:
            st.session_state["_confirm_generate"] = True
            st.session_state["_confirm_gen_year"] = sel_year
            st.session_state["_confirm_gen_month"] = sel_month
            st.rerun()
        else:
            st.session_state["_do_generate"] = True
            st.rerun()

    # ── Confirmation dialog ───────────────────────────────────────────────────
    if (
        st.session_state.get("_confirm_generate")
        and st.session_state.get("_confirm_gen_year") == sel_year
        and st.session_state.get("_confirm_gen_month") == sel_month
    ):
        _existing_for_confirm = load_saved_schedule(sel_year, sel_month)
        _existing_status = _existing_for_confirm.status.value.upper() if _existing_for_confirm else "?"
        st.warning(
            f"⚠️ You already have a **{_existing_status}** schedule for "
            f"**{SEASON_MONTHS[sel_month]} {sel_year}** (v{_existing_for_confirm.version if _existing_for_confirm else '?'}). "
            "Generating a new schedule will **archive the current one**. "
            "You can switch back to it later using the version selector."
        )
        _cc1, _cc2 = st.columns(2)
        if _cc1.button("🔄 Generate New Schedule", type="primary", use_container_width=True, key="confirm_gen_btn"):
            for _k in ("_confirm_generate", "_confirm_gen_year", "_confirm_gen_month"):
                st.session_state.pop(_k, None)
            st.session_state["_do_generate"] = True
            st.session_state["_do_archive_first"] = True
            st.rerun()
        if _cc2.button("Cancel", use_container_width=True, key="cancel_gen_btn"):
            for _k in ("_confirm_generate", "_confirm_gen_year", "_confirm_gen_month"):
                st.session_state.pop(_k, None)
            st.rerun()

    # ── Generate first pass ───────────────────────────────────────────────────
    if st.session_state.get("_do_generate"):
        st.session_state.pop("_do_generate", None)
        if st.session_state.pop("_do_archive_first", False):
            _next_ver = archive_existing_schedules(sel_year, sel_month)
        else:
            _next_ver = 1
        # Clear any prior infeasible/fallback state for a fresh attempt
        for _k in ("_inf_info", "_inf_demand", "_inf_year", "_inf_month", "_fallback_result"):
            st.session_state.pop(_k, None)
        with st.spinner(f"Generating {SEASON_MONTHS[sel_month]} {sel_year} schedule (up to 60s)…"):
            try:
                demand = generate_monthly_demand(sel_year, sel_month, ships, closed_days=_closed_days)
                if not demand:
                    st.error("No demand generated — check that the selected month is within the operating season (May–October).")
                else:
                    gen = ScheduleGenerator(employees, demand, shifts)
                    gen.build_model()
                    result = gen.solve()
                    info = gen.solve_info

                    if result is None:
                        st.session_state["_inf_info"] = info
                        st.session_state["_inf_demand"] = demand
                        st.session_state["_inf_year"] = sel_year
                        st.session_state["_inf_month"] = sel_month
                    else:
                        # Stamp version on the generated result
                        result = ScheduleRead(
                            id=result.id, month=result.month, year=result.year,
                            status=result.status, version=_next_ver,
                            created_at=result.created_at,
                            assignments=result.assignments,
                            is_fallback=getattr(result, "is_fallback", False),
                            fallback_notes=getattr(result, "fallback_notes", None),
                        )
                        st.session_state["current_schedule"] = result
                        st.session_state["current_demand"] = demand
                        st.session_state["schedule_year"] = sel_year
                        st.session_state["schedule_month"] = sel_month
                        # Reset version selector to show new version
                        st.session_state.pop(f"_ver_sel_{sel_year}_{sel_month}", None)
                        n_working = sum(1 for a in result.assignments if not a.is_day_off)
                        with st.expander("🔍 Solver diagnostics"):
                            _render_solver_diagnostics(info)
                        st.success(
                            f"✅ Schedule generated! v{_next_ver}  \n"
                            f"**{n_working}** working assignments across **{len(demand)}** days  \n"
                            f"Status: `{info.status_name}` — wall time: {info.wall_time:.1f}s"
                        )
            except Exception as e:
                st.error(f"Generation failed: {e}")
                import traceback
                st.code(traceback.format_exc(), language="text")

    # ── Infeasible state: diagnostics + fallback button ───────────────────────
    _inf_info = st.session_state.get("_inf_info")
    _inf_year = st.session_state.get("_inf_year")
    _inf_month = st.session_state.get("_inf_month")

    if _inf_info and _inf_year == sel_year and _inf_month == sel_month:
        st.error(
            f"**Schedule generation failed** (solver status: `{_inf_info.status_name}`).  \n"
            "The current constraints cannot be satisfied with the available employees."
        )
        with st.expander("🔍 Solver diagnostics", expanded=True):
            _render_solver_diagnostics(_inf_info)

        st.divider()
        st.warning(
            "**No valid schedule found with all constraints active.**  \n"
            "You can generate a best-effort schedule where non-critical constraints "
            "are progressively relaxed until a solution is found."
        )
        st.caption(
            "**Never relaxed:** opening hours coverage (≥1 in café at all times) · "
            "max 6 consecutive working days · daily/weekly rest periods · "
            "age-based limits · Eidsdal driver requirement · staffing caps"
        )
        if st.button(
            "⚡ Generate Best-Effort Schedule — some constraints will be relaxed to find a workable solution",
            type="primary",
            use_container_width=True,
            key="fallback_btn",
        ):
            _demand_fb = st.session_state.get("_inf_demand", [])
            if _demand_fb:
                with st.spinner(
                    "Running fallback solver — trying progressive constraint relaxation "
                    "(up to 4 passes × 60s each)…"
                ):
                    try:
                        fb_result = run_fallback_solve(employees, _demand_fb, shifts)
                    except Exception as _fe:
                        st.error(f"Fallback solver error: {_fe}")
                        import traceback
                        st.code(traceback.format_exc(), language="text")
                        fb_result = None

                if fb_result is not None:
                    _fb_next_ver = archive_existing_schedules(sel_year, sel_month)
                    fb_schedule = ScheduleRead(
                        id=fb_result.schedule.id,
                        month=fb_result.schedule.month,
                        year=fb_result.schedule.year,
                        status=ScheduleStatus.draft,
                        version=_fb_next_ver,
                        created_at=fb_result.schedule.created_at,
                        assignments=fb_result.schedule.assignments,
                        is_fallback=True,
                        fallback_notes=fb_result.notes_json(),
                    )
                    st.session_state["current_schedule"] = fb_schedule
                    st.session_state["current_demand"] = _demand_fb
                    st.session_state["schedule_year"] = sel_year
                    st.session_state["schedule_month"] = sel_month
                    st.session_state["_fallback_result"] = fb_result
                    st.session_state.pop(f"_ver_sel_{sel_year}_{sel_month}", None)
                    for _k in ("_inf_info", "_inf_demand", "_inf_year", "_inf_month"):
                        st.session_state.pop(_k, None)
                    st.rerun()
                else:
                    st.error(
                        "⛔ **Fallback solver exhausted all relaxation steps** — no workable "
                        "schedule found. This usually means there are not enough employees to "
                        "cover opening hours (≥1 in café at all times) while respecting rest "
                        "constraints. Options: add more employees, adjust availability dates, "
                        "or mark some low-demand days as **Closed** using the calendar above."
                    )

    # ── Load saved ────────────────────────────────────────────────────────────
    if load_clicked:
        saved = load_saved_schedule(sel_year, sel_month)
        if saved:
            demand = generate_monthly_demand(sel_year, sel_month, ships, closed_days=_closed_days)
            st.session_state["current_schedule"] = saved
            st.session_state["current_demand"] = demand
            st.session_state["schedule_year"] = sel_year
            st.session_state["schedule_month"] = sel_month
            for _k in ("_inf_info", "_inf_demand", "_inf_year", "_inf_month", "_fallback_result"):
                st.session_state.pop(_k, None)
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

            # ── Status badge ──────────────────────────────────────────────────
            _status_val = schedule.status
            _badge_icon = {"approved": "🟢", "draft": "🟡", "archived": "⬛"}.get(
                _status_val.value, "🔵"
            )
            _ver_num = getattr(schedule, "version", 1)
            st.markdown(
                f"**{_badge_icon} {_status_val.value.upper()}** &nbsp;·&nbsp; "
                f"Version {_ver_num} &nbsp;·&nbsp; "
                f"ID: `{str(schedule.id)[:8]}…`"
            )

            # ── Archived banner ───────────────────────────────────────────────
            _is_archived = _status_val == ScheduleStatus.archived
            if _is_archived:
                with st.container(border=True):
                    st.warning(
                        "⬛ **This is an archived version.** To make changes, generate a new "
                        "schedule or restore this version as a new draft."
                    )
                    if st.button(
                        "♻️ Restore This Version as New Draft",
                        type="primary",
                        use_container_width=True,
                        key="restore_archived_btn",
                    ):
                        _restored = restore_archived_schedule(schedule)
                        if _restored:
                            st.session_state["current_schedule"] = _restored
                            st.session_state["schedule_year"] = sel_year
                            st.session_state["schedule_month"] = sel_month
                            st.session_state.pop(f"_ver_sel_{sel_year}_{sel_month}", None)
                            st.success(f"✅ Restored as v{_restored.version} (draft).")
                            st.rerun()

            # Flash banner when LLM Apply updates the grid
            if "_apply_flash" in st.session_state:
                st.success(f"✅ {st.session_state.pop('_apply_flash')} — schedule grid updated below")

            # Cross-month warning: persisted across rerun (from Approve path)
            if "_cross_month_warn_sched" in st.session_state:
                st.warning(st.session_state.pop("_cross_month_warn_sched"))

            # Cross-month stale warning: shown whenever prev month was edited after
            # this schedule was generated (always-on check)
            _stale = _stale_prev_month_warning(schedule)
            if _stale:
                st.warning(f"⚠️ {_stale}")

            # Fallback banner: shown when schedule was generated in fallback mode
            if getattr(schedule, "is_fallback", False):
                _fb_notes = relaxation_notes_from_json(schedule.fallback_notes or "")
                _is_skeleton = is_skeleton_from_json(schedule.fallback_notes)

                if _is_skeleton:
                    st.error(
                        "🚨 **SKELETON SCHEDULE** — This is an absolute minimum schedule. "
                        "Production staffing has been set to zero and several scheduling "
                        "constraints have been dropped. **This schedule is not suitable for "
                        "normal operations.** Add more staff or mark busy days as closed."
                    )
                    st.caption(
                        "Skeleton mode dropped: 14-day paired rest · "
                        "Sunday rest requirements · staffing caps  |  "
                        "Opening hours coverage: still enforced"
                    )
                else:
                    with st.container(border=True):
                        st.warning(
                            "⚠️ **Best-effort schedule** — this schedule was generated with "
                            "relaxed constraints because the normal solver could not find a "
                            "valid solution with the current number of available employees."
                        )
                        if _fb_notes:
                            st.markdown("**Constraints that were relaxed:**")
                            for _note in _fb_notes:
                                st.markdown(f"- {_note}")

                # Staffing gaps — try live result first, fall back to stored JSON
                _fb_result = st.session_state.get("_fallback_result")
                _gaps = _fb_result.staffing_gaps if _fb_result else staffing_gaps_from_json(schedule.fallback_notes or "")
                if _gaps:
                    with st.expander(
                        f"📋 Staffing Gaps — {len(_gaps)} day(s) below normal requirements "
                        "(focus hiring or manual adjustments here)",
                        expanded=_is_skeleton,
                    ):
                        for _gap in _gaps:
                            st.write(f"🔴 {_gap.description()}")

            # ── Action buttons ────────────────────────────────────────────────
            _is_approved = schedule.status == ScheduleStatus.approved
            if not _is_archived:
                st.info(
                    "💾 **Save Draft** to keep editing later.  "
                    "🏁 **Approve & Finalize** to lock the schedule.  "
                    "📊 **Export** is available after approval.",
                    icon="ℹ️",
                )
            act1, act2, act3, act4 = st.columns(4)

            with act1:
                if st.button(
                    "💾 Save Draft",
                    use_container_width=True,
                    disabled=_is_approved or _is_archived,
                    help="Disabled for approved or archived schedules." if (_is_approved or _is_archived) else None,
                ):
                    if save_schedule_to_db(schedule):
                        st.success("Saved as draft — you can keep editing.")
                        _next = _next_month_name_if_exists(schedule.year, schedule.month)
                        if _next:
                            curr_name = f"{SEASON_MONTHS[schedule.month]} {schedule.year}"
                            st.warning(
                                f"⚠️ You have modified the **{curr_name}** schedule. "
                                f"The **{_next}** schedule was generated based on the previous "
                                f"version — consider regenerating **{_next}** to ensure consistency."
                            )

            with act2:
                if _is_archived:
                    st.button("🏁 Approve & Finalize", use_container_width=True, disabled=True,
                              help="Archived schedules cannot be approved. Restore first.")
                elif not _is_approved:
                    if st.button("🏁 Approve & Finalize", use_container_width=True, type="primary"):
                        if save_schedule_to_db(schedule):
                            if approve_schedule_in_db(schedule.id):
                                updated = ScheduleRead(
                                    id=schedule.id, month=schedule.month, year=schedule.year,
                                    status=ScheduleStatus.approved,
                                    version=getattr(schedule, "version", 1),
                                    created_at=schedule.created_at,
                                    modified_at=datetime.utcnow(),
                                    assignments=schedule.assignments,
                                )
                                st.session_state["current_schedule"] = updated
                                st.success("✅ Schedule approved and finalized!")
                                _next = _next_month_name_if_exists(schedule.year, schedule.month)
                                if _next:
                                    curr_name = f"{SEASON_MONTHS[schedule.month]} {schedule.year}"
                                    st.session_state["_cross_month_warn_sched"] = (
                                        f"⚠️ You have modified the **{curr_name}** schedule. "
                                        f"The **{_next}** schedule was generated based on the "
                                        f"previous version — consider regenerating **{_next}** "
                                        "to ensure consistency."
                                    )
                                st.rerun()
                else:
                    st.success("✅ Approved & finalized")

            with act3:
                if st.button(
                    "📊 Export →",
                    use_container_width=True,
                    disabled=not _is_approved,
                    help="Approve the schedule first to enable export." if not _is_approved else None,
                ):
                    st.info("Use the Export page in the sidebar to download the schedule.")

            with act4:
                if st.button("🔄 Clear View", use_container_width=True):
                    st.session_state.pop("current_schedule", None)
                    st.session_state.pop("current_demand", None)
                    st.rerun()

            st.caption(
                f"Created: {schedule.created_at.strftime('%Y-%m-%d %H:%M')}"
                + (f" · Modified: {schedule.modified_at.strftime('%Y-%m-%d %H:%M')}" if schedule.modified_at else "")
            )

            # ── Schedule Grid ─────────────────────────────────────────────────
            _grid_exp = st.session_state.get("grid_expanded", False)
            _grid_hdr, _grid_btn = st.columns([5, 1])
            _grid_hdr.subheader(f"📅 {SEASON_MONTHS[sel_month]} {sel_year} — Schedule Grid")
            if _grid_btn.button(
                "📅 Collapse" if _grid_exp else "🔍 Expand",
                key="grid_expand_btn",
                use_container_width=True,
            ):
                st.session_state["grid_expanded"] = not _grid_exp
                st.rerun()

            if not _grid_exp:
                render_schedule_grid(schedule, employees, shifts, demand_list, height=600, closed_days=_closed_days)
            else:
                st.info("⬇️ Full-width schedule grid displayed below — collapse to return to split view.")

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

# ── Full-width schedule grid (expanded mode) ──────────────────────────────────
if st.session_state.get("grid_expanded"):
    _fw_schedule = st.session_state.get("current_schedule")
    _fw_demand = st.session_state.get("current_demand", [])
    _fw_employees = employees if "employees" in locals() else []
    _fw_shifts = shifts if "shifts" in locals() else []
    _fw_closed = st.session_state.get(
        f"closed_days_{st.session_state.get('schedule_year', sel_year)}_{st.session_state.get('schedule_month', sel_month)}",
        set(),
    )
    if _fw_schedule and _fw_demand and _fw_employees and _fw_shifts:
        st.divider()
        _fw_hdr, _fw_btn = st.columns([5, 1])
        _fw_hdr.subheader(
            f"📅 {SEASON_MONTHS.get(st.session_state.get('schedule_month', sel_month), '')} "
            f"{st.session_state.get('schedule_year', sel_year)} — Schedule Grid (Full Width)"
        )
        if _fw_btn.button("📅 Collapse", key="collapse_grid_fw", use_container_width=True):
            st.session_state["grid_expanded"] = False
            st.rerun()
        render_schedule_grid(_fw_schedule, _fw_employees, _fw_shifts, _fw_demand, height=900, closed_days=_fw_closed)
