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

## UI/UX fixes

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

**NEVER remove or weaken this constraint.** It is the sole guarantee that the café is
never empty during operating hours. The `if not cov_vars: continue` guard only fires when
literally no employee has a variable for that slot (outside availability window) — it is
not a loophole.

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

### 23. Three new hard constraints — cross-month consecutive day violations
**Problem:** Employees were appearing to work 7–11 consecutive days when their July
schedule ended with several working days and their August schedule began with several more.
The solver had no knowledge of the previous month's assignments.

**Fix part 1 — cross-month constraint:**
`add_cross_month_consecutive_constraint()` in `constraints.py`. Called during
`build_model()` after `_load_prev_month_working()` queries the DB for M-1's schedule.

Algorithm: for each employee, count consecutive working days ending on the last day of
M-1 (`carry_in`, 0–6). If carry_in > 0, add a single binding constraint:
`sum(shift_vars on days 1…{7−carry_in} of M) ≤ 6−carry_in`

Only ONE constraint per employee is needed — all weaker windows are implied by it.
Returns the count of constraints added (logged for debugging).

**Fix part 2 — three new within-month hard constraints:**
1. `add_max_days_per_calendar_week()` — ≤6 per Mon–Sun ISO week. Mathematically implied
   by weekly rest but enforced explicitly as a named safety constraint.
2. `add_max_consecutive_working_days()` — rolling gap-aware 7-day windows; only
   constrains windows of exactly 7 consecutive calendar days (no date gaps).
3. `add_two_consecutive_days_off_per_14()` — every rolling 14-day window must contain
   ≥1 pair of adjacent off-days. Uses `pair_off_bv[(emp_id, d0)]` BoolVars (1 iff both
   d0 and d0+1 are off). Handles 4 cases: both outside availability, one inside, both inside.

**Performance note:** `add_two_consecutive_days_off_per_14` adds significant complexity.
For August (31 days), the solver hits the 60s timeout and returns FEASIBLE (not OPTIMAL).
This is acceptable — the solution respects all constraints.

**Validator updated:** `src/solver/validator.py` has corresponding check functions:
`_check_max_days_per_calendar_week`, `_check_max_consecutive_working_days` (cross-month
aware via `_load_prev_month_working_dates`), `_check_two_consecutive_days_off_per_14`.

**Where:** `src/solver/constraints.py`, `src/solver/scheduler.py`, `src/solver/validator.py`.

---

### 24. Over-coverage tiered penalty
**Problem:** The solver was assigning 1 extra person on 6 scattered days to fill full-time
contracted hours, but all the extras were on the same day — concentrating over-coverage
rather than spreading it.

**Fix:** `penalize_over_coverage()` in `soft_constraints.py`. Two BoolVar tiers per
(day, role) where `min_needed > 0`:
- `ov1`: 1 if assigned ≥ min_needed + 1 → penalty −3
- `ov2`: 1 if assigned ≥ min_needed + 2 → additional penalty −5

Effect: spreading 2 extras across 2 days costs −6; concentrating both on 1 day costs −8.
The solver prefers spreading.

**Where:** `src/solver/soft_constraints.py` — `penalize_over_coverage()`,
`WEIGHTS["over_coverage_t1"] = 3`, `WEIGHTS["over_coverage_t2"] = 5`.

---

### 25. Staffing minimum capped to available headcount
**Problem:** `add_daily_staffing_requirements` added hard constraint
`sum(cafe_vars) >= demand.cafe_needed`. If `cafe_needed = 4` but only 3 café employees
are available that day, the model is provably INFEASIBLE.

**Fix:** Cap the hard minimum to the actual number of distinct café employees with
variables for that day:
```python
cafe_emps_today = len({emp.id for emp in employees for s in cafe_shifts
                        if (emp.id, d, s.id) in variables})
effective_cafe_min = min(demand.cafe_needed, cafe_emps_today)
model.Add(sum(cafe_vars) >= effective_cafe_min)
```
Same logic for production.

**Why this still causes INFEASIBLE for low headcount:** If `effective_cafe_min = 3`
(= all available café employees) every day for a 31-day month, the solver must work all
3 every single day — but weekly rest requires ≥1 day off per 7 days. This is still
INFEASIBLE. The fallback solver (Step C onwards) further reduces the minimum, giving
employees enough slack for rest days.

