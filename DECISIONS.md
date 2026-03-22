# Technical Decisions, Bug Fixes & Key Patterns

This file documents the "why" behind major decisions and every significant bug that was
diagnosed and fixed, so a new session can understand the codebase without re-discovering
past pitfalls.

---

## Architecture decisions

### 1. ShipLanguageMapping was completely removed
**Decision:** Languages are stored directly on `CruiseShipORM.extra_language` as a
comma-separated normalised string (e.g. `"italian,spanish"`). There is no separate
ship_languages table.

**Why:** The Språk column in the cruise ship CSV already contains all language codes.
A separate upload step for language mapping was redundant, caused confusion, and introduced
a dependency that made infeasibility hard to diagnose.

**Where:** `src/ingestion/csv_parser.py` — `_parse_sprak()` converts comma-separated
Språk codes to normalised language names. `src/demand/language_matcher.py` —
`get_required_languages()` splits `ship.extra_language` on commas.

**Enum normalisation:** The `normalise_language` validator on `CruiseShipRead` normalises
each comma-separated token with `.lower().strip()`. Always use `.value` on enums — never
`str(enum)` — because `str(RoleCapability.cafe)` gives `"RoleCapability.cafe"` not `"cafe"`.

---

### 2. Language matching is a soft constraint, not hard
**Decision:** Language coverage (having a speaker of a required language on a café shift)
is a soft constraint with weight 100 (the highest), NOT a hard constraint.

**Why:** Making it hard caused INFEASIBLE solver results whenever no speaker was available
for a ship language on a given day. The old `add_language_requirements()` in
`constraints.py` still exists but is NOT called from `scheduler.py`.

**Where:** `src/solver/soft_constraints.py` — `prefer_language_coverage()` uses
`model.NewBoolVar` + `OnlyEnforceIf` pattern to reward coverage without mandating it.

---

### 3. SolveInfo diagnostic object
**Decision:** `ScheduleGenerator` exposes a `solve_info: SolveInfo` attribute populated
during `build_model()` and `solve()`.

**Why:** The solver was silently producing empty schedules (all day-off) without any error.
`SolveInfo` captures status, variable count, available employee count, working assignment
count, wall time, objective value, warnings, and diagnostics so the UI can show exactly
what went wrong.

**Where:** `src/solver/scheduler.py` — `SolveInfo` dataclass at top of file.
`src/ui/pages/4_schedule.py` — shows the solver diagnostics expander after every
generation attempt.

---

### 4. Zero-variable guard in the solver
**Decision:** If `_create_variables()` produces 0 variables, `solve()` returns `None`
immediately with a clear diagnostic message rather than calling CP-SAT.

**Why:** CP-SAT on an empty model returns OPTIMAL (trivially satisfied) with all vars = 0.
`_extract_schedule()` then builds a schedule of only day-off assignments — a silent failure
that was extremely confusing.

**Root cause discovered:** All 12 real employees had `availability_start/end` dates in
2025, but the app was scheduling for 2026. Zero variables were created; CP-SAT returned
OPTIMAL; the UI showed an "empty schedule".

---

### 5. Enum .value must always be used for display and comparison
**Pitfall:** `str(SomeEnum.value)` or f-string interpolation of an enum gives
`"EnumClass.member"` not `"member"`.

**Pattern to use everywhere:**
```python
role_str = emp.role_capability.value if hasattr(emp.role_capability, 'value') else str(emp.role_capability)
```

**Where this caused bugs:** Excel Summary sheet showed "RoleCapability.both" instead of
"Both". Employee filtering in `_write_employee_row()` was comparing enum to string and
always returning False. Fixed in `src/export/excel_export.py`.

---

### 6. Streamlit session state widget anti-pattern
**Pitfall:** Assigning to `st.session_state[key]` AFTER a widget that uses `key=key`
has already rendered raises `StreamlitAPIException`.

**Pattern:**
```python
# WRONG — crashes if the selectbox already rendered with key="editor_year"
st.session_state["editor_year"] = sched.year  # after st.selectbox(..., key="editor_year")

# CORRECT — initialise before any widget
if "editor_year" not in st.session_state:
    st.session_state["editor_year"] = 2026
# For navigation that must change the widget value, use pending keys + rerun:
st.session_state["_pending_editor_year"] = sched.year
st.rerun()
# Then at the TOP of the page (before any widget):
if "_pending_editor_year" in st.session_state:
    st.session_state["editor_year"] = st.session_state.pop("_pending_editor_year")
```

**Where fixed:** `src/ui/pages/5_schedule_editor.py` — lines 137–145 now initialise
`editor_year`/`editor_month` before any widget and handle the pending-key navigation
for the "Use current session schedule" button.

