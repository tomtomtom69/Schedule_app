"""LLM chat panel component — Phase 5.

Fix summary (current version):
- JSON stripped from displayed text including bare arrays ([...]) not just objects.
- Actions are sanitised before storage and before rendering — None/empty/malformed entries
  are silently discarded so the chat never shows empty brackets or comma arrays.
- Apply buttons give immediate in-place feedback: the card turns green on success or red
  on failure without requiring the user to scroll anywhere.
- st.toast() notification on successful apply.
- Expand/compact toggle is clearly labelled; a prominent Return banner and a bottom
  "Done editing?" button appear in expanded mode.
"""
from __future__ import annotations

import re
from datetime import date, datetime

import streamlit as st


# ── Text helpers ──────────────────────────────────────────────────────────────

def _strip_json(text: str) -> str:
    """Remove all JSON from display text — both objects and arrays."""
    # ```json ... ``` or ``` {...} ``` fences
    text = re.sub(r"```(?:json)?\s*[\[{][\s\S]*?[\]}]\s*```", "", text, flags=re.IGNORECASE)
    # Bare JSON arrays containing action objects
    text = re.sub(r"\[\s*\{[^]]*\"action\"\s*:[^]]*\}\s*\]", "", text, flags=re.DOTALL)
    # Bare JSON objects with an "action" key
    text = re.sub(r"\{[^{}]*\"action\"\s*:[^{}]*\}", "", text)
    # Empty array artefacts left behind: [], [,], [,,] etc.
    text = re.sub(r"\[\s*,*\s*\]", "", text)
    # Collapse excess blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _sanitise_actions(actions: list) -> list[dict]:
    """Return only valid, complete action dicts — discard None, empty, or malformed."""
    if not actions:
        return []
    result = []
    for a in actions:
        if not a or not isinstance(a, dict):
            continue
        # Must have all required fields with non-empty values
        if not a.get("action") or not a.get("employee") or not a.get("date"):
            continue
        if a["action"] not in ("assign", "unassign", "day_off"):
            continue
        result.append(a)
    return result


def _action_description(action: dict) -> str:
    """Return a human-readable one-liner for an action dict."""
    d = action["date"]
    date_str = d.strftime("%B %d") if hasattr(d, "strftime") else str(d)
    emp = action["employee"]
    act = action["action"]
    shift = action.get("shift") or ""
    reason = action.get("reason") or ""

    if act == "assign":
        desc = f"Assign **{emp}** to shift **{shift}** on {date_str}"
    elif act == "unassign":
        desc = f"Remove **{emp}**'s shift on {date_str}"
    elif act == "day_off":
        desc = f"Give **{emp}** a day off on {date_str}"
    else:
        desc = f"{act.upper()} **{emp}** on {date_str}"

    if reason:
        desc += f" — {reason}"
    return desc


# ── Schedule helpers ──────────────────────────────────────────────────────────

def _compute_total_hours(schedule, shift_templates) -> float:
    if not shift_templates:
        return 0.0
    shift_hours: dict[str, float] = {}
    for s in shift_templates:
        start_m = s.start_time.hour * 60 + s.start_time.minute
        end_m = s.end_time.hour * 60 + s.end_time.minute
        shift_hours[s.id] = (end_m - start_m) / 60.0
    return sum(
        shift_hours.get(a.shift_id, 0.0)
        for a in schedule.assignments
        if not a.is_day_off
    )


