"""Shared sidebar components — rendered on every page."""
from __future__ import annotations

import streamlit as st


def render_shift_legend() -> None:
    """Render a collapsible shift legend in the left sidebar.

    Loads shift templates from the database so it reflects any customisations
    made on the Settings page. Collapsed by default.
    """
    with st.sidebar.expander("📋 Shift Legend", expanded=False):
        try:
            from src.db.database import db_session
            from src.models.shift_template import ShiftTemplateORM

            with db_session() as db:
                shifts = db.query(ShiftTemplateORM).order_by(
                    ShiftTemplateORM.role, ShiftTemplateORM.id
                ).all()

            if not shifts:
                st.caption("No shift templates found.")
                return

            # Group by role
            cafe_shifts = [s for s in shifts if s.role == "cafe"]
            prod_shifts = [s for s in shifts if s.role == "production"]

            def _worked_h(s) -> float:
                duration_min = (
                    s.end_time.hour * 60 + s.end_time.minute
                    - s.start_time.hour * 60 - s.start_time.minute
                )
                return duration_min / 60.0 - 0.5

            for section_label, section_shifts in [
                ("☕ Café", cafe_shifts),
                ("🏭 Production", prod_shifts),
            ]:
                if not section_shifts:
                    continue
                st.markdown(f"**{section_label}**")
                rows = []
                for s in section_shifts:
                    start = str(s.start_time)[:5]
                    end = str(s.end_time)[:5]
                    wh = _worked_h(s)
                    rows.append(
                        f"`{s.id:>2}` {start}–{end} ({wh:.1f}h)"
                    )
                st.markdown("\n".join(rows))

        except Exception as exc:
            st.caption(f"Could not load shifts: {exc}")
