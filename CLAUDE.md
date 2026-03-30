# Claude Instructions — Geiranger Sjokolade Scheduler

Read this file at the start of every session. It tells you exactly how to work on this
project without re-discovering past mistakes or asking unnecessary questions.

**Also read:** `PROJECT.md` (architecture + data models) and `DECISIONS.md` (why things
are the way they are, including all past bug fixes).

---

## Project summary

A Streamlit + PostgreSQL + OR-Tools scheduling application for a Norwegian chocolate café.
All code lives in `src/` which is volume-mounted into the Docker container — edits are
live immediately without rebuild. The app runs at http://localhost:8510.

Container names: `geiranger-scheduler-app` (app) and `geiranger-scheduler-db` (Postgres).

---

## How to start a session

1. Read `PROJECT.md` for the overall architecture.
2. Read `DECISIONS.md` for past bugs and key patterns.
3. Ask the user what they want to fix or add.
4. Read the specific file(s) before editing — never modify code you haven't read.

To verify the app is running:
```bash
docker ps --format "{{.Names}}"
# Should show: geiranger-scheduler-app  geiranger-scheduler-db
```

To check syntax on changed files:
```bash
docker exec geiranger-scheduler-app python -c "
import ast, sys
for f in ['src/ui/pages/4_schedule.py']:
    try: ast.parse(open('/app/'+f).read()); print('OK '+f)
    except SyntaxError as e: print('ERR '+f+': '+str(e)); sys.exit(1)
"
```

To run a Python snippet against live data:
```bash
docker exec geiranger-scheduler-app python -c "..."
```

---

## Critical rules — always follow these

### Never assign to a session state key after its widget has rendered
```python
# WRONG — raises StreamlitAPIException
st.selectbox("Year", [...], key="editor_year")
st.session_state["editor_year"] = some_value  # too late!

# CORRECT — initialise before any widget
if "editor_year" not in st.session_state:
    st.session_state["editor_year"] = default_value
# To navigate (change value from a button click), use pending keys:
st.session_state["_pending_editor_year"] = new_value
st.rerun()
# Then at the TOP of the page (before any widget):
if "_pending_editor_year" in st.session_state:
    st.session_state["editor_year"] = st.session_state.pop("_pending_editor_year")
```

### Always use .value for enum display and DB writes
```python
# WRONG — gives "RoleCapability.cafe"
f"Role: {emp.role_capability}"

# CORRECT
role = emp.role_capability.value if hasattr(emp.role_capability, 'value') else str(emp.role_capability)
```

### Never call str() on an enum for comparison
```python
# WRONG
if emp.role_capability in ("cafe", "both"):

# CORRECT
if emp.role_capability in (RoleCapability.cafe, RoleCapability.both):
# or
if emp.role_capability.value in ("cafe", "both"):
```

### LLM calls only through llm_client.py
```python
# Never: import openai
# Always:
from src.llm_client import chat_completion
response = chat_completion(messages, temperature=0.3)
```

### DB sessions — always use context manager
```python
with db_session() as db:
    rows = db.query(EmployeeORM).all()
    # commits on exit, rolls back on exception
```

### Migrations — all ORM models must be imported in migrations.py
When adding a new model, add its import to the top of `src/db/migrations.py` even if the
function doesn't use it directly. This ensures `Base.metadata` knows about the table.

---

## Page-by-page guide

### `src/ui/pages/2_employees.py`
- 3 tabs: Employee List, Edit Employee, Upload CSV
- Upload tab tracks `_employees_saved_file` and `_employees_save_count` in session state
  to avoid re-showing "not saved" warning after a successful save + rerun
- `employees_unsaved` key shows the top-of-page warning banner

### `src/ui/pages/3_cruise_ships.py`
- 3 tabs: Ship List, Calendar View, Upload Ships CSV (language mapping tab was removed)
- Same save-tracking pattern as employees: `_ships_saved_file`, `_ships_save_count`
- Language data lives in `CruiseShipORM.extra_language` as comma-separated string