**Where:** `src/solver/constraints.py` — `add_daily_staffing_requirements()`.

---

### 26. Two-pass fallback solver
**Decision:** When INFEASIBLE, instead of a dead end, offer a best-effort schedule with
progressively relaxed constraints.

**Why a button rather than automatic fallback:** The user must be aware that constraints
were relaxed before using or publishing the schedule. An automatic silent fallback would
hide the staffing shortage problem.

**Implementation:** `src/solver/fallback.py` — `run_fallback_solve()` tries steps B→C→D→E.
Each step creates a new `ScheduleGenerator` with a modified `demand` list (produced by
`_relax_demand()`). Steps B and C reduce `cafe_needed`/`production_needed` in the demand
dataclasses using `dataclasses.replace()`. Step D additionally passes
`disable_both_preference=True` to `build_model()`, which threads through to
`add_soft_constraints()` and skips `prefer_both_on_production()`.

**`disable_both_preference` threading:**
- `ScheduleGenerator.build_model(disable_both_preference=False)` → stores as `self._disable_both_preference`
- `_add_soft_constraints()` → passes via `getattr(self, "_disable_both_preference", False)`
- `add_soft_constraints(..., disable_both_preference=False)` → conditionally calls `prefer_both_on_production`

**Session state for fallback:**
- `_inf_info`, `_inf_demand`, `_inf_year`, `_inf_month` — stored when first pass is INFEASIBLE
- `_fallback_result` — stored after successful fallback (holds StaffingGap list for the session)
- Cleared on: new Generate click, Load Saved click

**Persistence:** `ScheduleRead.is_fallback` + `fallback_notes` (JSON) stored in DB.
`staffing_gaps_from_json()` and `relaxation_notes_from_json()` reconstruct on load, so
the banner works even after a session restart.

**Where:** `src/solver/fallback.py`, `src/solver/soft_constraints.py`,
`src/solver/scheduler.py`, `src/ui/pages/4_schedule.py`, `src/ui/pages/5_schedule_editor.py`,
`src/export/excel_export.py`.

---

### 27. Cross-month edit warnings
**Problem:** If the user regenerates May and then looks at June (which was generated using
the old May as its cross-month carry-in), June's consecutive-day constraints may now be
wrong without any visual indication.

**Fix — two warnings:**
1. **After saving/approving M** (inline or persisted via `_cross_month_warn_sched`):
   if M+1 exists in DB, warn to regenerate M+1.
2. **Always-on banner on M's page:** if M-1's `modified_at > M's created_at`, warn that
   M was generated before the latest version of M-1.

**Helper functions** (in both `4_schedule.py` and `5_schedule_editor.py`):
- `_next_month_name_if_exists(year, month)` — queries DB for M+1
- `_stale_prev_month_warning(schedule)` — compares timestamps; returns warning string or None

**Precision:** Uses `prev_orm.modified_at > schedule.created_at` (not `modified_at`) to
avoid false positives from loading/viewing without editing.

**Where:** `src/ui/pages/4_schedule.py` and `src/ui/pages/5_schedule_editor.py`.

---

### 28. Dark mode — chat panel HTML divs
**Bug:** The LLM chat panel renders message bubbles and action cards as raw HTML divs.
In dark mode, these divs had light background colors but no explicit text color set,
so the text was dark-on-dark and invisible.

**Fix:** Added explicit `color:` declarations to all inline HTML divs:
- Message bubbles: `color:#1a1a1a`
- Proposed changes banner: `color:#7B6000`
- Applied card: `color:#155724`
- Failed card: `color:#721c24`
- Pending card: `color:#1a1a1a`
- Empty state: `color:#888`

**Where:** `src/ui/components/chat_panel.py` — all `st.markdown(f"<div style=...>")` calls.

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
- When reading new optional columns from ORM, use `getattr(orm, "col_name", default)` for backwards compatibility

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