---

### 7. Ghost "NOT SAVED" warning after saving
**Pitfall:** After saving and calling `st.rerun()`, Streamlit keeps the file in the
uploader widget. The `if uploaded:` block re-runs, re-parses the file, and re-sets
`employees_unsaved = len(records)` before the page can show the success state.

**Fix:** Track `_employees_saved_file = uploaded.name` in session state on successful
save. At the top of the `if uploaded:` block, check if the name matches and show a
"already saved" message instead of re-parsing.

**Where:** `src/ui/pages/2_employees.py` and `src/ui/pages/3_cruise_ships.py` — see the
`_employees_saved_file` / `_ships_saved_file` session state keys.

---

### 8. SQLAlchemy create_all does not drop removed tables
**Pitfall:** `Base.metadata.create_all()` is idempotent — it adds missing tables but
never drops tables that no longer have ORM models. When `ShipLanguageMapping` was removed,
its table persisted in the DB.

**Fix:** `src/db/migrations.py` has `reset_all_tables()` which does `drop_all` then
`create_all`. Use this when the schema changes in a breaking way. WARNING: destroys all data.

**Also:** All ORM model files must be imported at the top of `migrations.py` (even if
unused) so they register with `Base.metadata` before `create_all` runs.

---

### 9. LLM JSON must not be shown to the user
**Decision:** The LLM returns action proposals as JSON embedded in its response text.
This JSON is stripped before display using `_strip_json()` in `chat_panel.py`.

**Where:** `src/ui/components/chat_panel.py` — `_strip_json()` uses regex to remove
`\`\`\`json ... \`\`\`` fences and bare `{"action":...}` objects.

---

### 10. LLM changes must never auto-apply
**Decision:** Proposed schedule changes are shown as action cards with individual Apply
buttons and an "Apply All" button. Nothing is applied without explicit user confirmation.

**On apply:** `_do_apply()` in `chat_panel.py`:
- Applies each action via `apply_action()` from `advisor.py`
- Resets schedule status to `draft` (any edit invalidates approval)
- Saves the updated schedule to DB immediately
- Computes hours diff (before vs after) and shows it as a confirmation message

---

### 11. Pre-generate validation warnings
**Decision:** On `4_schedule.py`, before the user can generate, compute `_avail_emps` —
employees whose availability overlaps the selected month. Show an error if zero, a warning
if no ships exist for that month.

**Also:** On employee upload (`2_employees.py`) and ship upload (`3_cruise_ships.py`),
cross-check the other table's years and warn if they don't overlap — this catches the
"all employees have 2025 dates but ships are 2026" mistake early.

---

### 12. Schedule save flow
**Decision:** Two explicit actions:
- **Save Draft** — saves current state, keeps `status=draft`, remains editable (disabled once approved)
- **Approve & Finalize** — saves + sets `status=approved`, locks the schedule
- **Export** — disabled until `status=approved`

**Where:** `src/ui/pages/4_schedule.py` — action buttons section with `_is_approved` flag.

---

### 13. Expandable chat mode
**Decision:** The chat panel has a compact sidebar mode (default, [3,1] columns) and an
expanded mode ([2,3] columns) toggled by a button. The toggle writes `chat_expanded` to
session state; the calling page reads it to set column ratios.

**In expanded mode:** `_render_condensed_schedule()` shows a mini DataFrame of the days
most relevant to the current conversation (±3 days from any proposed change, up to 14 days;
falls back to first 7 days of the month).

**Where:** `src/ui/components/chat_panel.py` + layout section of `4_schedule.py` and
`5_schedule_editor.py`.

---

## Patterns to follow

### Enum values
Always use `.value` for display and DB writes. Use the `hasattr(x, 'value')` guard if
the value might already be a plain string (e.g. after reading from DB before ORM conversion).

### DB session
Always use the context manager:
```python
with db_session() as db:
    ...
```
`db_session()` is in `src/db/database.py`. It commits on exit and rolls back on exception.

### Pydantic models vs ORM models
- `EmployeeRead`, `CruiseShipRead`, `ScheduleRead` etc. are Pydantic (used everywhere in Python code)
- `EmployeeORM`, `CruiseShipORM`, `ScheduleORM` etc. are SQLAlchemy (only used in DB sessions)
- Convert ORM → Pydantic explicitly in data loaders (no `from_orm` — manual mapping)

### LLM calls
All LLM calls go through `src/llm_client.py:chat_completion()`. Never import `openai`
anywhere else.

### Streamlit session state
- Initialise with `if key not in st.session_state: st.session_state[key] = default` at
  the TOP of the page, before any widgets.
- Never assign to a key after its widget has rendered.
- For cross-page navigation values, use `_pending_*` keys + `st.rerun()`.

