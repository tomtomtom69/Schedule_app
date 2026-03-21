"""Cruise Ships page — upload schedules, language mappings, and view calendar."""
from __future__ import annotations

import streamlit as st

from src.db.database import db_session
from src.ingestion.csv_parser import parse_cruise_ships_csv, parse_ship_languages_csv
from src.ingestion.validators import validate_cruise_schedule
from src.models.cruise_ship import CruiseShipORM, CruiseShipRead, ShipLanguageORM
from src.models.employee import EmployeeORM

st.set_page_config(page_title="Cruise Ships", page_icon="🚢", layout="wide")
st.title("🚢 Cruise Ships")

tab_view, tab_calendar, tab_ships, tab_langs = st.tabs(
    ["Ship List", "Calendar View", "Upload Ships CSV", "Upload Language Mapping"]
)


# ── Tab 1: Ship List ──────────────────────────────────────────────────────────
with tab_view:
    st.subheader("Cruise Ship Schedule")
    try:
        with db_session() as db:
            langs = {r.ship_name: r.primary_language for r in db.query(ShipLanguageORM).all()}
            all_ship_data = [
                {
                    "Date": s.date,
                    "Ship": s.ship_name,
                    "Port": str(s.port),
                    "Arrival": str(s.arrival_time)[:5],
                    "Departure": str(s.departure_time)[:5],
                    "Size": str(s.size),
                    "Good Ship": "⭐ Yes" if s.good_ship else "No",
                    "Language": langs.get(s.ship_name, s.extra_language or "—"),
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

            if st.button("Save Ships to Database", type="primary"):
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
                    st.success(f"Saved {len(records)} ship arrivals.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Save failed: {e}")


# ── Tab 4: Upload Language Mapping ────────────────────────────────────────────
with tab_langs:
    st.subheader("Upload Ship Language Mapping")
    st.markdown(
        """
        **Required columns:** `ship_name, primary_language`

        **Example:**
        ```
        ship_name,primary_language
        Costa Diadema,italian
        AIDA,german
        ```
        """
    )

    # Show current mapping
    try:
        with db_session() as db:
            current_langs = [
                {"Ship": r.ship_name, "Language": r.primary_language}
                for r in db.query(ShipLanguageORM).order_by(ShipLanguageORM.ship_name).all()
            ]
        if current_langs:
            st.caption(f"Current mappings: {len(current_langs)}")
            import pandas as pd
            st.dataframe(current_langs, use_container_width=True, hide_index=True)
        else:
            st.info("No language mappings stored yet.")
    except Exception as e:
        st.error(f"Could not load language mappings: {e}")

    uploaded_langs = st.file_uploader("Choose file", type=["csv", "xlsx"], key="langs_upload")

    if uploaded_langs:
        records, errors = parse_ship_languages_csv(uploaded_langs)

        if errors:
            st.error(f"{len(errors)} row(s) failed validation:")
            for err in errors:
                st.write(f"- Row {err['row']}: {err['error']}")

        if records:
            st.success(f"{len(records)} language mapping(s) parsed.")

            if st.button("Save Language Mappings", type="primary"):
                try:
                    with db_session() as db:
                        for record in records:
                            existing = db.query(ShipLanguageORM).filter_by(ship_name=record.ship_name).first()
                            if existing:
                                existing.primary_language = record.primary_language
                            else:
                                db.add(ShipLanguageORM(**record.model_dump()))
                    st.success(f"Saved {len(records)} language mappings.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Save failed: {e}")