### Adding a new DB column
1. Add to ORM model in `src/models/`
2. Add to Pydantic Read schema (with default for backwards compat)
3. Add `ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...` to `run_safe_migrations()` in `src/db/migrations.py`
4. Read with `getattr(orm, "col_name", default)` until you know the migration has run
5. Run `docker exec geiranger-scheduler-app python -c "from src.db.migrations import run_safe_migrations; run_safe_migrations()"` to apply immediately

---

### 29. CSV parser — tolerant real-world input
**Decision:** `parse_employees_csv()` in `src/ingestion/csv_parser.py` was rewritten to be
highly tolerant of imperfect data and returns a 3-tuple `(records, errors, corrections)`.

**Key behaviours:**
- Accepts `age` OR `date_of_birth` column; parses dates in YYYY-MM-DD, DD-MM-YYYY, D MMM YYYY,
  D MMMM YYYY, Norwegian month names (jan/feb/mars/apr/mai/jun/jul/aug/sep/okt/nov/des),
  and plain integers (interpreted as age → computed as `today - age years`).
- Accepts `Cafe/café/Caf/Kafe → "cafe"`, `Production/Manager Production → "production"`,
  `Both/Begge → "both"` — all case-insensitive.
- Accepts `part-time/Part-time/part_time` for `employment_type`.
- Accepts `1/0/yes/no/true/false/TRUE/FALSE/ja/nei` for `driving_licence`.
- Normalises `housing` case-insensitively.
- Normalises language names to lowercase; auto-adds "english" if missing; splits on `;` or `,`.
- Skips blank rows silently.
- Collects per-row correction notes (e.g. `"Normalised: Caf → cafe, 1 → true"`) returned
  as the third element of the tuple.

**Return type change:** `EmployeeParseResult = tuple[list, list[dict], list[str]]`
The calling page (`2_employees.py`) unpacks: `records, errors, corrections = parse_employees_csv(uploaded)`.

**Where:** `src/ingestion/csv_parser.py` — `_ROLE_MAP`, `_EMPTYPE_MAP`, `_MONTH_NAMES`,
`_DOB_FORMATS`, `_normalize_role`, `_normalize_emptype`, `_normalize_housing`,
`_coerce_bool_noted`, `_parse_dob`, `_normalize_languages`, `_is_blank_row`.
`src/ui/pages/2_employees.py` — corrections expander added under the upload tab.

---

### 30. dayfirst=True pandas date parsing bug — fixed with explicit strptime
**Bug:** `pd.to_datetime("2026-05-01", dayfirst=True)` returned January 5, 2026 instead of
May 1, 2026 — swapping day and month in ISO-format strings that have day ≤ 12.

**Fix:** Replaced direct pandas usage in `_parse_date_field()` with a custom `_parse_dob()`
that tries each explicit `strptime` format in order. ISO format (`%Y-%m-%d`) is tried first
before any fuzzy/dayfirst parsing. Pandas `dayfirst=True` is never used on availability dates.

**Where:** `src/ingestion/csv_parser.py` — `_parse_dob()`, `_parse_date_field()`.

**Rule:** Never use `pd.to_datetime(..., dayfirst=True)` on fields that could be ISO-format
dates. Always use explicit `datetime.strptime(val, "%Y-%m-%d")` first.

---

### 31. Staffing rules stored in DB, editable in Settings
**Decision:** Minimum staffing counts (café/production per season/scenario) are stored in a
`staffing_rules` DB table rather than only as hardcoded Python constants. The table is seeded
from `STAFFING_RULES` on first run (if empty). The Settings page Tab 3 provides an editable
`st.data_editor` grid.

**Why:** The business owner needs to adjust staffing levels (e.g. add 1 café worker on all
peak days) without touching code. The DB is the source of truth at runtime; the hardcoded
`STAFFING_RULES` dict is the fallback.

**Flow:** `generate_monthly_demand()` calls `load_staffing_rules_from_db()` (in
`seasonal_rules.py`) which queries the DB, merges any missing keys from hardcoded defaults,
and returns the same nested-dict structure. Transparent to callers.