### Adding a new page
Streamlit multipage: add a file to `src/ui/pages/` with prefix `N_name.py`. The file
must call `st.set_page_config()` as its first Streamlit call.

---

---

## UI/UX fixes (session 2026-03-22)

### 14. Ghost "NOT SAVED" warning after saving — upload pages
**Bug:** After a successful DB save + `st.rerun()`, Streamlit keeps the uploaded file in
the file-uploader widget. The `if uploaded:` block re-runs, re-parses the file, and
re-sets `employees_unsaved = len(records)` — causing the warning banner to reappear
immediately after saving.

**Fix:** Store `_employees_saved_file = uploaded.name` (and `_employees_save_count`) in
session state on successful save. At the top of `if uploaded:`, check if the name matches
— if so, show a "already saved" success message and skip all re-parsing.

**Where:** `src/ui/pages/2_employees.py` and `src/ui/pages/3_cruise_ships.py`.
Keys: `_employees_saved_file`, `_employees_save_count`, `_ships_saved_file`,
`_ships_save_count`.

---

### 15. Schedule save/approve flow clarified
**Decision:** Two explicit actions replace the confusing single "Approve" path:
- **Save Draft** (`disabled=True` once approved) — saves, keeps `status=draft`, editable
- **Approve & Finalize** — saves + locks as `status=approved`
- **Export** — `disabled=not _is_approved` with tooltip explaining why

An `st.info` banner above the buttons explains the flow to the user.

**Where:** `src/ui/pages/4_schedule.py` — action buttons section, controlled by
`_is_approved = schedule.status == ScheduleStatus.approved`.

---

### 16. Year mismatch validation — early warning
**Decision:** Cross-check the opposing table's years at upload time and at generate time.

- On employee upload: query ship years from DB; warn if employee availability years don't
  overlap with ship years.
- On ship upload: query employee availability years; warn if ship years don't overlap.
- On schedule page: compute `_avail_emps` before generating; block generation with an error
  if zero employees are available for the selected month; warn if no ships exist.

**Where:** `2_employees.py`, `3_cruise_ships.py`, `4_schedule.py`.

---

### 17. LLM chat — action sanitisation prevents empty array display
**Bug:** The chat was displaying a large empty array with commas — the raw JSON action list
leaking through as display text.

**Root causes:**
1. `_strip_json()` only caught `{...}` objects, not `[...]` arrays — LLM sometimes returns
   an array `[{"action": ...}]` directly.
2. Empty array artefacts `[]`, `[,]`, `[,,]` were left behind after stripping.
3. `msg["actions"]` could contain `None` or malformed dicts; `if actions:` passes for a
   non-empty list of Nones.

**Fix:**
- `_strip_json()` now also strips bare JSON arrays and empty array artefacts.
- `_sanitise_actions(actions)` filters out any `None`, non-dict, or structurally incomplete
  entry (missing `action`, `employee`, or `date` fields, or invalid action type).
- Called before storing in `_run_chat()` AND before rendering in the message loop.

**Where:** `src/ui/components/chat_panel.py` — `_strip_json()` and `_sanitise_actions()`.

---

### 18. LLM Apply button — in-place per-card feedback
**Bug:** After clicking Apply, there was no immediate confirmation — the user had to scroll
down to find the new confirmation message added to the chat history.

**Fix:** Per-card state tracked in session state:
- `st.session_state["applied_actions"]` — set of `(msg_idx, action_idx)` tuples
- `st.session_state["failed_actions"]` — dict mapping same tuples to error strings

On click: the card key is added to `applied_actions` before `_do_apply` is called.
On rerun: that card slot renders as a green `✅ Applied: [desc]` pill instead of a button.
On exception: the key moves to `failed_actions` and the slot renders as a red `❌ Failed:
[reason]` pill.

`st.toast("Change applied — schedule updated ✅")` fires immediately before `st.rerun()`.

The "Apply All" button only counts *pending* (not already applied or failed) actions.
Clearing the chat (`Clear` button) also clears `applied_actions` and `failed_actions`.

**Where:** `src/ui/components/chat_panel.py` — `_render_action_cards()` and `_do_apply()`.

---

### 19. Expandable chat mode — three-layer escape navigation
**Bug:** The single small toggle button in the chat sidebar was easy to miss when in
expanded mode; users couldn't find how to return to the schedule grid.

**Fix — three explicit escape paths:**
1. **Full-width primary button at the top of the page** (above both columns, in
   `4_schedule.py` and `5_schedule_editor.py`): `"📅 Return to Schedule View — click here
   to go back to the full schedule grid"` — impossible to miss, rendered as a primary
   (blue) button spanning the full page width.
