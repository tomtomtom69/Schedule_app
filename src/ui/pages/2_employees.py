"""Employees page — upload CSV, view, edit, and manage employees."""
from __future__ import annotations

from datetime import date

import streamlit as st

from src.db.database import db_session
from src.ingestion.csv_parser import parse_employees_csv
from src.ingestion.validators import validate_employee_list
from src.models.cruise_ship import CruiseShipORM
from src.models.employee import EmployeeORM, EmployeeRead

st.set_page_config(page_title="Employees", page_icon="👥", layout="wide")
from src.ui.components.sidebar import render_shift_legend
render_shift_legend()
st.title("👥 Employees")

# ── Unsaved data warning ──────────────────────────────────────────────────────
_unsaved = st.session_state.get("employees_unsaved", 0)
if _unsaved:
    st.warning(
        f"⚠️ **{_unsaved} employee(s) parsed but NOT yet saved.** "
        "Go to the **Upload CSV** tab and click **Save to Database** to keep your data.",
        icon="⚠️",
    )
    st.components.v1.html(
        "<script>"
        "window.parent.onbeforeunload = function() {"
        "  return 'You have unsaved employee data. Are you sure you want to leave?';"
        "};"
        "</script>",
        height=0,
    )

tab_view, tab_edit, tab_upload = st.tabs(["Employee List", "Edit Employee", "Upload CSV"])


# ── Tab 1: Employee List + Quick Stats ───────────────────────────────────────
with tab_view:
    try:
        with db_session() as db:
            rows = db.query(EmployeeORM).order_by(EmployeeORM.name).all()
            total = len(rows)
            full_time = sum(1 for e in rows if e.employment_type == "full_time")
            part_time = total - full_time
            eidsdal_count = sum(1 for e in rows if e.housing == "eidsdal")
            driver_count = sum(1 for e in rows if e.driving_licence)
            eidsdal_driver_count = sum(
                1 for e in rows if e.housing == "eidsdal" and e.driving_licence
            )
            lang_counts: dict[str, int] = {}
            for e in rows:
                for lang in (e.languages if isinstance(e.languages, list) else []):
                    lang_counts[lang] = lang_counts.get(lang, 0) + 1
            data = [
                {
                    "Name": ("🏔 " if e.housing == "eidsdal" else "")
                            + ("🚗 " if e.driving_licence else "")
                            + e.name,
                    "Role": e.role_capability.value if hasattr(e.role_capability, 'value') else e.role_capability,
                    "Type": e.employment_type.value if hasattr(e.employment_type, 'value') else e.employment_type,
                    "Hours/wk": e.contracted_hours,
                    "DOB": str(e.date_of_birth) if getattr(e, "date_of_birth", None) else "—",
                    "Housing": e.housing,
                    "Languages": ", ".join(e.languages if isinstance(e.languages, list) else []),
                    "Available": f"{e.availability_start} – {e.availability_end}",
                }
                for e in rows
            ]

        if data:
            # Quick Stats Panel
            st.subheader("Quick Stats")
            col1, col2, col3, col4, col5 = st.columns(5)
            col1.metric("Total Employees", total)
            col2.metric("Full-Time", full_time)
            col3.metric("Part-Time", part_time)
            col4.metric("Eidsdal", eidsdal_count)
            col5.metric("Drivers", driver_count)

            # Language coverage
            with st.expander("Language Coverage"):
                for lang, count in sorted(lang_counts.items()):
                    icon = "✅" if count >= 2 else "⚠️"
                    st.write(f"{icon} **{lang.capitalize()}**: {count} employee(s)")

            # Eidsdal warnings
            if eidsdal_count > 0:
                if eidsdal_driver_count == 0:
                    st.error("No licensed drivers among Eidsdal employees — solver will be infeasible!")
                elif eidsdal_driver_count == 1 and eidsdal_count > 5:
                    st.warning(
                        "Only 1 Eidsdal driver. If more than 5 Eidsdal workers are scheduled "
                        "on the same day, a 2nd driver is required."
                    )
                else:
                    st.success(f"Eidsdal transport OK: {eidsdal_driver_count} driver(s) for {eidsdal_count} Eidsdal workers.")

            st.divider()
            st.subheader("All Employees")
            st.dataframe(data, use_container_width=True, hide_index=True)
            st.caption(f"🏔 = Eidsdal  🚗 = Driver  |  Total: {total} employees")
        else:
            st.info("No employees in the database. Upload a CSV in the 'Upload CSV' tab.")

    except Exception as e:
        st.error(f"Could not load employees: {e}")