def _save_schedule_to_db(schedule) -> None:
    try:
        from src.db.database import db_session
        from src.models.schedule import AssignmentORM, ScheduleORM

        with db_session() as db:
            existing = db.query(ScheduleORM).filter_by(
                year=schedule.year, month=schedule.month
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
    except Exception as e:
        st.warning(f"Auto-save after LLM edit failed: {e}")


# ── Main render function ──────────────────────────────────────────────────────

def render_chat_panel(
    schedule=None,
    employees: list | None = None,
    demand: list | None = None,
    shift_templates: list | None = None,
) -> None:
    """Render the schedule assistant chat panel.

    The expand/compact toggle is stored in st.session_state["chat_expanded"].
    The calling page must read that flag and adjust its column ratio accordingly,
    and render a full-width return banner above the columns when expanded.
    """
    expanded = st.session_state.get("chat_expanded", False)

    # ── Header row: toggle + title ─────────────────────────────────────────────
    _tcol, _hcol = st.columns([1, 3])
    with _tcol:
        toggle_label = "📅 Schedule" if expanded else "🔍 Expand Chat for Editing"
        if st.button(toggle_label, use_container_width=True, key="chat_expand_toggle"):
            st.session_state["chat_expanded"] = not expanded
            st.rerun()
    with _hcol:
        st.markdown("### 💬 Schedule Assistant")
    st.caption("Ask questions or request schedule adjustments")

    if schedule is None:
        st.info("Generate or load a schedule to enable the assistant.")
        return

    if employees is None or demand is None or shift_templates is None:
        st.info("Loading schedule data…")
        return

    # ── Condensed schedule view (expanded mode only) ──────────────────────────
    if expanded:
        _render_condensed_schedule(schedule, employees, shift_templates)
        st.divider()

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

    # ── Prompt to expand when latest response has proposals ───────────────────
    messages = st.session_state.get("chat_messages", [])
    _latest_actions: list[dict] = []
    if messages:
        _last_asst = next((m for m in reversed(messages) if m["role"] == "assistant"), None)
        if _last_asst:
            _latest_actions = _sanitise_actions(_last_asst.get("actions", []))
    if _latest_actions and not expanded:
        st.info("💡 Click **🔍 Expand Chat for Editing** for a better view of proposed changes.")

    # ── Message history ───────────────────────────────────────────────────────
    chat_height = 420 if expanded else 300
    msg_container = st.container(height=chat_height)

    with msg_container:
        if not messages:
            st.markdown(
                "<div style='color:#999;font-size:12px;text-align:center;margin-top:60px;'>"
                "Ask me anything about the schedule.<br>"
                "E.g. «Why is Eva on P2 on August 7?»<br>"
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

            # Sanitise before rendering — never render empty/malformed action lists
            valid_actions = _sanitise_actions(msg.get("actions", []))
            if valid_actions:
                _render_action_cards(i, valid_actions, schedule, employees, demand, shift_templates)

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
        st.session_state.pop("applied_actions", None)
        st.session_state.pop("failed_actions", None)
        st.session_state.advisor.reset_history()
        st.rerun()

    if submitted and user_input.strip():
        _run_chat(user_input.strip(), schedule, employees, demand, shift_templates)

    # ── Bottom "return to schedule" button (expanded mode only) ──────────────
    if expanded:
        st.divider()
        if st.button(
            "Done editing? → Return to Schedule View",
            use_container_width=True,
            key="chat_return_bottom",
        ):
            st.session_state["chat_expanded"] = False
            st.rerun()


# ── Condensed schedule mini-view (expanded mode) ──────────────────────────────

def _render_condensed_schedule(schedule, employees, shift_templates) -> None:
    import pandas as pd

    messages = st.session_state.get("chat_messages", [])
    action_dates: set[date] = set()
    if messages:
        last_asst = next((m for m in reversed(messages) if m["role"] == "assistant"), None)
        if last_asst:
            for act in _sanitise_actions(last_asst.get("actions", [])):
                d = act.get("date")
                if d and hasattr(d, "strftime"):
                    action_dates.add(d)

    all_dates = sorted({a.date for a in schedule.assignments})
    if action_dates:
        relevant = [
            d for d in all_dates
            if any(abs((d - ad).days) <= 3 for ad in action_dates)
        ][:14]
    else:
        relevant = all_dates[:7]

    if not relevant:
        return

    st.caption("📅 Relevant schedule days:")
    assign_map = {(a.employee_id, a.date): a for a in schedule.assignments}

    rows = []
    for emp in sorted(employees, key=lambda e: e.name):
        row: dict = {"Employee": emp.name}
        for d in relevant:
            col_key = d.strftime("%d %a")
            a = assign_map.get((emp.id, d))
            if a:
                row[col_key] = "off" if a.is_day_off else a.shift_id
            elif emp.availability_start <= d <= emp.availability_end:
                row[col_key] = ""
            else:
                row[col_key] = "—"
        rows.append(row)

    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True, height=200)


# ── Action cards ──────────────────────────────────────────────────────────────

def _render_action_cards(
    msg_idx: int,
    actions: list[dict],
    schedule,
    employees,
    demand,
    shift_templates,
) -> None:
    """Render proposal cards with per-card apply tracking and in-place feedback."""
    applied = st.session_state.setdefault("applied_actions", set())
    failed = st.session_state.setdefault("failed_actions", {})

    # Only show the banner if at least one action is still pending
    pending = [j for j in range(len(actions)) if (msg_idx, j) not in applied and (msg_idx, j) not in failed]
    if pending:
        st.markdown(
            "<div style='background:#FFF9C4;border-left:4px solid #F9A825;border-radius:4px;"
            "padding:6px 10px;margin:4px 0;font-size:12px;'>"
            "<b>📋 Proposed changes — review and apply individually or all at once:</b></div>",
            unsafe_allow_html=True,
        )

    for j, action in enumerate(actions):
        desc = _action_description(action)
        card_key = (msg_idx, j)

        if card_key in applied:
            # In-place success feedback
            st.markdown(
                f"<div style='background:#D4EDDA;border-left:4px solid #28A745;border-radius:6px;"
                f"padding:6px 10px;margin:2px 0;font-size:12px;'>✅ Applied: {desc}</div>",
                unsafe_allow_html=True,
            )
        elif card_key in failed:
            # In-place error feedback
            err = failed[card_key]
            st.markdown(
                f"<div style='background:#F8D7DA;border-left:4px solid #DC3545;border-radius:6px;"
                f"padding:6px 10px;margin:2px 0;font-size:12px;'>❌ Failed: {err}</div>",
                unsafe_allow_html=True,
            )
        else:
            # Pending card with Apply button
            card_col, btn_col = st.columns([5, 1])
            with card_col:
                st.markdown(
                    f"<div style='background:#F5F5F5;border-radius:6px;padding:6px 10px;"
                    f"margin:2px 0;font-size:12px;'>→ {desc}</div>",
                    unsafe_allow_html=True,
                )
            with btn_col:
                if st.button("Apply", key=f"apply_{msg_idx}_{j}", type="primary", use_container_width=True):
                    applied.add(card_key)
                    try:
                        _do_apply(
                            [action], schedule, employees, demand, shift_templates,
                            label=f"Applied: {desc}",
                        )
                    except Exception as e:
                        applied.discard(card_key)
                        failed[card_key] = str(e)
                        st.rerun()

    # Apply All button — only if 2+ actions still pending
    if len(pending) > 1:
        if st.button(
            f"✅ Apply All {len(pending)} Remaining Changes",
            key=f"apply_all_{msg_idx}",
            use_container_width=True,
        ):
            for j in pending:
                applied.add((msg_idx, j))
            try:
                _do_apply(
                    [actions[j] for j in pending],
                    schedule, employees, demand, shift_templates,
                    label=f"Applied {len(pending)} changes",
                )
            except Exception as e:
                for j in pending:
                    applied.discard((msg_idx, j))
                    failed[(msg_idx, j)] = str(e)
                st.rerun()


# ── Apply logic ───────────────────────────────────────────────────────────────

def _do_apply(
    actions: list[dict],
    schedule,
    employees,
    demand,
    shift_templates,
    label: str = "Applied changes",
) -> None:
    """Apply one or more actions, save to DB, reset to draft, show diff + toast."""
    from src.llm.advisor import apply_action
    from src.models.enums import ScheduleStatus
    from src.models.schedule import ScheduleRead

    hours_before = _compute_total_hours(schedule, shift_templates)
    current = schedule
    all_warnings: list[str] = []

    for action in actions:
        new_sched, warns = apply_action(action, current, employees, demand, shift_templates)
        new_sched = ScheduleRead(
            id=new_sched.id,
            month=new_sched.month,
            year=new_sched.year,
            status=ScheduleStatus.draft,
            created_at=new_sched.created_at,
            modified_at=datetime.utcnow(),
            assignments=new_sched.assignments,
        )
        current = new_sched
        all_warnings.extend(warns)

    hours_after = _compute_total_hours(current, shift_templates)
    _save_schedule_to_db(current)

    st.session_state["current_schedule"] = current
    st.session_state["editor_schedule"] = current
    if "advisor" in st.session_state:
        st.session_state.advisor.schedule = current

    diff = hours_after - hours_before
    diff_str = (
        f"  \nTotal hours: **{hours_before:.0f}h → {hours_after:.0f}h** (Δ{diff:+.0f}h)"
        if abs(diff) > 0.05
        else ""
    )
    conf = f"✅ {label}.{diff_str}  \n_Schedule set back to draft — re-approve when ready._"
    if all_warnings:
        conf += "\n\n⚠️ " + "  \n⚠️ ".join(all_warnings)

    st.session_state.chat_messages.append({"role": "assistant", "content": conf, "actions": []})

    # Flash banner for the calling page's schedule grid
    st.session_state["_apply_flash"] = label

    # Toast notification — gives immediate top-of-page feedback
    try:
        st.toast("Change applied — schedule updated ✅", icon="✅")
    except Exception:
        pass  # st.toast not available in all Streamlit versions

    st.rerun()


# ── Chat / explain helpers ────────────────────────────────────────────────────

def _run_chat(user_input: str, schedule, employees, demand, shift_templates) -> None:
    if "chat_messages" not in st.session_state:
        st.session_state.chat_messages = []

    st.session_state.chat_messages.append({"role": "user", "content": user_input})

    with st.spinner("Thinking…"):
        result = st.session_state.advisor.chat(user_input)

    clean_text = _strip_json(result["text"])
    # Sanitise actions before storing — discard any malformed/empty entries
    clean_actions = _sanitise_actions(result.get("actions", []))

    st.session_state.chat_messages.append({
        "role": "assistant",
        "content": clean_text,
        "actions": clean_actions,
    })
    st.rerun()


def _run_explain(schedule) -> None:
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
        "content": _strip_json(text),
        "actions": [],
    })
    st.rerun()


def _run_explain_violations(schedule, employees, demand, shift_templates) -> None:
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
        "content": _strip_json(text),
        "actions": [],
    })
    st.rerun()
