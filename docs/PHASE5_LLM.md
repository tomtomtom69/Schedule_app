# Phase 5: LLM Integration — Implementation Guide

**Goal:** Wire up the LLM advisory layer so the business owner can discuss, adjust, and understand schedules through natural language.

**Instructions for Claude Code:** Implement all files described below without requesting approval for each step. Only ask for approval before running system commands. CRITICAL: All OpenAI calls go through `src/llm_client.py` — never import `openai` in any other file. The model name comes from `.env` via `config.py`.

---

## 5.1 LLM Client (already created in Phase 1)

Verify `src/llm_client.py` exists and provides:
- `chat_completion(messages, temperature, max_tokens, response_format) -> str`
- `chat_completion_json(messages, **kwargs) -> str`
- Model comes from `settings.llm_model` (set in `.env`)

No changes needed if Phase 1 was implemented correctly.

---

## 5.2 Prompt Templates

### `src/llm/prompts.py`

All prompts live here. No prompt strings hardcoded elsewhere.

```python
SYSTEM_PROMPT = """You are a scheduling assistant for Geiranger Sjokolade, a chocolate shop and café in Geiranger, Norway. You help the business owner understand and adjust staff schedules.

Context:
- The business has two roles: café staff and production (chocolate manufacturing)
- Staff schedules are affected by cruise ship arrivals
- Norwegian labor law applies (max 9h normal shift, 10h absolute, 40h/week normal, 48h absolute, 11h daily rest, 35h weekly rest)
- Some employees live in Eidsdal and share 2 cars (5 seats each, need at least 1 driver per car)

When suggesting changes, always output a JSON action block that the system can parse:
{
  "action": "swap" | "assign" | "unassign" | "day_off",
  "employee": "name",
  "date": "YYYY-MM-DD",
  "shift": "shift_id or null",
  "reason": "explanation"
}

If the request doesn't map to a specific action, just explain in plain text.
"""

def build_schedule_context(schedule, employees, demand, violations) -> str:
    """
    Build a compact text representation of the current schedule state
    that fits within token limits.
    
    Format:
    - Date range and season
    - Employee list with roles and constraints
    - Day-by-day assignments (compact: "Aug 1: Aina=P2, Vanna=5, Aniol=5")
    - Current violations/warnings
    - Cruise ships this month
    """

def build_explain_prompt(schedule, date=None, employee=None) -> list[dict]:
    """Prompt to explain why the schedule looks the way it does."""

def build_adjustment_prompt(user_request, schedule_context) -> list[dict]:
    """Prompt to process a natural-language adjustment request."""

def build_validation_prompt(violations) -> list[dict]:
    """Prompt to explain constraint violations in plain language."""
```

---

## 5.3 Advisor Module

### `src/llm/advisor.py`

```python
import json
from src.llm_client import chat_completion, chat_completion_json
from src.llm.prompts import (
    SYSTEM_PROMPT,
    build_schedule_context,
    build_adjustment_prompt,
    build_explain_prompt,
    build_validation_prompt,
)

class ScheduleAdvisor:
    def __init__(self, schedule, employees, demand, shift_templates):
        self.schedule = schedule
        self.employees = employees
        self.demand = demand
        self.shift_templates = shift_templates
        self.conversation_history = []

    def get_schedule_context(self) -> str:
        """Build compact schedule context string."""
        violations = validate_schedule(self.schedule, ...)
        return build_schedule_context(
            self.schedule, self.employees, self.demand, violations
        )

    def chat(self, user_message: str) -> dict:
        """
        Process a user message and return response + optional actions.
        
        Returns:
            {
                "text": "LLM's explanation/response",
                "actions": [  # list of suggested changes, may be empty
                    {
                        "action": "swap",
                        "employee": "Aina",
                        "date": "2025-08-05",
                        "shift": "P2",
                        "reason": "..."
                    }
                ]
            }
        """
        context = self.get_schedule_context()
        
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Current schedule:\n{context}"},
        ]
        
        # Add conversation history
        for msg in self.conversation_history:
            messages.append(msg)
        
        messages.append({"role": "user", "content": user_message})
        
        response = chat_completion(messages)
        
        # Store in history
        self.conversation_history.append({"role": "user", "content": user_message})
        self.conversation_history.append({"role": "assistant", "content": response})
        
        # Parse actions from response
        actions = self._extract_actions(response)
        
        return {"text": response, "actions": actions}

    def explain_schedule(self, date=None, employee=None) -> str:
        """Ask LLM to explain schedule decisions."""
        messages = build_explain_prompt(self.schedule, date, employee)
        return chat_completion(messages)

    def explain_violations(self, violations) -> str:
        """Ask LLM to explain constraint violations in plain language."""
        messages = build_validation_prompt(violations)
        return chat_completion(messages)

    def _extract_actions(self, response: str) -> list[dict]:
        """
        Parse JSON action blocks from LLM response.
        LLM may include JSON in markdown code blocks.
        Gracefully handle missing or malformed JSON.
        """
        # Try to find JSON blocks in the response
        # Return empty list if none found
```