# ── Tab 2: Edit Employee ──────────────────────────────────────────────────────
with tab_edit:
    st.subheader("Edit or Delete Employee")

    try:
        with db_session() as db:
            all_employees = db.query(EmployeeORM).order_by(EmployeeORM.name).all()
            emp_names = [e.name for e in all_employees]

        if not emp_names:
            st.info("No employees in database. Upload a CSV first.")
        else:
            selected_name = st.selectbox("Select employee", emp_names, key="edit_select")

            with db_session() as db:
                row = db.query(EmployeeORM).filter_by(name=selected_name).first()
                emp = {
                    "name": row.name,
                    "role_capability": row.role_capability.value if hasattr(row.role_capability, 'value') else row.role_capability,
                    "employment_type": row.employment_type.value if hasattr(row.employment_type, 'value') else row.employment_type,
                    "contracted_hours": row.contracted_hours,
                    "housing": row.housing,
                    "driving_licence": row.driving_licence,
                    "languages": row.languages if isinstance(row.languages, list) else [],
                    "availability_start": row.availability_start,
                    "availability_end": row.availability_end,
                    "date_of_birth": getattr(row, "date_of_birth", None),
                } if row else None

            if emp:
                with st.form("edit_employee_form"):
                    st.markdown(f"**Editing: {emp['name']}**")

                    col1, col2 = st.columns(2)

                    with col1:
                        new_name = st.text_input("Name", value=emp["name"])
                        new_role = st.selectbox(
                            "Role Capability",
                            options=["cafe", "production", "both"],
                            index=["cafe", "production", "both"].index(emp["role_capability"]),
                        )
                        new_emp_type = st.selectbox(
                            "Employment Type",
                            options=["full_time", "part_time"],
                            index=["full_time", "part_time"].index(emp["employment_type"]),
                        )
                        new_hours = st.number_input(
                            "Contracted Hours/week",
                            min_value=1.0,
                            max_value=40.0,
                            value=float(emp["contracted_hours"]),
                            step=0.5,
                        )

                    with col2:
                        new_housing = st.selectbox(
                            "Housing",
                            options=["geiranger", "eidsdal"],
                            index=["geiranger", "eidsdal"].index(emp["housing"]),
                        )
                        new_licence = st.checkbox("Driving Licence", value=emp["driving_licence"])
                        new_langs_str = st.text_input(
                            "Languages (comma-separated)",
                            value=", ".join(emp["languages"]),
                        )
                        new_avail_start = st.date_input(
                            "Availability Start",
                            value=emp["availability_start"],
                            min_value=date(2026, 5, 1),
                            max_value=date(2026, 10, 15),
                        )
                        new_avail_end = st.date_input(
                            "Availability End",
                            value=emp["availability_end"],
                            min_value=date(2026, 5, 1),
                            max_value=date(2026, 10, 15),
                        )
                        new_dob = st.date_input(
                            "Date of Birth (optional — used for age-based constraints)",
                            value=emp.get("date_of_birth"),
                            min_value=date(1950, 1, 1),
                            max_value=date(2015, 12, 31),
                            format="YYYY-MM-DD",
                            help="Required for employees under 18. Leave blank for adults.",
                        )

                    col_save, col_delete = st.columns([3, 1])
                    save_clicked = col_save.form_submit_button("Save Changes", type="primary")
                    delete_clicked = col_delete.form_submit_button("Delete Employee", type="secondary")

                if save_clicked:
                    try:
                        langs = [l.strip().lower() for l in new_langs_str.split(",") if l.strip()]
                        if "english" not in langs:
                            langs = ["english"] + langs
                        with db_session() as db:
                            orm = db.query(EmployeeORM).filter_by(name=selected_name).first()
                            if orm:
                                orm.name = new_name
                                orm.role_capability = new_role
                                orm.employment_type = new_emp_type
                                orm.contracted_hours = new_hours
                                orm.housing = new_housing
                                orm.driving_licence = new_licence
                                orm.languages = langs
                                orm.availability_start = new_avail_start
                                orm.availability_end = new_avail_end
                                orm.date_of_birth = new_dob
                        st.success(f"Updated {new_name}.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Save failed: {e}")

                if delete_clicked:
                    if st.session_state.get("confirm_delete") != selected_name:
                        st.session_state["confirm_delete"] = selected_name
                        st.warning(f"Click Delete again to confirm deletion of {selected_name}.")
                    else:
                        try:
                            with db_session() as db:
                                orm = db.query(EmployeeORM).filter_by(name=selected_name).first()
                                if orm:
                                    db.delete(orm)
                            st.session_state.pop("confirm_delete", None)
                            st.success(f"Deleted {selected_name}.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Delete failed: {e}")

    except Exception as e:
        st.error(f"Error loading employee editor: {e}")