### `src/ui/pages/2_employees.py`
- Upload tab: `_employees_saved_file` / `_employees_save_count` in session state prevent
  the "not saved" banner from reappearing after a successful save + rerun
- Year mismatch check at upload: cross-queries ship years from DB and warns if no overlap

### `src/ui/pages/3_cruise_ships.py`
- Same saved-file tracking as employees: `_ships_saved_file`, `_ships_save_count`
- Year mismatch check at upload: cross-queries employee availability years

### `src/ui/pages/4_schedule.py`
- Month/year selectors use `key="sched_year"` / `key="sched_month"` (NOT "schedule_year")
- `_avail_emps` computed before generate button — shows error if zero, blocks generation
- `_is_approved = schedule.status == ScheduleStatus.approved` controls button states:
  - "Save Draft" disabled when approved
  - "Approve & Finalize" is the approval action (was "Approve")
  - "Export" disabled until approved, with tooltip
- **Two-pass generation flow:**
  1. First pass: normal solver. On success → show schedule. On INFEASIBLE → store
     `_inf_info`, `_inf_demand`, `_inf_year`, `_inf_month` in session state; render
     diagnostics + "⚡ Generate Best-Effort Schedule" button (key: `fallback_btn`)
  2. Fallback button → calls `run_fallback_solve()` in `src/solver/fallback.py`; on
     success creates `ScheduleRead(is_fallback=True, fallback_notes=JSON)` and stores
     `_fallback_result` in session state
  3. Clear all `_inf_*` / `_fallback_result` keys on new Generate click or Load Saved
- Solver diagnostics helper `_render_solver_diagnostics(info)` defined inline on the page
- Column ratio: `[2, 3] if chat_expanded else [3, 1]`
- **When `chat_expanded` is True**, a full-width primary "Return to Schedule View" button
  is rendered above both columns (key: `return_schedule_banner`)
- Flash banner: after `st.divider()` inside the `else:` branch, pop `_apply_flash` from
  session state and show `st.success()` — this gives immediate grid-update confirmation
  after an LLM Apply click
- **Cross-month warnings:** `_next_month_name_if_exists` + `_stale_prev_month_warning`
  helpers; warn after Save/Approve if M+1 exists; always-on stale banner if M-1 was
  modified after M was generated
- **Fallback banner:** shown when `schedule.is_fallback` — yellow bordered container with
  relaxation notes list + staffing gaps expander

### `src/ui/pages/5_schedule_editor.py`
- Session state initialised at module level (before columns/widgets):
  - `editor_year` defaults to 2026, `editor_month` defaults to 8
  - `_pending_editor_year` / `_pending_editor_month` handled before widgets for navigation
- "Use current session schedule" button uses pending keys + `st.rerun()`
- Column ratio: `[2, 3] if chat_expanded else [3, 1]`
- Same full-width return banner as `4_schedule.py` when expanded
- Flash banner: same `_apply_flash` pop pattern after `st.divider()`, before `st.subheader`
- Same cross-month warning helpers and fallback banner as `4_schedule.py`

### `src/ui/pages/6_export.py`
- 4 tabs: Download (Excel+PDF), Validation, Employee Summary, Coverage Heatmap
- Excel uses openpyxl, PDF uses ReportLab — both must be installed in the container

### `src/ui/components/chat_panel.py`
- `render_chat_panel(schedule, employees, demand, shift_templates)` — call from any page
- `chat_expanded` in session state controls layout; three-layer escape navigation:
  1. Full-width primary button on the calling page (above columns)
  2. Toggle button in the panel header (compact: "🔍 Expand Chat for Editing")
  3. Bottom button below the input form (expanded mode only)
- `_strip_json(text)` strips JSON objects AND arrays AND empty artefacts like `[]`, `[,,]`
- `_sanitise_actions(actions)` — always call before storing or rendering action lists;
  discards None, non-dict, missing required fields, invalid action types