---

## 5.4 Action Executor

```python
# In src/llm/advisor.py or separate file

def apply_action(action: dict, schedule: Schedule, employees: list, shifts: list) -> tuple[Schedule, list[str]]:
    """
    Apply a single LLM-suggested action to the schedule.
    
    Returns: (updated_schedule, list_of_warnings)
    
    Actions:
    - "assign": Set employee to shift on date
    - "unassign": Remove employee from date
    - "swap": Swap two employees' shifts on a date
    - "day_off": Mark employee as day off on date
    
    After applying, re-validate the schedule and return any new violations.
    """
```

---

## 5.5 Chat Panel Integration

### Update `src/ui/components/chat_panel.py`

Replace the Phase 4 placeholder with real LLM integration:

```python
def render_chat_panel(schedule, employees, demand, shift_templates):
    st.sidebar.subheader("💬 Schedule Assistant")
    
    # Initialize advisor in session state
    if "advisor" not in st.session_state:
        st.session_state.advisor = ScheduleAdvisor(
            schedule, employees, demand, shift_templates
        )
    
    # Display message history
    for msg in st.session_state.get("chat_messages", []):
        with st.sidebar.chat_message(msg["role"]):
            st.write(msg["content"])
            if msg.get("actions"):
                for action in msg["actions"]:
                    col1, col2 = st.columns([3, 1])
                    col1.write(f"→ {action['action']}: {action['employee']} on {action['date']}")
                    if col2.button("Apply", key=f"apply_{action['date']}_{action['employee']}"):
                        apply_and_refresh(action)
    
    # Input
    user_input = st.sidebar.chat_input("Ask about the schedule...")
    if user_input:
        # Add user message
        st.session_state.chat_messages.append({"role": "user", "content": user_input})
        
        # Get LLM response
        result = st.session_state.advisor.chat(user_input)
        
        st.session_state.chat_messages.append({
            "role": "assistant",
            "content": result["text"],
            "actions": result["actions"],
        })
        
        st.rerun()
```

---

## 5.6 Example Interactions

The LLM should handle these types of requests:

1. **"Why is Aina working P5 on August 7?"**
   → Explains based on demand (cruise ship day, production needed) and her role capability

2. **"Can you swap Vanna and Aniol on August 16?"**
   → Checks constraints, proposes the swap with action block, warns if it creates violations

3. **"We need an extra person in the café on August 12, there's a big ship"**
   → Suggests which available employee to assign, which shift, with rationale

4. **"Show me who's working overtime this month"**
   → Summarizes weekly hours for each employee, flags anyone over 40h

5. **"Can Denis work mornings next week instead of evenings?"**
   → Proposes shift changes for the week, checks 11h rest constraint

---

## 5.7 Acceptance Criteria

Phase 5 is complete when:
- [ ] Chat panel sends messages to LLM and displays responses
- [ ] LLM receives current schedule context (compact format)
- [ ] LLM can explain why a specific assignment was made
- [ ] LLM can suggest swaps/changes with parseable action blocks
- [ ] "Apply" button on suggestions modifies the schedule in the grid
- [ ] After applying a change, violations are rechecked and displayed
- [ ] Conversation history is maintained within the session
- [ ] Changing LLM_MODEL in .env changes which model responds (no code changes)
- [ ] Chat works with gpt-4o-mini (verify) and can be switched to gpt-4o
