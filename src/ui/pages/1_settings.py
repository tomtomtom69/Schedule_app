"""Settings page — establishment parameters, seasons, shift templates, staffing rules."""
from __future__ import annotations

from datetime import date, time

import pandas as pd
import streamlit as st

from src.db.database import db_session
from src.demand.seasonal_rules import STAFFING_RULES, Season
from src.models.establishment import EstablishmentSettingsORM
from src.models.shift_template import ShiftTemplateORM

st.set_page_config(page_title="Settings", page_icon="⚙️", layout="wide")
from src.ui.components.sidebar import render_shift_legend
render_shift_legend()
st.title("⚙️ Settings")

tab1, tab2, tab3, tab4 = st.tabs(
    ["Season & Hours", "Shift Templates", "Staffing Rules", "Eidsdal Transport"]
)

# ── Tab 1: Season Configuration ──────────────────────────────────────────────
with tab1:
    st.subheader("Season Configurations")
    st.caption("Define operating seasons, opening/closing times, and production start.")

    try:
        with db_session() as db:
            season_data = [
                {
                    "ID": s.id,
                    "Season": s.season.upper(),
                    "Start": s.date_range_start.isoformat(),
                    "End": s.date_range_end.isoformat(),
                    "Opening": str(s.opening_time)[:5],
                    "Closing": str(s.closing_time)[:5],
                    "Production Start": str(s.production_start)[:5],
                    "Max Café/Day": getattr(s, "max_cafe_per_day", 5) or 5,
                    "Max Prod/Day": getattr(s, "max_prod_per_day", 4) or 4,
                }
                for s in db.query(EstablishmentSettingsORM).order_by(
                    EstablishmentSettingsORM.date_range_start
                ).all()
            ]

        if season_data:
            df = pd.DataFrame(season_data)

            edited = st.data_editor(
                df,
                use_container_width=True,
                column_config={
                    "ID": st.column_config.NumberColumn("ID", disabled=True),
                    "Season": st.column_config.SelectboxColumn(
                        "Season",
                        options=["LOW", "MID", "PEAK"],
                    ),
                    "Start": st.column_config.TextColumn("Start (YYYY-MM-DD)"),
                    "End": st.column_config.TextColumn("End (YYYY-MM-DD)"),
                    "Opening": st.column_config.TextColumn("Opening (HH:MM)"),
                    "Closing": st.column_config.TextColumn("Closing (HH:MM)"),
                    "Production Start": st.column_config.TextColumn("Prod Start (HH:MM)"),
                    "Max Café/Day": st.column_config.NumberColumn(
                        "Max Café/Day",
                        min_value=1, max_value=20,
                        help="Hard cap on café staff per day (raised to cap+1 if ≥2 good ships in port).",
                    ),
                    "Max Prod/Day": st.column_config.NumberColumn(
                        "Max Prod/Day",
                        min_value=0, max_value=20,
                        help="Hard cap on production staff per day.",
                    ),
                },
                hide_index=True,
                num_rows="fixed",
                key="season_editor",
            )

            if st.button("Save Season Configurations", type="primary"):
                errors = []
                try:
                    with db_session() as db:
                        for _, row in edited.iterrows():
                            orm = db.query(EstablishmentSettingsORM).filter_by(id=int(row["ID"])).first()
                            if orm is None:
                                continue
                            orm.season = row["Season"].lower()
                            orm.date_range_start = date.fromisoformat(row["Start"])
                            orm.date_range_end = date.fromisoformat(row["End"])
                            h, m = map(int, row["Opening"].split(":"))
                            orm.opening_time = time(h, m)
                            h, m = map(int, row["Closing"].split(":"))
                            orm.closing_time = time(h, m)
                            h, m = map(int, row["Production Start"].split(":"))
                            orm.production_start = time(h, m)
                            orm.max_cafe_per_day = int(row["Max Café/Day"])
                            orm.max_prod_per_day = int(row["Max Prod/Day"])
                    st.success("Season configurations saved.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Save failed: {e}")
        else:
            st.info("No season configurations found. Run the application to seed defaults.")

    except Exception as e:
        st.error(f"Could not load seasons: {e}")


