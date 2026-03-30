"""LLM Schedule Advisor — Phase 5.

Wraps LLM calls with schedule context, conversation history,
action extraction, and action application.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import date, datetime

from src.demand.forecaster import DailyDemand
from src.llm.prompts import (
    SYSTEM_PROMPT,
    build_adjustment_prompt,
    build_explain_prompt,
    build_schedule_context,
    build_validation_prompt,
)
from src.llm_client import chat_completion
from src.models.employee import EmployeeRead
from src.models.enums import ScheduleStatus
from src.models.schedule import AssignmentRead, ScheduleRead
from src.models.shift_template import ShiftTemplateRead
from src.solver import validate_schedule

logger = logging.getLogger(__name__)

_VALID_ACTIONS = {"assign", "unassign", "day_off"}


class ScheduleAdvisor:
    """Manages LLM-assisted schedule discussions and adjustments.

    Keep one instance per schedule per session (stored in st.session_state).
    Re-instantiate when the schedule changes.
    """

    def __init__(
        self,
        schedule: ScheduleRead,
        employees: list[EmployeeRead],
        demand: list[DailyDemand],
        shift_templates: list[ShiftTemplateRead],
    ) -> None:
        self.schedule = schedule
        self.employees = employees
        self.demand = demand
        self.shift_templates = shift_templates
        self.conversation_history: list[dict] = []
        self._emp_by_name: dict[str, EmployeeRead] = {e.name: e for e in employees}
        self._shift_ids: set[str] = {s.id for s in shift_templates}

    # ── Context ───────────────────────────────────────────────────────────────

    def get_schedule_context(self) -> str:
        """Build compact schedule context string for LLM."""
        violations = []
        try:
            violations = validate_schedule(
                self.schedule, self.employees, self.demand, self.shift_templates
            )
        except Exception:
            pass
        return build_schedule_context(
            self.schedule, self.employees, self.demand, violations, self.shift_templates
        )

    # ── Main chat interface ───────────────────────────────────────────────────

    def chat(self, user_message: str) -> dict:
        """Process a user message and return response + optional actions.

        Returns:
            {
                "text": str,          # LLM explanation/response
                "actions": list[dict] # parsed action blocks (may be empty)
            }
        """
        context = self.get_schedule_context()
        messages = build_adjustment_prompt(user_message, context)

        # Splice conversation history between the system+context message and the
        # current user message so the LLM has continuity.
        if self.conversation_history:
            messages = messages[:-1] + self.conversation_history + [messages[-1]]

        logger.info("LLM chat: %s", user_message[:80])
        try:
            response = chat_completion(messages, temperature=0.3)
        except Exception as e:
            logger.error("LLM call failed: %s", e)
            return {"text": f"LLM unavailable: {e}", "actions": []}

        # Store in history (keep last 10 turns to avoid context bloat)
        self.conversation_history.append({"role": "user", "content": user_message})
        self.conversation_history.append({"role": "assistant", "content": response})
        if len(self.conversation_history) > 20:
            self.conversation_history = self.conversation_history[-20:]

        actions = self._extract_actions(response)
        return {"text": response, "actions": actions}

    def explain_schedule(
        self,
        target_date: date | None = None,
        employee_name: str | None = None,
    ) -> str:
        """Ask LLM to explain schedule decisions for a date or employee."""
        context = self.get_schedule_context()
        messages = build_explain_prompt(self.schedule, context, target_date, employee_name)
        try:
            return chat_completion(messages, temperature=0.2)
        except Exception as e:
            logger.error("LLM explain failed: %s", e)
            return f"LLM unavailable: {e}"

    def explain_violations(self, violations: list) -> str:
        """Ask LLM to explain constraint violations in plain language."""
        context = self.get_schedule_context()
        messages = build_validation_prompt(violations, context)
        try:
            return chat_completion(messages, temperature=0.2)
        except Exception as e:
            logger.error("LLM validation explain failed: %s", e)
            return f"LLM unavailable: {e}"

    def reset_history(self) -> None:
        """Clear conversation history (e.g. after regenerating the schedule)."""
        self.conversation_history = []

    # ── Action extraction ─────────────────────────────────────────────────────

    def _extract_actions(self, response: str) -> list[dict]:
        """Parse JSON action blocks from LLM response.

        The LLM may embed JSON in markdown code fences (```json ... ```)
        or inline. Handles both single objects and arrays.
        Gracefully ignores malformed JSON.
        """
        actions: list[dict] = []

        # 1. Try markdown code blocks first
        code_blocks = re.findall(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", response)
        candidates = list(code_blocks)

        # 2. If no code blocks, try bare JSON objects in the text
        if not candidates:
            candidates = re.findall(r"\{[^{}]*\"action\"\s*:[^{}]*\}", response)

        for raw in candidates:
            try:
                obj = json.loads(raw)
                if isinstance(obj, list):
                    for item in obj:
                        action = self._validate_action(item)
                        if action:
                            actions.append(action)
                elif isinstance(obj, dict):
                    action = self._validate_action(obj)
                    if action:
                        actions.append(action)
            except json.JSONDecodeError:
                continue

        return actions

    def _validate_action(self, obj: dict) -> dict | None:
        """Validate and normalise an action dict. Returns None if invalid."""
        action_type = obj.get("action", "").lower().strip()
        if action_type not in _VALID_ACTIONS:
            return None

        employee_name = obj.get("employee", "").strip()
        if not employee_name:
            return None

        date_str = obj.get("date", "")
        try:
            target_date = date.fromisoformat(str(date_str))
        except (ValueError, TypeError):
            return None

        shift = obj.get("shift")
        if shift is not None:
            shift = str(shift).strip()

        return {
            "action": action_type,
            "employee": employee_name,
            "date": target_date,
            "shift": shift,
            "reason": obj.get("reason", ""),
        }


# ── Action executor ───────────────────────────────────────────────────────────

def apply_action(
    action: dict,
    schedule: ScheduleRead,
    employees: list[EmployeeRead],
    demand: list[DailyDemand],
    shift_templates: list[ShiftTemplateRead],
) -> tuple[ScheduleRead, list[str]]:
    """Apply a single LLM-suggested action to the schedule.

    Returns:
        (updated_schedule, warnings: list[str])

    Actions:
    - "assign":   Set employee to the given shift on the given date.
    - "unassign": Remove employee's assignment (mark as day off).
    - "day_off":  Explicitly mark employee as day off on the given date.
    """
    warnings: list[str] = []

    # Resolve employee
    emp = next((e for e in employees if e.name == action["employee"]), None)
    if emp is None:
        warnings.append(f"Employee '{action['employee']}' not found.")
        return schedule, warnings

    target_date: date = action["date"]
    action_type: str = action["action"]
    shift_id: str | None = action.get("shift")

    # Validate availability
    if not (emp.availability_start <= target_date <= emp.availability_end):
        warnings.append(
            f"{emp.name} is not available on {target_date} "
            f"(availability: {emp.availability_start}–{emp.availability_end})."
        )
        return schedule, warnings

    # Validate shift for "assign"
    if action_type == "assign":
        if not shift_id:
            warnings.append("'assign' action requires a shift ID.")
            return schedule, warnings
        valid_shifts = {s.id for s in shift_templates}
        if shift_id not in valid_shifts:
            warnings.append(
                f"Invalid shift '{shift_id}' suggested — only predefined shifts are allowed. "
                f"Valid shift IDs: {', '.join(sorted(valid_shifts))}"
            )
            return schedule, warnings

    # Determine new shift_id and is_day_off
    if action_type == "assign":
        new_shift_id = shift_id
        new_is_day_off = False
    else:
        # unassign or day_off
        new_shift_id = "off"
        new_is_day_off = True

    # Rebuild assignments list, replacing or inserting the target record
    updated: list[AssignmentRead] = []
    replaced = False
    for a in schedule.assignments:
        if a.employee_id == emp.id and a.date == target_date:
            updated.append(AssignmentRead(
                id=a.id,
                schedule_id=a.schedule_id,
                employee_id=a.employee_id,
                date=a.date,
                shift_id=new_shift_id,
                is_day_off=new_is_day_off,
                notes=f"Applied via LLM: {action.get('reason', '')}",
            ))
            replaced = True
        else:
            updated.append(a)

    if not replaced:
        # Employee had no assignment for this date — insert one
        updated.append(AssignmentRead(
            id=uuid.uuid4(),
            schedule_id=schedule.id,
            employee_id=emp.id,
            date=target_date,
            shift_id=new_shift_id,
            is_day_off=new_is_day_off,
            notes=f"Applied via LLM: {action.get('reason', '')}",
        ))

    new_schedule = ScheduleRead(
        id=schedule.id,
        month=schedule.month,
        year=schedule.year,
        status=schedule.status,
        created_at=schedule.created_at,
        modified_at=datetime.utcnow(),
        assignments=sorted(updated, key=lambda a: (a.date, str(a.employee_id))),
    )

    # Post-apply validation: report any new errors
    try:
        new_violations = validate_schedule(new_schedule, employees, demand, shift_templates)
        errors = [v for v in new_violations if v.severity == "error"]
        if errors:
            for v in errors[:5]:
                date_str = f" ({v.date})" if v.date else ""
                warnings.append(f"⚠️ New violation: {v.employee}{date_str}: {v.message}")
    except Exception:
        pass

    return new_schedule, warnings
