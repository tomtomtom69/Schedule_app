# Geiranger Sjokolade — Staff Scheduler

## What the application does

A monthly staff scheduler for Geiranger Sjokolade, a chocolate café in Geiranger, Norway. The business is heavily affected by cruise ship arrivals — when large ships dock, demand spikes and staffing must be planned days in advance. The app:

1. Ingests cruise ship schedule data (Norwegian Excel files with a "Språk" column for languages)
2. Calculates daily staffing demand (café workers + production workers) based on ship arrivals, season, port, and ship quality rating
3. Solves a CP-SAT constraint-satisfaction model to produce a valid monthly schedule
4. Lets the manager review, edit, approve, and export the schedule
5. Provides an LLM chat assistant for schedule questions and adjustments

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
# or to rebuild:
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
│   │   └── daily_demand.py        # DailyDemandORM (stored demand snapshots)
│   ├── db/
│   │   ├── database.py            # SQLAlchemy engine, Base, db_session context manager
│   │   ├── migrations.py          # create_all_tables(), reset_all_tables()
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
│   │   └── __init__.py            # Public: ScheduleGenerator, validate_schedule, Violation
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
│       │   ├── 1_settings.py      # Season configs, shift templates, Eidsdal settings
│       │   ├── 2_employees.py     # Employee list, edit form, CSV upload
│       │   ├── 3_cruise_ships.py  # Ship list, calendar view, CSV upload
│       │   ├── 4_schedule.py      # Schedule generator + approval flow
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
Fields: `id` (UUID), `name`, `languages` (list[str]), `role_capability` (cafe/production/both), `employment_type` (full_time/part_time), `contracted_hours` (float, weekly), `housing` (geiranger/eidsdal), `driving_licence` (bool), `availability_start` (date), `availability_end` (date), `preferences` (JSON dict).

### CruiseShip
Fields: `id`, `ship_name`, `date`, `arrival_time`, `departure_time`, `port` (enum), `size` (big/small), `good_ship` (bool), `extra_language` (str|None — comma-separated normalised language names, e.g. `"italian,spanish"`).

**Note:** `ShipLanguageMapping` was fully removed. Languages come directly from the Språk column in the cruise ship CSV, parsed into `extra_language`.

### ShiftTemplate
Fields: `id` (str, e.g. "1", "P1"), `role` (cafe/production), `label` (display name), `start_time`, `end_time`. Seeded by `src/db/seed.py`.

### Schedule / Assignment
`ScheduleORM` has `month`, `year`, `status` (draft/approved), `created_at`, `modified_at`.
`AssignmentORM` has `employee_id`, `date`, `shift_id`, `is_day_off` (bool), `notes`.
The solver always creates day-off placeholder assignments for available employees who aren't working, so the grid always shows every employee for every available day.

---

## Demand engine

**`generate_monthly_demand(year, month, ships) → list[DailyDemand]`**

For each calendar day in the month that falls within the operating season:
1. Determine season (low/mid/peak) from date
2. Look up ships arriving that day
3. Calculate `effective_ship_impact` (Geiranger=1.0 per ship, Hellesylt=0.5)
4. Apply `STAFFING_RULES[season][scenario]` to get `cafe_needed` and `production_needed`
5. Extract required languages from `ship.extra_language` (split on comma, filter "english")

**Staffing scenarios (simplified):** no_cruise, with_cruise, with_good_ship — each has different café/production counts per season.

---

## Solver

**`ScheduleGenerator(employees, demand, shifts, settings)`**

1. `build_model()`: creates BoolVar for each compatible (employee, day, shift) triple, adds hard constraints, adds soft constraints + objective
2. `solve()`: runs CP-SAT with 60s timeout; returns `ScheduleRead` or None if infeasible

**Hard constraints:** one shift/day per employee, daily staffing minimums, 48h weekly max, 11h daily rest, 35h weekly rest, role capability matching, availability dates, Eidsdal transport (max 10 per trip, driver required), **opening hours coverage** (≥1 café employee on a covering shift for every 1-hour slot within `opening_time`–`closing_time`; production same for slots with ≥2 covering shifts).

**Opening hours coverage detail:** `add_opening_hours_coverage()` in `constraints.py`. Settings auto-loaded from `EstablishmentSettings` DB table by `ScheduleGenerator._load_settings()`. For peak season (08:30–20:15): slot 08:30–09:30 is only covered by shift 1, slot 19:30–20:15 only by shift 5 — these single-shift slots force the solver to assign at least one person to each end of the day. Coverage is always ≥1 (no harbor-time elevation — total headcount is handled by the staffing constraint).

**Soft constraints (objective weights):** language coverage (100), full-time preference (±10), Eidsdal grouping (8), employee preferences (5), minimize overtime (3), fair hours distribution (5), **shift variety** (−2 penalty for same shift on consecutive days to encourage natural rotation).

**`SolveInfo`** dataclass captures: `status_name`, `num_variables`, `num_days`, `num_employees_available`, `num_working_assignments`, `wall_time`, `objective_value`, `diagnostics` (list[str]), `warnings` (list[str]).

**Pre-flight checks** (run before solving): employees with no availability overlap this month, language gaps, staffing capacity shortfalls. These populate `solve_info.warnings`.

---

## LLM integration

**`ScheduleAdvisor`** stored in `st.session_state.advisor`. Resets when schedule ID changes.

`advisor.chat(user_message)` → `{"text": str, "actions": list[dict]}`

Action dict: `{"action": "assign"|"unassign"|"day_off", "employee": str, "date": date, "shift": str|None, "reason": str}`

`apply_action(action, schedule, employees, demand, shift_templates)` → `(new_schedule, warnings)`

The chat panel (`chat_panel.py`) strips JSON from displayed text before showing it, renders action proposals as clickable cards, and saves to DB after each apply.

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
4. **Schedule** — select month, click Generate, review solver diagnostics, Save Draft or Approve & Finalize
5. **Schedule Editor** — fine-tune individual assignments in the pivot grid
6. **Export** — download Excel (Vaktlista format) or PDF after approval