**Where:**
- `src/models/staffing_rule.py` — `StaffingRuleORM` with `UniqueConstraint("season","scenario")`
- `src/demand/seasonal_rules.py` — `load_staffing_rules_from_db()` (lazy DB import to avoid circulars)
- `src/db/seed.py` — `seed_staffing_rules()` (idempotent, only inserts if count=0)
- `src/db/migrations.py` — `CREATE TABLE IF NOT EXISTS staffing_rules (...)`
- `src/demand/forecaster.py` — `generate_monthly_demand()` now accepts `rules: dict | None = None`
- `src/ui/pages/1_settings.py` — Tab 3 replaced with editable data_editor

---

### 32. Closed days — solver skip + grey grid columns
**Decision:** A `closed_days` DB table lets the manager mark specific dates as shop-closed.
Closed days are completely skipped by the demand engine (no assignments generated) and
displayed as locked grey columns in the schedule grid and Excel export.

**Why closed days count as rest automatically:** The demand engine produces no `DailyDemand`
entries for closed days, so the solver's `_days` list has a gap. The consecutive-day
constraint only fires on windows of exactly 7 consecutive calendar days — a gap breaks
the window, so the closed day implicitly provides rest without an explicit "day off" assignment.

**UI:** Expandable "🔒 Closed Days" section above the Generate button on the Schedule page.
A weekly checkbox grid (Mon–Sun, padded) lets the manager toggle dates. State is kept in
`st.session_state[f"closed_days_{year}_{month}"]` (loaded from DB on first access). Save
button calls `save_closed_days()` which replaces the month's records atomically.

**Grid rendering:** `build_schedule_html()` and `render_schedule_grid()` accept
`closed_days: set[date] | None`. Closed columns get a grey 🔒 header; employee cells show
`—` in grey; cruise info shows "CLOSED" in the Ships row only.

**Excel:** Same grey treatment with `COLORS["closed"] = "CCCCCC"` in both half-blocks.

**Where:**
- `src/models/closed_day.py` — `ClosedDayORM` (UUID pk, date UNIQUE, year, reason nullable)
- `src/db/migrations.py` — `CREATE TABLE IF NOT EXISTS closed_days (...)`
- `src/demand/forecaster.py` — `generate_monthly_demand(closed_days=...)` skips closed dates
- `src/solver/scheduler.py` — `__init__(closed_days=...)` filters `_days` and `_demand_map`
- `src/ui/components/schedule_grid.py` — `build_schedule_html`, `_employee_row_html`, `_cruise_info_rows_html`, `render_schedule_grid` all accept `closed_days`
- `src/export/excel_export.py` — `export_schedule_to_excel(closed_days=...)` passes through to half-writers
- `src/ui/pages/4_schedule.py` — `load_closed_days()`, `save_closed_days()` helpers; closed days calendar UI; `_closed_days` wired to generate/load/render calls

---

### 33. Skeleton mode — last-resort fallback pass
**Decision:** After Steps B/C/D/E all fail, a final "Skeleton Mode" pass runs with the
absolute minimum constraints. This ensures *some* schedule is always returned rather than
a total failure.

**What skeleton mode does:**
- Sets café minimum = 1 per open day, production minimum = 0 on all days
- Drops: `add_two_consecutive_days_off_per_14`, `add_sunday_rest_constraints`,
  `add_max_staffing_caps`, `add_opening_hours_coverage`
- Uses **`model.Minimize(sum_of_all_vars))`** instead of the normal maximise objective
  (gives employees maximum rest)
- `disable_both_preference=True` (flex employees freely assigned)

**Implementation:**
- `ScheduleGenerator.build_model(skeleton_mode=True)` — stores flag, skips 4 complex constraints,
  calls `_add_skeleton_objective()` instead of `_add_soft_constraints()`
- `src/solver/fallback.py` — Skeleton step after E; `FallbackResult.is_skeleton` property;
  `is_skeleton_from_json(fallback_notes_json)` helper for loading from DB

**UI:**
- Red `st.error` banner ("🚨 SKELETON SCHEDULE") instead of yellow warning
- Staffing gaps expander auto-expanded (`expanded=True`) for skeleton
- Caption lists the four dropped constraints

**Where:** `src/solver/scheduler.py`, `src/solver/fallback.py`, `src/ui/pages/4_schedule.py`.

---

---

### 34. Skeleton mode objective changed: minimize → maximize
**Decision:** Changed `_add_skeleton_objective()` from `model.Minimize(sum_of_all_vars)`
to `model.Maximize(sum_of_all_vars)`.