# ── Tab 3: Upload CSV ─────────────────────────────────────────────────────────
with tab_upload:
    st.subheader("Upload Employee CSV or Excel")
    st.markdown(
        """
        **Required columns:**
        `name, languages, role_capability, employment_type, contracted_hours, housing, driving_licence, availability_start, availability_end`

        **Optional columns:** `date_of_birth` or `age` — required for employees under 18.

        **Tolerant input — all of these are accepted:**
        | Field | Accepted values |
        |---|---|
        | `role_capability` | `cafe` / `Cafe` / `café` / `Caf`, `production` / `Production` / `Manager Production`, `both` |
        | `employment_type` | `full_time` / `full-time` / `Full-Time`, `part_time` / `part-time` / `Part-time` |
        | `driving_licence` | `true`/`false`, `yes`/`no`, `1`/`0`, `TRUE`/`FALSE` |
        | `housing` | `Geiranger` / `geiranger`, `Eidsdal` / `eidsdal` |
        | `languages` | Comma **or** semicolon separated; any case; `english` auto-added if missing |
        | `date_of_birth` | `YYYY-MM-DD`, `DD-MM-YYYY`, `DD.MM.YYYY`, `1 dec 2010`, `01 des 2010`, etc. |
        | `age` | Integer age — converted to approximate birth year (Jan 1) |

        Blank rows are skipped silently. Auto-corrections are shown after upload.

        **Example:**
        ```
        name,languages,role_capability,employment_type,contracted_hours,housing,driving_licence,availability_start,availability_end,date_of_birth
        Aina,"english;spanish",Both,full-time,37.5,Eidsdal,yes,2026-05-01,2026-10-15,
        Lars,English,Café,part_time,20.0,geiranger,1,2026-06-01,2026-08-31,15-03-2010
        ```
        """
    )

    uploaded = st.file_uploader("Choose file", type=["csv", "xlsx"])

    if uploaded:
        # If this exact file was already saved this session, don't re-parse / re-warn
        if st.session_state.get("_employees_saved_file") == uploaded.name:
            _saved_count = st.session_state.get("_employees_save_count", 0)
            st.success(
                f"✅ **{_saved_count} employee(s)** from this file are already saved to the database.  \n"
                "Remove the file above and upload a different one to add more employees."
            )
        else:
            # New file — clear any stale saved marker
            st.session_state.pop("_employees_saved_file", None)
            records, errors, corrections = parse_employees_csv(uploaded)

            if errors:
                st.error(f"{len(errors)} row(s) failed validation:")
                for err in errors:
                    st.write(f"- Row {err['row']}: {err['error']}")

            if records:
                st.session_state["employees_unsaved"] = len(records)
                st.success(f"{len(records)} employee(s) parsed successfully.")

                if corrections:
                    with st.expander(f"ℹ️ {len(corrections)} auto-correction(s) applied — click to review"):
                        for note in corrections:
                            st.caption(note)

                # Year mismatch check against existing ship data
                try:
                    with db_session() as db:
                        ship_years = {r.date.year for r in db.query(CruiseShipORM).all()}
                    if ship_years:
                        emp_years = {r.availability_start.year for r in records} | {r.availability_end.year for r in records}
                        if not emp_years & ship_years:
                            st.warning(
                                f"⚠️ **Year mismatch:** Employee availability covers {sorted(emp_years)} "
                                f"but cruise ship data is for {sorted(ship_years)}. "
                                "The solver will have zero available employees — update availability dates or re-upload ships."
                            )
                except Exception:
                    pass

                warnings = validate_employee_list(records)
                for w in warnings:
                    st.warning(w)

                import pandas as pd
                preview_data = [
                    {
                        "Name": r.name,
                        "Role": r.role_capability.value if hasattr(r.role_capability, 'value') else r.role_capability,
                        "Type": r.employment_type.value if hasattr(r.employment_type, 'value') else r.employment_type,
                        "Hours": r.contracted_hours,
                        "DOB": str(r.date_of_birth) if r.date_of_birth else "—",
                        "Housing": r.housing,
                        "Driver": r.driving_licence,
                        "Languages": ", ".join(r.languages),
                    }
                    for r in records
                ]
                st.dataframe(preview_data, use_container_width=True, hide_index=True)

                import_mode = st.radio(
                    "Import mode",
                    ["Append (add new, update existing by name)", "Replace all (delete existing first)"],
                    horizontal=True,
                )

                st.divider()
                with st.container(border=True):
                    st.markdown(
                        f"### 💾 Save {len(records)} Employee(s) to Database",
                        help="This will write the previewed employees to the database.",
                    )
                    st.caption(
                        "Mode: **Replace all**" if "Replace" in import_mode
                        else "Mode: **Append / update by name**"
                    )
                    if st.button(
                        f"✅ Save {len(records)} Employee(s) to Database",
                        type="primary",
                        use_container_width=True,
                        key="save_employees_btn",
                    ):
                        try:
                            with db_session() as db:
                                if "Replace" in import_mode:
                                    db.query(EmployeeORM).delete()
                                for record in records:
                                    existing = db.query(EmployeeORM).filter_by(name=record.name).first()
                                    if existing:
                                        for field, value in record.model_dump().items():
                                            setattr(existing, field, value)
                                    else:
                                        db.add(EmployeeORM(**record.model_dump()))
                            st.session_state["_employees_saved_file"] = uploaded.name
                            st.session_state["_employees_save_count"] = len(records)
                            st.session_state.pop("employees_unsaved", None)
                            st.components.v1.html(
                                "<script>window.parent.onbeforeunload = null;</script>",
                                height=0,
                            )
                            st.success(
                                f"✅ **{len(records)} employee(s) saved successfully!**  \n"
                                "The Employee List tab now shows the updated data."
                            )
                            st.balloons()
                            st.rerun()
                        except Exception as e:
                            st.error(f"Save failed: {e}")
