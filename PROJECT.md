# Geiranger Sjokolade — Staff Scheduler

## What the application does

A monthly staff scheduler for Geiranger Sjokolade, a chocolate café in Geiranger, Norway. The business is heavily affected by cruise ship arrivals — when large ships dock, demand spikes and staffing must be planned days in advance. The app:

1. Ingests cruise ship schedule data (Norwegian Excel files with a "Språk" column for languages)
2. Calculates daily staffing demand (café workers + production workers) based on ship arrivals, season, port, and ship quality rating
3. Solves a CP-SAT constraint-satisfaction model to produce a valid monthly schedule
4. Falls back to a progressively-relaxed solver if the normal model is INFEASIBLE
5. Lets the manager review, edit, approve, and export the schedule
6. Provides an LLM chat assistant for schedule questions and adjustments

**Operating season:** 1 May – 15 October
**App URL (local):** http://localhost:8510

---

## Infrastructure

| Component | Detail |
|---|---|
| Runtime | Docker Compose: two containers |
| App container | `geiranger-scheduler-app` (Streamlit, port 8510→8501) |
| DB container | `geiranger-scheduler-db` (Postgres 16) |
| Volume (live) | `./src:/app/src` — code changes take effect immediately without rebuild |
| Volume (data) | `./uploads:/app/uploads` |
| DB persistence | Named volume `scheduler_postgres_data` |
| Python path | `PYTHONPATH=/app` inside container |

**Starting the app:**
```bash
docker compose up -d
# or to rebuild after Dockerfile/requirements changes:
docker compose up -d --build
```

**Running Python commands against live data:**
```bash
docker exec geiranger-scheduler-app python -c "..."
```

**Important:** `docs/` is NOT volume-mounted. To copy test files into the container, use `docker exec python -c "pathlib.Path(...).write_text(...)"` or `docker cp`.

---

## Technology stack

| Layer | Technology |
|---|---|
| UI | Streamlit multipage app |
| Database ORM | SQLAlchemy 2.x + Pydantic v2 |
| Database | PostgreSQL 16 |
| Solver | OR-Tools CP-SAT (Google) |
| LLM | OpenAI-compatible API (configurable model via `LLM_MODEL` env var) |
| Excel export | openpyxl |
| PDF export | ReportLab |
| Configuration | Pydantic BaseSettings from `.env` |

---

## Directory structure

