"""LLM chat panel component — Phase 5.

Renders the schedule assistant chat interface with real LLM integration.
Uses ScheduleAdvisor from src/llm/advisor.py.
"""
from __future__ import annotations

import streamlit as st


def render_chat_panel(
    schedule=None,
    employees: list | None = None,
    demand: list | None = None,
    shift_templates: list | None = None,
) -> None:
    """Render the schedule assistant chat panel.

    Initialises a ScheduleAdvisor in session state (resets if schedule changes).
    Shows conversation history with Apply buttons next to action suggestions.
    """
    st.markdown("### 💬 Schedule Assistant")
    st.caption("Ask questions or request schedule adjustments")

    if schedule is None:
        st.info("Generate or load a schedule to enable the assistant.")
        return

    if employees is None or demand is None or shift_templates is None:
        st.info("Loading schedule data…")
        return

    # ── Initialise or reset advisor ───────────────────────────────────────────
    current_id = str(schedule.id)
    if (
        "advisor" not in st.session_state
        or st.session_state.get("advisor_schedule_id") != current_id
    ):
        from src.llm.advisor import ScheduleAdvisor
        st.session_state.advisor = ScheduleAdvisor(schedule, employees, demand, shift_templates)
        st.session_state.advisor_schedule_id = current_id
        st.session_state.chat_messages = []
    else:
        # Keep advisor in sync with latest schedule state (edits via grid)
        st.session_state.advisor.schedule = schedule

    # ── Quick-action buttons ──────────────────────────────────────────────────
    with st.expander("Quick actions"):
        col1, col2 = st.columns(2)
        with col1:
            if st.button("📋 Explain month", use_container_width=True):
                _run_explain(schedule=schedule)
        with col2:
            if st.button("⚠️ Explain violations", use_container_width=True):
                _run_explain_violations(schedule, employees, demand, shift_templates)

    # ── Message history ───────────────────────────────────────────────────────
    messages = st.session_state.get("chat_messages", [])
    msg_container = st.container(height=320)

    with msg_container:
        if not messages:
            st.markdown(
                "<div style='color:#999;font-size:12px;text-align:center;margin-top:60px;'>"
                "Ask me anything about the schedule.<br>"
                "E.g. «Why is Eva working P2 on August 7?»<br>"
                "or «Give Alice a day off on August 15»</div>",
                unsafe_allow_html=True,
            )

        for i, msg in enumerate(messages):
            role_icon = "🧑" if msg["role"] == "user" else "🤖"
            bg = "#E8F4FF" if msg["role"] == "assistant" else "#F0F0F0"
            st.markdown(
                f"<div style='background:{bg};border-radius:8px;padding:8px 12px;"
                f"margin:4px 0;font-size:13px;'>"
                f"<b>{role_icon}</b> {msg['content']}</div>",
                unsafe_allow_html=True,
            )

            # Show Apply buttons for action suggestions
            actions = msg.get("actions", [])
            if actions:
                st.caption("Suggested changes:")
                for j, action in enumerate(actions):
                    date_str = action["date"].strftime("%b %d") if hasattr(action["date"], "strftime") else str(action["date"])
                    description = (
                        f"{action['action'].upper()} {action['employee']} on {date_str}"
                        + (f" → {action['shift']}" if action.get("shift") else "")
                    )
                    reason = action.get("reason", "")
                    key = f"apply_{i}_{j}_{action['employee']}_{date_str}"
                    apply_col, desc_col = st.columns([1, 4])
                    if apply_col.button("Apply", key=key, type="primary"):
                        _apply_action(action, schedule, employees, demand, shift_templates)
                    desc_col.caption(f"{description} — {reason}" if reason else description)

    # ── Input form ────────────────────────────────────────────────────────────
    with st.form("chat_form", clear_on_submit=True):
        user_input = st.text_input(
            "Message",
            placeholder="Ask about or adjust the schedule…",
            label_visibility="collapsed",
        )
        col_send, col_clear = st.columns([3, 1])
        submitted = col_send.form_submit_button("Send", use_container_width=True, type="primary")
        cleared = col_clear.form_submit_button("Clear", use_container_width=True)

    if cleared:
        st.session_state.chat_messages = []
        st.session_state.advisor.reset_history()
        st.rerun()

    if submitted and user_input.strip():
        _run_chat(user_input.strip(), schedule, employees, demand, shift_templates)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run_chat(
    user_input: str,
    schedule,
    employees,
    demand,
    shift_templates,
) -> None:
    """Send user message to advisor and store response."""
    if "chat_messages" not in st.session_state:
        st.session_state.chat_messages = []

    st.session_state.chat_messages.append({"role": "user", "content": user_input})

    with st.spinner("Thinking…"):
        result = st.session_state.advisor.chat(user_input)

    st.session_state.chat_messages.append({
        "role": "assistant",
        "content": result["text"],
        "actions": result.get("actions", []),
    })
    st.rerun()


def _run_explain(schedule) -> None:
    """Ask advisor to explain the full month."""
    if "chat_messages" not in st.session_state:
        st.session_state.chat_messages = []

    st.session_state.chat_messages.append({
        "role": "user",
        "content": "Give me an overview of the key scheduling decisions this month.",
    })
    with st.spinner("Explaining…"):
        text = st.session_state.advisor.explain_schedule()
    st.session_state.chat_messages.append({
        "role": "assistant",
        "content": text,
        "actions": [],
    })
    st.rerun()


def _run_explain_violations(schedule, employees, demand, shift_templates) -> None:
    """Ask advisor to explain current violations."""
    if "chat_messages" not in st.session_state:
        st.session_state.chat_messages = []

    from src.solver import validate_schedule
    try:
        violations = validate_schedule(schedule, employees, demand, shift_templates)
    except Exception:
        violations = []

    st.session_state.chat_messages.append({
        "role": "user",
        "content": "Please explain the current constraint violations.",
    })
    with st.spinner("Analysing violations…"):
        text = st.session_state.advisor.explain_violations(violations)
    st.session_state.chat_messages.append({
        "role": "assistant",
        "content": text,
        "actions": [],
    })
    st.rerun()


def _apply_action(action: dict, schedule, employees, demand, shift_templates) -> None:
    """Apply a single LLM action to the schedule and update session state."""
    from src.llm.advisor import apply_action

    new_schedule, warnings = apply_action(action, schedule, employees, demand, shift_templates)

    # Update session state
    st.session_state["current_schedule"] = new_schedule
    st.session_state.advisor.schedule = new_schedule

    date_str = action["date"].strftime("%b %d") if hasattr(action["date"], "strftime") else str(action["date"])
    msg = (
        f"✅ Applied: {action['action'].upper()} {action['employee']} on {date_str}"
        + (f" → {action['shift']}" if action.get("shift") else "")
    )
    if warnings:
        msg += "\n" + "\n".join(warnings)
    st.session_state.chat_messages.append({"role": "assistant", "content": msg, "actions": []})
    st.rerun()