**Why:** The original minimize-assignments objective left employees severely under-rostered
(solver assigns the bare legal minimum). Skeleton mode is the last resort — employees
should still get as close to their contracted hours as possible within the relaxed constraints.

**Where:** `src/solver/scheduler.py` — `_add_skeleton_objective()`.

---

### 35. Opening hours coverage always enforced — even in skeleton mode
**Decision:** `add_opening_hours_coverage()` moved outside the `if not skeleton:` block.
It is now called unconditionally in `_add_hard_constraints()`.

**Why:** Without it the solver puts all café staff on shift 5. Skeleton mode drops
`add_two_consecutive_days_off_per_14`, `add_sunday_rest_constraints`, and
`add_max_staffing_caps` — but opening hours is "never-relax" alongside legal rest periods.
The constraint already guards against empty `cov_vars` per slot, so it stays feasible at
minimum headcount.

**Where:** `src/solver/scheduler.py` — `_add_hard_constraints()`.

---

### 36. Infeasibility hint: impossible days shown explicitly
**Decision:** `_generate_infeasibility_hints()` checks each demand day for the pigeonhole
condition: if `cafe_needed + production_needed > total_available_employees`, the hint names
the date, roles, and counts with "Close this day or reduce staffing rules."

**Why May 2026 is infeasible:** Only 3 employees available (Aina, Jose, Paula). Cruise days
require café=3 + prod=1 = 4 workers. Impossible by pigeonhole. Fix: close the specific cruise
days or reduce low-season staffing rules from café=3 to café=2.

**Why June 2026 is infeasible:** Peak demand requires prod=2, but only Aina (role=both) is
production-capable before June 20 (Ferran/Noel start June 20, Marta June 28). Fix: reduce
mid-season production minimum to 1 for June 1–15.

**Where:** `src/solver/scheduler.py` — `_generate_infeasibility_hints()`.

---

### 37. Part-time overtime fix: minimize_overtime uses contracted_hours per employee
**Decision:** `minimize_overtime()` was using `_ADULT_NORMAL_WEEKLY_WORKED_MIN` (37.5h)
as the target for ALL employees. Since the hard cap is also 37.5h, overtime was always 0
for everyone — the function was a no-op.

**Fix:** `target_weekly_worked = max(int(emp.contracted_hours * 60), max_shift_min)`.
Part-time employees (15h/week = 900 min/week) are now penalised when scheduled beyond their
contracted 2 shifts/week.

**Scale:** weight=3 × overtime capped at 2 full shifts (900 min) = max 2700 pts penalty per
week vs +5/shift net reward on quiet days. Strongly negative beyond target.

**Result (August 2026):** Alicia/Kalle went from 108h (720% of target) to 57h (~86%).
Full-timers unchanged at 86–104% of their monthly target.

**Where:** `src/solver/soft_constraints.py` — `minimize_overtime()`.

---

### 38. distribute_hours_fairly fixed: per-shift count, not per-minute
**Decision:** Was using `worked_minutes` as coefficients — a 1-shift imbalance (450 min) ×
weight=5 = -2,250 penalty, overwhelming the +50 contracted_hours reward. Fixed to use shift
COUNT (coefficient=1): max penalty = 5 × len(days) ≈ 155.

**Where:** `src/solver/soft_constraints.py` — `distribute_hours_fairly()`.

---

### 39. Chat Apply: DB save returns bool, preserves is_fallback/fallback_notes
**Decision:** `_save_schedule_to_db()` now returns `bool`; `_do_apply()` checks the return
value, shows `st.error()` on failure, logs per-action changes, and preserves `is_fallback`
and `fallback_notes` when creating the new `ScheduleRead`.

**Where:** `src/ui/components/chat_panel.py`.

---

### 41. Production coverage in `add_opening_hours_coverage` — only when demand exists
**Bug:** The solver was INFEASIBLE for May 2026 even with café_needed=1 and production_needed=0
for all days, and even with 3 café-capable employees and only 14 open days (trivially solvable).