```
Schedule_app/
├── src/
│   ├── config.py                  # Settings from .env (DB + LLM params)
│   ├── llm_client.py              # ONLY file that imports openai; all LLM calls go here
│   ├── models/
│   │   ├── enums.py               # All enums (RoleCapability, EmploymentType, Housing, etc.)
│   │   ├── employee.py            # EmployeeBase/Create/Read/ORM
│   │   ├── cruise_ship.py         # CruiseShipBase/Create/Read/ORM (no ShipLanguage — removed)
│   │   ├── shift_template.py      # ShiftTemplateBase/Create/Read/ORM
│   │   ├── establishment.py       # EstablishmentSettingsBase/Read/ORM
│   │   ├── schedule.py            # ScheduleORM, AssignmentORM, ScheduleRead, AssignmentRead
│   │   │                          #   ScheduleRead has: is_fallback (bool), fallback_notes (str|None JSON)
│   │   ├── daily_demand.py        # DailyDemandORM (stored demand snapshots)
│   │   ├── staffing_rule.py       # StaffingRuleORM — editable café/production minimums per season+scenario
│   │   └── closed_day.py          # ClosedDayORM — dates when shop is closed (solver skips these)
│   ├── db/
│   │   ├── database.py            # SQLAlchemy engine, Base, db_session context manager
│   │   ├── migrations.py          # create_all_tables(), reset_all_tables(), run_safe_migrations()
│   │   └── seed.py                # Seeds shift templates + default season settings
│   ├── ingestion/
│   │   ├── csv_parser.py          # parse_employees_csv(), parse_cruise_ships_csv()
│   │   └── validators.py          # validate_employee_list(), validate_cruise_schedule()
│   ├── demand/
│   │   ├── seasonal_rules.py      # STAFFING_RULES dict, get_season(), get_staffing_scenario()
│   │   ├── forecaster.py          # DailyDemand dataclass, calculate_daily_demand(), generate_monthly_demand()
│   │   ├── language_matcher.py    # get_required_languages() — reads from ship.extra_language (comma-separated)
│   │   └── db_store.py            # upsert/save/get daily demand from DB
│   ├── solver/
│   │   ├── constraints.py         # All hard constraint functions for CP-SAT
│   │   ├── soft_constraints.py    # Objective function: soft constraints + weights
│   │   ├── transport.py           # Eidsdal transport hard constraints
│   │   ├── validator.py           # Post-generation pure-Python validator → list[Violation]
│   │   ├── scheduler.py           # ScheduleGenerator class (build_model + solve), SolveInfo
│   │   ├── fallback.py            # run_fallback_solve(), FallbackResult, StaffingGap
│   │   └── __init__.py            # Public: ScheduleGenerator, validate_schedule, Violation,
│   │                              #         run_fallback_solve, FallbackResult, StaffingGap
│   ├── llm/
│   │   ├── prompts.py             # SYSTEM_PROMPT + all prompt builders
│   │   ├── advisor.py             # ScheduleAdvisor class + apply_action() function
│   │   └── __init__.py
│   ├── export/
│   │   ├── excel_export.py        # export_schedule_to_excel() — openpyxl Vaktlista format
│   │   └── pdf_export.py          # export_schedule_to_pdf() — ReportLab landscape A4
│   └── ui/
│       ├── app.py                 # Entry point: DB init + welcome page
│       ├── pages/
│       │   ├── 1_settings.py      # Season configs, shift templates, staffing rules (editable), Eidsdal settings
│       │   ├── 2_employees.py     # Employee list, edit form, CSV upload (tolerant parser with correction notes)
│       │   ├── 3_cruise_ships.py  # Ship list, calendar view, CSV upload
│       │   ├── 4_schedule.py      # Schedule generator + closed days calendar + approval flow + fallback UI
│       │   ├── 5_schedule_editor.py  # Interactive pivot-table editor
│       │   └── 6_export.py        # Download Excel/PDF, validation dashboard, heatmap
│       └── components/
│           ├── chat_panel.py      # LLM chat UI with action cards and expand mode
│           ├── schedule_grid.py   # HTML schedule grid component
│           └── ship_calendar.py   # Monthly calendar with ship badges
├── tests/
│   ├── test_models.py
│   ├── test_demand.py
│   └── test_solver.py
├── docker-compose.yaml
├── Dockerfile
├── requirements.txt
└── .env                           # Never commit — contains DB creds + OPENAI_API_KEY
```

---

## Data models

### Employee
Fields: `id` (UUID), `name`, `languages` (list[str]), `role_capability` (cafe/production/both), `employment_type` (full_time/part_time), `contracted_hours` (float, weekly), `housing` (geiranger/eidsdal), `driving_licence` (bool), `availability_start` (date), `availability_end` (date), `date_of_birth` (date|None), `preferences` (JSON dict).

### CruiseShip
Fields: `id`, `ship_name`, `date`, `arrival_time`, `departure_time`, `port` (enum), `size` (big/small), `good_ship` (bool), `extra_language` (str|None — comma-separated normalised language names, e.g. `"italian,spanish"`).

**Note:** `ShipLanguageMapping` was fully removed. Languages come directly from the Språk column in the cruise ship CSV, parsed into `extra_language`.

### ShiftTemplate
Fields: `id` (str, e.g. "1", "P1"), `role` (cafe/production), `label` (display name), `start_time`, `end_time`. Seeded by `src/db/seed.py`.

### Schedule / Assignment
`ScheduleORM` has `month`, `year`, `status` (draft/approved), `created_at`, `modified_at`, `is_fallback` (bool, default False), `fallback_notes` (text|None — JSON blob).

`AssignmentORM` has `employee_id`, `date`, `shift_id`, `is_day_off` (bool), `notes`.

The solver always creates day-off placeholder assignments for available employees who aren't working, so the grid always shows every employee for every available day.