2. **Toggle button renamed** in compact mode: `"🔍 Expand Chat for Editing"` (was
   `"🔍 Expand"`).
3. **Bottom button at the foot of the chat panel**: `"Done editing? → Return to Schedule
   View"` — rendered after the input form in expanded mode only.

**Where:** `chat_panel.py` (toggle label + bottom button) and `4_schedule.py` /
`5_schedule_editor.py` (top banner).

---

### 20. Opening hours coverage — hard constraint in solver

**Bug:** The solver assigned almost all café staff to shift 5 (13:00–21:00). The café
had zero staff from opening (08:30) until 13:00.

**Root cause:** The solver's only staffing constraint was a total headcount per day
(`add_daily_staffing_requirements`). It counted how many people were working but not
*when* they worked. Putting everyone on shift 5 satisfied the count while leaving the
morning completely unstaffed.

**Fix:** `add_opening_hours_coverage()` in `src/solver/constraints.py`:
1. Loads `EstablishmentSettings` from DB (via `ScheduleGenerator._load_settings()` in
   `build_model()` if not already provided).
2. Divides each day's operating hours into 1-hour slots.
3. For each slot, computes which café shifts cover it
   (`shift.start_time ≤ slot_start AND shift.end_time ≥ slot_end`).
4. Adds hard constraint: `sum(café_employees_on_covering_shifts) ≥ 1`.

Key forced assignments for peak season (08:30–20:15):
- Slot 08:30–09:30: **only shift 1** covers it → forces ≥1 person on shift 1 every day.
- Slot 19:30–20:15: **only shift 5** covers it → forces ≥1 person on shift 5 every day.
- All intermediate slots are covered by multiple shifts (solver has freedom).

**Production coverage:** Same logic applied but only for slots covered by ≥2 production
shifts. Extreme slots (only P1 or only P5) are skipped to avoid infeasibility with the
limited production headcount.

**Where:** `src/solver/constraints.py` — `add_opening_hours_coverage()`.
`src/solver/scheduler.py` — `_load_settings()`, `_add_hard_constraints()`.

---

### 21. Shift variety — soft constraint in solver

**Problem:** Even with the coverage constraint fixing the morning/evening, the solver
still tended to assign each employee to the same shift every day (e.g. always shift 1
or always shift 5), producing an unnatural rigid schedule.

**Fix:** `penalize_same_shift_consecutive()` in `src/solver/soft_constraints.py`.
Weight = 2 (low, so it doesn't fight coverage or staffing constraints). For each employee
and each pair of consecutive calendar days, creates a `BoolVar both_same` that is 1 iff
the employee is on the exact same shift ID on both days. Penalises this with `-weight`
in the maximisation objective.

Uses CP-SAT pattern:
```python
model.AddBoolAnd([v1, v2]).OnlyEnforceIf(both_same)
model.AddBoolOr([v1.Not(), v2.Not()]).OnlyEnforceIf(both_same.Not())
```

**Where:** `src/solver/soft_constraints.py` — `penalize_same_shift_consecutive()`,
`WEIGHTS["shift_variety"] = 2`, called from `add_soft_constraints()`.

---

### 22. LLM Apply — grid refresh flash banner

**Bug:** After clicking Apply on an LLM-proposed schedule change, the schedule grid
visually re-rendered (because `_do_apply()` calls `st.rerun()`) but there was no
confirmation visible next to the grid. Users couldn't tell whether their click worked.

**Fix:** `_do_apply()` in `chat_panel.py` now sets
`st.session_state["_apply_flash"] = label` before calling `st.rerun()`. The calling
pages (`4_schedule.py` and `5_schedule_editor.py`) pop this key with `.pop()` immediately
after `st.divider()`, showing a `st.success()` banner above the grid. The `.pop()` pattern
ensures the banner appears exactly once (on the rerun triggered by apply) and disappears
on the next user interaction.

**Where:** `src/ui/components/chat_panel.py` — `_do_apply()` (sets `_apply_flash`).
`src/ui/pages/4_schedule.py` and `5_schedule_editor.py` — pop and show the banner.

---

## Known limitations / future work

- The solver timeout is 60 seconds. For large months (many employees, many constraint
  combinations), it may return FEASIBLE (not OPTIMAL). This is acceptable.
- The Excel export uses a fixed two-block layout (days 1–15, 16–31). It does not adapt
  to months shorter than 31 days in a cosmetically perfect way.
- The LLM advisor has no memory of which changes were applied in previous sessions — it
  only has the current conversation history (capped at 20 turns).
- `6_export.py` (renamed from `5_export.py` after editor was added as page 5) — if the
  Export page is referenced as page 5 anywhere, check the actual filename.