- `_do_apply(actions, ...)` applies, saves to DB, resets to draft, shows diff + `st.toast`
- Per-card apply tracking via `applied_actions` (set) and `failed_actions` (dict) in
  session state — keyed by `(msg_idx, action_idx)`. Cards turn green on success, red on
  failure. Cleared by the "Clear" button.
- `_render_condensed_schedule()` shown in expanded mode — ±3 days around proposed changes

---

## Solver quick reference

**`ScheduleGenerator(employees, demand, shifts)`**
```python
gen = ScheduleGenerator(employees, demand, shifts)
gen.build_model()   # auto-loads EstablishmentSettings from DB if not passed
result = gen.solve()   # ScheduleRead or None
info = gen.solve_info  # SolveInfo with status, warnings, diagnostics
```

**Opening hours coverage (hard constraint):**
`add_opening_hours_coverage()` in `constraints.py` ensures ≥1 café employee is on a
shift covering every 1-hour slot from `opening_time` to `closing_time`. Loaded settings
come from `EstablishmentSettings` DB table (auto-loaded by `_load_settings()` in
`build_model()`). Coverage is `≥1` per slot — total headcount is left to
`add_daily_staffing_requirements`. Production coverage applies only to slots with ≥2
covering shifts to avoid over-constraining with limited headcount.

**Shift variety (soft constraint, weight=2):**
`penalize_same_shift_consecutive()` in `soft_constraints.py` penalises assigning the
same shift to an employee on two consecutive calendar days.

**`SolveInfo` key attributes:**
- `info.is_success` — True if OPTIMAL/FEASIBLE with at least 1 working assignment
- `info.is_empty_solution` — True if OK but 0 working shifts (availability mismatch)
- `info.warnings` — list of pre-flight warnings (availability gaps, language gaps)
- `info.diagnostics` — list of infeasibility hints

**Common infeasibility causes:**
1. Employee `availability_start`/`availability_end` dates don't overlap with the scheduled month
2. Peak café demand exceeds number of café-capable employees available
3. Eidsdal transport constraint: no licensed driver among Eidsdal workers
4. 35h weekly rest + 48h weekly limit conflict when very few employees cover many days
5. Opening hours coverage constraint: if a season's operating window requires both an
   early shift AND a late shift every day but not enough employees are available, the
   model becomes infeasible — check employee headcount vs peak season staffing rules

**To reset the DB when schema changes:**
```bash
docker exec geiranger-scheduler-app python -c "
from src.db.migrations import reset_all_tables
from src.db.seed import seed_defaults
reset_all_tables()
seed_defaults()
print('Done')
"
```
WARNING: this destroys all data.

---

## Language handling

Languages flow: CSV Språk column → `_parse_sprak()` → `CruiseShip.extra_language` (comma-separated) → `get_required_languages()` (split + filter "english") → `DailyDemand.languages_required` → `prefer_language_coverage()` soft constraint.

The `normalise_language` validator on `CruiseShipRead` lowercases and strips each token.
`"english"` is always filtered out from required languages (everyone speaks it).

Supported Språk codes in `csv_parser.py`:
```python
_LANG_CODES = {
    "i": "italian",  "e": "english",  "f": "french",
    "s": "spanish",  "d": "german",   "n": "norwegian",
    "p": "portuguese", "j": "japanese",
}
```

---

## Export files

### Excel (`src/export/excel_export.py`)
- Two half-month blocks stacked: days 1–15 top, days 16–31 bottom
- Color coding: green = working, red = day-off (full-time), orange = unscheduled (part-time)
- Cruise info rows between employee rows showing ship arrivals
- Summary sheet with per-employee hours, role, and employment type

### PDF (`src/export/pdf_export.py`)
- ReportLab landscape A4
- Schedule grid + shift legend + summary stats page

---

## Common tasks

### Add a new hard constraint
1. Write a function in `src/solver/constraints.py` following the existing pattern
2. Call it from `ScheduleGenerator._add_hard_constraints()` in `scheduler.py`
3. Write a unit test in `tests/test_constraints.py`