### StaffingRule
Fields: `id` (int, serial PK), `season` (str: "low"/"mid"/"peak"), `scenario` (str: e.g. "with_cruise"), `cafe_needed` (int), `production_needed` (int). Unique constraint on `(season, scenario)`. Seeded from `STAFFING_RULES` on first run if table is empty. Editable in Settings → Tab 3.

### ClosedDay
Fields: `id` (UUID PK), `date` (date, UNIQUE), `year` (int), `reason` (text, nullable). One row per closed date. The manager creates these via the calendar UI on the Schedule page. Closed days are skipped by `generate_monthly_demand()` and shown as grey 🔒 columns in the schedule grid and Excel export.

`fallback_notes` JSON schema:
```json
{
  "steps": ["C"],
  "notes": ["Language matching: already soft...", "Minimum staffing: reduced by 1..."],
  "gaps": [{"date": "2026-05-04", "role": "cafe", "required": 4, "scheduled": 3}]
}
```

---

## Demand engine

**`generate_monthly_demand(year, month, ships, closed_days=None, rules=None) → list[DailyDemand]`**

For each calendar day in the month that falls within the operating season:
1. Skip if date is in `closed_days` (no demand, no assignments)
2. Determine season (low/mid/peak) from date
3. Look up ships arriving that day
4. Calculate `effective_ship_impact` (Geiranger=1.0 per ship, Hellesylt=0.5)
5. Load staffing rules: if `rules` not provided, calls `load_staffing_rules_from_db()` which reads
   from the `staffing_rules` DB table (falls back to hardcoded `STAFFING_RULES` if table empty)
6. Apply rules to get `cafe_needed` and `production_needed`
7. Extract required languages from `ship.extra_language` (split on comma, filter "english")

**Staffing scenarios (simplified):** no_cruise, with_cruise, with_good_ship — each has different café/production counts per season.

**Staffing rules DB:** The `staffing_rules` table is the live source of truth. It is seeded from
`STAFFING_RULES` on first run. Editable in Settings → Tab 3. `load_staffing_rules_from_db()`
uses lazy import to avoid circular imports between `seasonal_rules.py` and `database.py`.

---

## Solver

**`ScheduleGenerator(employees, demand, shifts, settings)`**

1. `build_model(disable_both_preference=False)`: creates BoolVar for each compatible (employee, day, shift) triple, adds hard constraints, adds soft constraints + objective
2. `solve()`: runs CP-SAT with 60s timeout; returns `ScheduleRead` or None if infeasible/timeout

**Hard constraints (in `constraints.py`):**
- `add_one_shift_per_day` — at most one shift per employee per day
- `add_daily_staffing_requirements` — minimum café/production headcount per day. **Capped to available employees**: `effective_min = min(demand.cafe_needed, actual_cafe_emps_today)` to prevent INFEASIBLE when headcount is below peak demand
- `add_weekly_hour_limits` — 37.5h/week adults, 40h age 15-18, 35h under 15 (worked minutes, not raw duration)
- `add_daily_rest` — 11h rest between end of one shift and start of the next day's shift
- `add_weekly_rest` — rolling 7-day window: ≤6 working days (= 35h continuous rest)
- `add_max_days_per_calendar_week` — ≤6 working days per Mon–Sun ISO week (explicit safety cap)
- `add_max_consecutive_working_days` — no employee works >6 consecutive calendar days (rolling gap-aware 7-day windows)
- `add_two_consecutive_days_off_per_14` — every rolling 14-day window must contain ≥1 pair of adjacent days off. Uses `pair_off_bv` BoolVars per (employee, day0) indicating both day0 and day0+1 are off
- `add_cross_month_consecutive_constraint` — prevents >6-day runs spanning the M-1/M boundary. Loads M-1's last 6 working days per employee from DB; for each employee with carry_in K: adds `sum(shift_vars on days 1…{7-K} of M) ≤ 6-K`
- `add_age_based_constraints` — under-15: only shift 6 allowed; 15-18: weekly cap only
- `add_sunday_rest_constraints` — in any 4 consecutive Sundays/holidays, at most 3 worked
- `add_max_staffing_caps` — upper cap: ≤5 café normally, ≤6 on good-ship days with ≥2 ships, ≤4 production
- `add_opening_hours_coverage` — **NEVER RELAX**: ≥1 café employee covering each 1-hour slot within opening/closing times; ≥1 production employee for slots covered by ≥2 production shifts
- `add_eidsdal_transport_constraints` — in `transport.py`; driver required when any Eidsdal employee works