**Root cause:** `add_opening_hours_coverage` in `constraints.py` added production coverage
constraints (≥1 production employee covering each hourly slot with ≥2 covering shifts) for
EVERY open day, regardless of `demand.production_needed`. In May 2026, Aina (role=`both`)
is the only production-capable employee. The constraint forced her onto a production shift
every single open day. Combined with `add_weekly_rest` (which limits each employee to ≤6
working days per rolling 7-day open-day window), Aina was required to work every day AND
rest at least 1 day per 7 days — an irreconcilable contradiction. This made every 7-day
window of the entire month infeasible.

**Why changing Aina to `role=cafe` fixed it:** Aina no longer had production shift variables,
so the production coverage constraint had `cov_vars = []` and was skipped (via `if not cov_vars: continue`).

**Fix 1:** In `add_opening_hours_coverage`, the production coverage section is now wrapped in
`if dd.production_needed > 0:`. Production coverage is only enforced when the day actually
has production demand. On days with production_needed=0, the constraint is skipped entirely.
No change to café coverage (which is always enforced unconditionally).

**Fix 2:** In `add_daily_staffing_requirements`, added a combined-capacity cap:
`effective_prod_min = max(0, n_avail_today - effective_cafe_min)` prevents setting
café+production minimums that collectively exceed total available employees (which would
always be infeasible for a `both`-role employee who can only work one role per day).

**Verified:** With override rules café=1, prod=0: solver now returns OPTIMAL (41 working
assignments for 14 open days, 5 available employees, 0.13s). With default rules and the
genuine May 17 conflict (good ship needs café=3 + prod=1, only 3 employees available): solver
returns INFEASIBLE with an explicit human-readable diagnostic, and the fallback solver
succeeds at Step C.

**Where:** `src/solver/constraints.py` — `add_opening_hours_coverage()` and
`add_daily_staffing_requirements()`.

---

### 42. Closed days and staffing rules — debugging aids added
**Context:** The user believed closed days and edited staffing rules were being ignored
because the solver still returned INFEASIBLE after closing 17/31 May days and lowering
minimums to café=1, prod=0.

**Actual situation:**
1. Closed days WERE working — `generate_monthly_demand` correctly skipped them,
   `ScheduleGenerator.__init__` filtered them from `self._days` and `self._demand_map`.
2. Staffing rules WERE being read from DB via `load_staffing_rules_from_db()`.
3. The REAL cause was bug #41 above (production coverage).

**Aids added:**
- `ScheduleGenerator.__init__`: logs closed days list + open day count at INFO level.
- `ScheduleGenerator.build_model()`: logs distinct staffing minimums from the demand map.
- `4_schedule.py`: shows an info banner (open days count, closed days count) above the
  Generate button whenever any days are closed.
- `1_settings.py`: after saving staffing rules, immediately re-queries the DB and displays
  a "Saved values" readback block so the user can verify the save was applied.

**Where:** `src/solver/scheduler.py`, `src/ui/pages/4_schedule.py`, `src/ui/pages/1_settings.py`.

---

### 40. Grid expand/collapse toggle on Schedule page
**Decision:** "🔍 Expand" / "📅 Collapse" button toggles `st.session_state["grid_expanded"]`.
When expanded, the normal grid is hidden and a full-width grid at height=900 is rendered
BELOW both columns (using the full Streamlit container width).

**Where:** `src/ui/pages/4_schedule.py`.

## Known limitations / future work

- The solver timeout is 60 seconds per pass. For complex months (`add_two_consecutive_days_off_per_14` especially), it may return FEASIBLE (not OPTIMAL). This is acceptable.
- The fallback solver runs up to 4 passes × 60s each — worst case ~4 minutes. The spinner stays visible throughout.
- The Excel export uses a fixed two-block layout (days 1–15, 16–31). It does not adapt to months shorter than 31 days in a cosmetically perfect way.
- The LLM advisor has no memory of which changes were applied in previous sessions — it only has the current conversation history (capped at 20 turns).
- Cross-month validator checks for `_check_two_consecutive_days_off_per_14` use a sliding-by-14 window to avoid flooding violations; the first occurrence in each window is reported.
- Sunday rest: the rolling 26-week average (≤13 Sundays per 26) cannot be enforced within a single month. It is monitored by the validator as a warning only.