### Add a new soft constraint
1. Add weight to `WEIGHTS` dict in `src/solver/soft_constraints.py`
2. Write function following the `obj_vars` / `obj_coeffs` append pattern
   (append positive values to reward, negative to penalise — objective is maximised)
3. Call from `add_soft_constraints()` in same file

### Debug opening hours coverage infeasibility
The `add_opening_hours_coverage()` constraint is the most likely cause of new INFEASIBLE
results after the solver previously worked. Check:
1. How many café-capable employees are available for the month?
2. Peak season (08:30–20:15) requires ≥1 on shift 1 AND ≥1 on shift 5 every day — that's
   a minimum of 2 distinct café workers per day on top of any weekly-rest constraints.
3. Production coverage only applies to slots with ≥2 covering shifts, so it is safer, but
   still verify production headcount vs production_needed values in seasonal rules.
4. If infeasible after adding employees, check if the harbor-time logic is being triggered
   (currently removed — coverage is always ≥1 per slot, not elevated for harbor time).

### Add a new DB model
1. Add ORM class to `src/models/`
2. Add Pydantic Base/Create/Read schemas
3. Import the model module in `src/db/migrations.py`
4. Run `reset_all_tables()` + `seed_defaults()` if needed

### Fix a UI session state bug
1. Check: is the key used as a widget `key=` parameter?
2. If yes, initialise it at the TOP of the page before any widgets
3. Never re-assign it after the widget renders
4. For navigation (changing value via button), use `_pending_*` + `st.rerun()`

### Debug an empty schedule
1. Check `gen.solve_info.warnings` — are any employees unavailable this month?
2. Compare employee `availability_start`/`availability_end` years vs scheduled month year
3. Check `gen.solve_info.num_variables` — if 0, no employee overlaps the month at all
4. Check `gen.solve_info.status_name` — INFEASIBLE means constraints conflict; UNKNOWN means timeout

---

## What NOT to do

- Do not create `docs/` files inside the Docker container — `docs/` is not volume-mounted
- Do not use `str(enum_value)` for display, comparisons, or DB writes
- Do not assign to `st.session_state[key]` after the widget with `key=key` has rendered
- Do not import `openai` anywhere except `src/llm_client.py`
- Do not call `Base.metadata.create_all()` without importing all model modules first
- Do not add a ShipLanguageMapping table or ShipLanguageCreate model — intentionally removed
- Do not use `add_language_requirements()` from `constraints.py` — moved to soft constraints
- Do not apply LLM-suggested changes automatically — always require user confirmation
- Do not show raw JSON in the chat UI — always strip with `_strip_json()` AND sanitise
  with `_sanitise_actions()` before storing or rendering
- Do not check `if actions:` before rendering — always use `_sanitise_actions(actions)`
  first; a list of Nones is truthy but renders as empty brackets
- Do not store applied/failed action state anywhere except `applied_actions` and
  `failed_actions` in session state — both are dicts/sets of `(msg_idx, action_idx)` tuples
- Do not add a "Return to schedule" button only inside the chat column in expanded mode —
  the full-width primary button must live on the calling page above the columns, otherwise
  it's too small to find
- Do not rename "Save Draft" + "Approve & Finalize" back to a single "Approve" button —
  the two-step flow is intentional and user-tested
- Do not remove or bypass `add_opening_hours_coverage()` — it is the hard constraint that
  prevents everyone being assigned to shift 5; without it the solver trivially satisfies
  headcount by putting all café staff on the last shift
- Do not apply the harbor-time elevated minimum to single-shift slots (e.g. 08:30–09:30
  is only covered by shift 1) — requiring cafe_needed people on that one shift makes the
  model INFEASIBLE; harbor elevation is removed; total headcount is handled by the
  separate `add_daily_staffing_requirements` constraint
- Do not set `_apply_flash` in session state anywhere other than `_do_apply()` in
  `chat_panel.py`; the calling pages pop it with `.pop()` so it shows exactly once