**Opening hours coverage detail:** For peak season (08:30–20:15): slot 08:30–09:30 is only covered by shift 1 → forces ≥1 on early shift every day. Slot 19:30–20:15 only by shift 5 → forces ≥1 on late shift every day. The `if not cov_vars: continue` guard only skips when literally no employee has a variable for that slot (outside availability), not when employees are on rest days — rest is handled by the solver itself choosing when to schedule rest days.

**Soft constraints (objective weights in `soft_constraints.py`):**
- `language_coverage`: 100 — reward having a speaker on café when a ship language is required
- `good_ship_day`: 60, `cruise_day`: 35, `no_cruise_day`: 15 — per-assignment rewards by demand level
- `part_time_penalty`: 10 — deducted from day reward for part-time assignments
- `both_on_production`: 20 — reward "both" employee on production; −20 on café (skipped when `disable_both_preference=True` in fallback Step D+)
- `eidsdal_grouping`: 8 — reward Eidsdal workers on the same shift
- `employee_preferences`: 5, `fair_distribution`: 5, `minimize_overtime`: 3
- `shift_variety`: −2 — penalty for same shift on two consecutive days
- `over_coverage_t1`: −3, `over_coverage_t2`: −5 — tiered penalty for each extra above daily min (spreading 2 extras across 2 days costs −6, concentrating on 1 day costs −8)

**`SolveInfo`** captures: `status_name`, `num_variables`, `num_days`, `num_employees_available`, `num_working_assignments`, `wall_time`, `objective_value`, `diagnostics` (list[str]), `warnings` (list[str]).

Properties: `is_success` (OPTIMAL/FEASIBLE + working > 0), `is_empty_solution` (OPTIMAL/FEASIBLE + working == 0).

**Pre-flight checks** (run before solving): employees with no availability overlap, language gaps, staffing capacity shortfalls vs peak demand. These populate `solve_info.warnings`.

---

## Fallback solver (`src/solver/fallback.py`)

When the normal solver returns INFEASIBLE, the UI shows diagnostics and a **"⚡ Generate Best-Effort Schedule"** button. When clicked, `run_fallback_solve()` tries 4 progressively looser models, stopping at the first feasible one.

**Steps (each is a complete new solve attempt):**
- **Step A** — Language: already soft in the main solver; noted in report, no re-solve
- **Step B** — Reduce café/production minimums by 1 on **no-cruise days** only
- **Step C** — Reduce café/production minimums by 1 on **all days**
- **Step D** — Same demand as Step C + `disable_both_preference=True` (flex employees assigned freely)
- **Step E** — Absolute floor: café minimum = 1 (where demand existed), production minimum = 0
- **Skeleton** — café ≥ 1 per open day, production = 0, drops 4 complex constraints (opening hours
  coverage, 14-day paired rest, Sunday rest, staffing caps), uses `model.Minimize(sum_vars)` objective

**Never relaxed in any fallback step:**
- Opening hours coverage (≥1 in café per slot at all times)
- Max 6 consecutive working days
- Daily rest (11h) and weekly rest (35h / rolling 7-day)
- Age-based shift restrictions
- Max staffing caps (5–6 café, 4 production)
- Eidsdal driver requirement

**`FallbackResult`** dataclass: `schedule`, `steps_applied`, `relaxation_notes`, `staffing_gaps` (list[StaffingGap]).

**`StaffingGap`** dataclass: `date`, `role`, `required` (original demand), `scheduled` (what fallback assigned).

`FallbackResult.notes_json()` serialises to JSON for storage in `ScheduleORM.fallback_notes`.

`staffing_gaps_from_json()`, `relaxation_notes_from_json()`, and `is_skeleton_from_json()` reconstruct
from stored JSON (used when loading from DB after a session restart).

