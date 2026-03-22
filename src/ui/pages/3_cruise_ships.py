"""Cruise Ships page — upload schedules and view calendar."""
from __future__ import annotations

import streamlit as st

from src.db.database import db_session
from src.ingestion.csv_parser import parse_cruise_ships_csv
from src.ingestion.validators import validate_cruise_schedule
from src.models.cruise_ship import CruiseShipORM, CruiseShipRead
from src.models.employee import EmployeeORM

st.set_page_config(page_title="Cruise Ships", page_icon="🚢", layout="wide")
st.title("🚢 Cruise Ships")

# ── Unsaved data warning ──────────────────────────────────────────────────────
_unsaved_ships = st.session_state.get("ships_unsaved", 0)
if _unsaved_ships:
    st.warning(
        f"⚠️ **{_unsaved_ships} ship arrival(s)** parsed but NOT yet saved. "
        "Go to the **Upload Ships CSV** tab and click Save to keep your data.",
        icon="⚠️",
    )
    st.components.v1.html(
        "<script>"
        "window.parent.onbeforeunload = function() {"
        "  return 'You have unsaved cruise ship data. Are you sure you want to leave?';"
        "};"
        "</script>",
        height=0,
    )

tab_view, tab_calendar, tab_ships = st.tabs(
    ["Ship List", "Calendar View", "Upload Ships CSV"]
)


# ── Tab 1: Ship List ──────────────────────────────────────────────────────────
with tab_view:
    st.subheader("Cruise Ship Schedule")
    try:
        with db_session() as db:
            all_ship_data = [
                {
                    "Date": s.date,
                    "Ship": s.ship_name,
                    "Port": str(s.port),
                    "Arrival": str(s.arrival_time)[:5],
                    "Departure": str(s.departure_time)[:5],
                    "Size": str(s.size),
                    "Good Ship": "⭐ Yes" if s.good_ship else "No",
                    "Language": s.extra_language or "—",
                    "_month": s.date.strftime("%Y-%m"),
                    "_good": s.good_ship,
                    "_geiranger": "geiranger" in str(s.port),
                    "_hellesylt": str(s.port) == "hellesylt",
                }
                for s in db.query(CruiseShipORM).order_by(
                    CruiseShipORM.date, CruiseShipORM.arrival_time
                ).all()
            ]

        if all_ship_data:
            total_ships = len(all_ship_data)
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Total Arrivals", total_ships)
            col2.metric("Good Ships", sum(1 for s in all_ship_data if s["_good"]))
            col3.metric("Geiranger", sum(1 for s in all_ship_data if s["_geiranger"]))
            col4.metric("Hellesylt", sum(1 for s in all_ship_data if s["_hellesylt"]))

            months_available = sorted({s["_month"] for s in all_ship_data})
            selected_month = st.selectbox(
                "Filter by month",
                options=["All"] + months_available,
                index=0,
            )

            filtered = [
                s for s in all_ship_data
                if selected_month == "All" or s["_month"] == selected_month
            ]
            display_cols = ["Date", "Ship", "Port", "Arrival", "Departure", "Size", "Good Ship", "Language"]
            st.dataframe(
                [{k: s[k] for k in display_cols} for s in filtered],
                use_container_width=True, hide_index=True,
            )
            st.caption(f"Showing {len(filtered)} of {total_ships} ship arrivals")
        else:
            st.info("No cruise ships in the database. Upload a schedule CSV.")

    except Exception as e:
        st.error(f"Could not load cruise ships: {e}")