# ── Tab 2: Shift Templates ────────────────────────────────────────────────────
with tab2:
    st.subheader("Shift Templates")
    st.caption(
        "Café shifts: IDs 1–9. Production shifts: IDs P1–P9. "
        "Max shift duration: 10 hours."
    )

    try:
        with db_session() as db:
            shift_data = [
                {
                    "ID": s.id,
                    "Role": s.role,
                    "Label": s.label,
                    "Start": str(s.start_time)[:5],
                    "End": str(s.end_time)[:5],
                }
                for s in db.query(ShiftTemplateORM).order_by(ShiftTemplateORM.id).all()
            ]
        df_shifts = pd.DataFrame(shift_data) if shift_data else pd.DataFrame(
            columns=["ID", "Role", "Label", "Start", "End"]
        )

        edited_shifts = st.data_editor(
            df_shifts,
            use_container_width=True,
            column_config={
                "ID": st.column_config.TextColumn("ID", help="e.g. '6' or 'P3'"),
                "Role": st.column_config.SelectboxColumn(
                    "Role", options=["cafe", "production"]
                ),
                "Label": st.column_config.TextColumn("Label"),
                "Start": st.column_config.TextColumn("Start (HH:MM)"),
                "End": st.column_config.TextColumn("End (HH:MM)"),
            },
            hide_index=True,
            num_rows="dynamic",
            key="shift_editor",
        )

        if st.button("Save Shift Templates", type="primary"):
            try:
                with db_session() as db:
                    # Delete all and re-insert (simple replace strategy)
                    db.query(ShiftTemplateORM).delete()
                    for _, row in edited_shifts.iterrows():
                        sid = str(row["ID"]).strip()
                        start_h, start_m = map(int, row["Start"].split(":"))
                        end_h, end_m = map(int, row["End"].split(":"))
                        start_t = time(start_h, start_m)
                        end_t = time(end_h, end_m)

                        duration_min = (end_h * 60 + end_m) - (start_h * 60 + start_m)
                        if duration_min <= 0:
                            st.error(f"Shift {sid}: end must be after start.")
                            continue
                        if duration_min > 600:
                            st.error(f"Shift {sid}: exceeds 10-hour maximum.")
                            continue

                        db.add(ShiftTemplateORM(
                            id=sid,
                            role=row["Role"],
                            label=row["Label"],
                            start_time=start_t,
                            end_time=end_t,
                        ))
                st.success("Shift templates saved.")
                st.rerun()
            except Exception as e:
                st.error(f"Save failed: {e}")

    except Exception as e:
        st.error(f"Could not load shift templates: {e}")


# ── Tab 3: Staffing Rules ─────────────────────────────────────────────────────
with tab3:
    st.subheader("Staffing Rules")
    st.caption(
        "Shows the default staffing levels per season and scenario from the demand engine. "
        "These are code-defined defaults in Phase 4. "
        "DB-editable overrides will be added in a future phase."
    )

    for season in [Season.low, Season.mid, Season.peak]:
        rules = STAFFING_RULES.get(season, {})
        st.markdown(f"**{season.value.upper()} Season**")
        rows = []
        for scenario, counts in rules.items():
            rows.append({
                "Scenario": scenario.replace("_", " ").title(),
                "Production": counts.get("production", 0),
                "Café": counts.get("cafe", 0),
                "Total": counts.get("production", 0) + counts.get("cafe", 0),
            })
        if rows:
            df_rules = pd.DataFrame(rows)
            st.dataframe(df_rules, use_container_width=True, hide_index=True)
        st.divider()


# ── Tab 4: Eidsdal Transport Settings ────────────────────────────────────────
with tab4:
    st.subheader("Eidsdal Transport Settings")
    st.caption(
        "Employees in Eidsdal are transported by car. "
        "At least 1 licensed driver is required if any Eidsdal employee works; "
        "2 drivers required if more than 5 Eidsdal workers are scheduled."
    )

    # Current settings are code-level constants in src/solver/transport.py
    from src.solver.transport import EIDSDAL_CARS, MAX_EIDSDAL_WORKERS, SEATS_PER_CAR

    col1, col2, col3 = st.columns(3)
    col1.metric("Cars available", EIDSDAL_CARS)
    col2.metric("Seats per car", SEATS_PER_CAR)
    col3.metric("Max Eidsdal workers/day", MAX_EIDSDAL_WORKERS)

    st.info(
        "Transport capacity is enforced as a hard constraint in the schedule solver. "
        "To change these values, update `src/solver/transport.py`. "
        "DB-configurable transport settings are planned for a future phase."
    )

    # Show Eidsdal employees from DB
    try:
        from src.models.employee import EmployeeORM

        with db_session() as db:
            eidsdal_emps = [
                {
                    "name": e.name,
                    "role": e.role_capability.value if hasattr(e.role_capability, 'value') else e.role_capability,
                    "type": e.employment_type.value if hasattr(e.employment_type, 'value') else e.employment_type,
                    "driver": e.driving_licence,
                }
                for e in db.query(EmployeeORM).filter(EmployeeORM.housing == "eidsdal").all()
            ]

        if eidsdal_emps:
            st.markdown(f"**Eidsdal employees ({len(eidsdal_emps)}):**")
            for e in eidsdal_emps:
                icon = "🚗" if e["driver"] else "👤"
                st.write(f"{icon} {e['name']} — {e['role']}, {e['type']}")
            drivers = [e for e in eidsdal_emps if e["driver"]]
            if len(drivers) == 0:
                st.error("No licensed drivers in Eidsdal! Solver will be infeasible.")
            elif len(drivers) == 1:
                st.warning(
                    f"Only 1 driver ({drivers[0]['name']}). "
                    "If more than 5 Eidsdal workers are scheduled, 2 drivers are required."
                )
            else:
                st.success(f"{len(drivers)} licensed drivers available.")
        else:
            st.info("No Eidsdal employees in the database.")
    except Exception as e:
        st.error(f"Could not load Eidsdal employees: {e}")