`FallbackResult.is_skeleton` property: `True` when `steps_applied == ["SKELETON"]`.

**UI integration (`4_schedule.py`):**
- After INFEASIBLE: diagnostics expander stays visible + fallback button appears
- `_inf_info`, `_inf_demand`, `_inf_year`, `_inf_month` stored in session state
- After fallback success: `ScheduleRead` has `is_fallback=True`, `fallback_notes=JSON`
- **Normal fallback:** yellow bordered banner + collapsible staffing gaps expander
- **Skeleton fallback:** red `st.error` banner with 🚨 warning + auto-expanded gaps expander
- Same banners shown in `5_schedule_editor.py`
- Excel export (row 3): yellow warning note when `is_fallback`

---

## Cross-month edit warnings

Two helpers in both `4_schedule.py` and `5_schedule_editor.py`:

- `_next_month_name_if_exists(year, month)` — queries DB; returns "June 2026" if M+1 exists
- `_stale_prev_month_warning(schedule)` — compares `prev_orm.modified_at > schedule.created_at`; returns warning string if true

Warnings shown:
1. **Inline after Save Draft / Approve** — if M+1 exists, warn that M+1 may be stale
2. **Always-on banner** — if M-1 was modified after M was generated, warn to regenerate M

---

## LLM integration

**`ScheduleAdvisor`** stored in `st.session_state.advisor`. Resets when schedule ID changes.

`advisor.chat(user_message)` → `{"text": str, "actions": list[dict]}`

Action dict: `{"action": "assign"|"unassign"|"day_off", "employee": str, "date": date, "shift": str|None, "reason": str}`

`apply_action(action, schedule, employees, demand, shift_templates)` → `(new_schedule, warnings)`

The chat panel (`chat_panel.py`) strips JSON from displayed text before showing it, renders action proposals as clickable cards, and saves to DB after each apply.

---

## DB migrations

All schema changes are applied via `run_safe_migrations()` in `src/db/migrations.py`, using `ALTER TABLE … ADD COLUMN IF NOT EXISTS` (idempotent, safe to run on startup).

Current migrations:
- `employees.date_of_birth DATE`
- `establishment_settings.max_cafe_per_day INTEGER NOT NULL DEFAULT 5`
- `establishment_settings.max_prod_per_day INTEGER NOT NULL DEFAULT 4`
- `schedules.is_fallback BOOLEAN NOT NULL DEFAULT FALSE`
- `schedules.fallback_notes TEXT`
- `CREATE TABLE IF NOT EXISTS staffing_rules (id SERIAL PK, season TEXT, scenario TEXT, cafe_needed INT, production_needed INT, UNIQUE(season, scenario))`
- `CREATE TABLE IF NOT EXISTS closed_days (id UUID PK, date DATE UNIQUE, year INT, reason TEXT NULL)`

To reset the entire DB (destroys all data):
```bash
docker exec geiranger-scheduler-app python -c "
from src.db.migrations import reset_all_tables
from src.db.seed import seed_defaults
reset_all_tables()
seed_defaults()
print('Done')
"
```

---

## Environment variables (`.env`)

```
POSTGRES_USER=...
POSTGRES_PASSWORD=...
POSTGRES_DB=scheduler
POSTGRES_HOST=db
POSTGRES_PORT=5432
OPENAI_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini
LLM_TEMPERATURE=0.2
LLM_MAX_TOKENS=4096
```

---

## Page flow (user journey)

1. **Settings** — verify season dates and shift templates are seeded correctly
2. **Employees** — upload employee CSV (one-time; re-upload to update)
3. **Cruise Ships** — upload ship schedule CSV (from the port authority / Excel export)
4. **Schedule** — select month, optionally mark closed days (🔒 calendar UI above Generate), click Generate; if FEASIBLE → review and Save/Approve; if INFEASIBLE → view diagnostics and optionally generate best-effort (Steps B→C→D→E→Skeleton)
5. **Schedule Editor** — fine-tune individual assignments in the pivot grid
6. **Export** — download Excel (Vaktlista format) or PDF after approval