# ── Tab 2: Calendar View ──────────────────────────────────────────────────────
with tab_calendar:
    st.subheader("Monthly Calendar View")

    try:
        with db_session() as db:
            ship_reads = [
                CruiseShipRead(
                    id=s.id, ship_name=s.ship_name, date=s.date,
                    arrival_time=s.arrival_time, departure_time=s.departure_time,
                    port=s.port, size=s.size, good_ship=s.good_ship,
                    extra_language=s.extra_language,
                )
                for s in db.query(CruiseShipORM).order_by(CruiseShipORM.date).all()
            ]
            months = sorted({(s.date.year, s.date.month) for s in ship_reads})

        if not ship_reads:
            st.info("No cruise ships in database. Upload a schedule CSV first.")
        else:
            # Month selector
            months = sorted({(s.date.year, s.date.month) for s in ship_reads})
            month_labels = [f"{m[0]}-{m[1]:02d}" for m in months]

            if months:
                selected_label = st.selectbox(
                    "Select month",
                    options=month_labels,
                    index=0,
                    key="cal_month",
                )
                sel_year, sel_month = int(selected_label[:4]), int(selected_label[5:7])

                from src.ui.components.ship_calendar import render_ship_calendar
                render_ship_calendar(sel_year, sel_month, ship_reads, height=520)
            else:
                st.info("No months available.")

    except Exception as e:
        st.error(f"Could not render calendar: {e}")


# ── Tab 3: Upload Ships CSV ───────────────────────────────────────────────────
with tab_ships:
    st.subheader("Upload Cruise Ship Schedule")
    st.markdown(
        """
        **Required columns:**
        `ship_name, date, arrival_time, departure_time, port, size, good_ship`

        **Port values:** `geiranger_4B_SW`, `geiranger_3S`, `geiranger_2`, `hellesylt`

        **Example:**
        ```
        ship_name,date,arrival_time,departure_time,port,size,good_ship
        Costa Diadema,2026-08-04,11:30,19:30,geiranger_4B_SW,big,false
        ```
        """
    )
    uploaded_ships = st.file_uploader("Choose file", type=["csv", "xlsx"], key="ships_upload")

    if uploaded_ships:
        records, errors = parse_cruise_ships_csv(uploaded_ships)

        if errors:
            st.error(f"{len(errors)} row(s) failed validation:")
            for err in errors:
                st.write(f"- Row {err['row']}: {err['error']}")

        if records:
            st.session_state["ships_unsaved"] = len(records)
            st.success(f"{len(records)} ship arrival(s) parsed.")

            warnings = validate_cruise_schedule(records)
            for w in warnings:
                st.warning(w)

            import pandas as pd
            preview = [
                {
                    "Date": str(r.date),
                    "Ship": r.ship_name,
                    "Port": str(r.port),
                    "Arrival": str(r.arrival_time)[:5],
                    "Departure": str(r.departure_time)[:5],
                    "Size": str(r.size),
                    "Good Ship": r.good_ship,
                }
                for r in records
            ]
            st.dataframe(preview, use_container_width=True, hide_index=True)

            st.divider()
            with st.container(border=True):
                st.markdown(
                    f"### 🚢 Save {len(records)} Ship Arrival(s) to Database",
                    help="Upserts by (ship_name, date) — existing records will be updated.",
                )
                if st.button(
                    f"✅ Save {len(records)} Ship Arrival(s) to Database",
                    type="primary",
                    use_container_width=True,
                    key="save_ships_btn",
                ):
                    try:
                        with db_session() as db:
                            for record in records:
                                data = record.model_dump()
                                existing = (
                                    db.query(CruiseShipORM)
                                    .filter_by(ship_name=record.ship_name, date=record.date)
                                    .first()
                                )
                                if existing:
                                    for k, v in data.items():
                                        setattr(existing, k, v)
                                else:
                                    db.add(CruiseShipORM(**data))
                        st.session_state.pop("ships_unsaved", None)
                        st.components.v1.html(
                            "<script>window.parent.onbeforeunload = null;</script>",
                            height=0,
                        )
                        st.success(
                            f"✅ **{len(records)} ship arrival(s) saved successfully!**  \n"
                            "The Ship List tab now shows the updated schedule."
                        )
                        st.balloons()
                        st.rerun()
                    except Exception as e:
                        st.error(f"Save failed: {e}")

