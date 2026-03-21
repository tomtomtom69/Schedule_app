# Project Structure

```
geiranger-scheduler/
├── .env                          # API keys, DB credentials, LLM model config
├── .env.example                  # Template with placeholder values
├── docker-compose.yaml           # Postgres + Streamlit app
├── Dockerfile                    # Python app container
├── requirements.txt              # Python dependencies
├── docs/
│   ├── SPEC.md                   # Full application specification
│   ├── STRUCTURE.md              # This file
│   ├── PHASE1_FOUNDATION.md      # Implementation guide: foundation
│   ├── PHASE2_DEMAND.md          # Implementation guide: demand engine
│   ├── PHASE3_SOLVER.md          # Implementation guide: schedule solver
│   ├── PHASE4_UI.md              # Implementation guide: Streamlit UI
│   ├── PHASE5_LLM.md             # Implementation guide: LLM integration
│   └── PHASE6_EXPORT.md          # Implementation guide: export & polish
├── src/
│   ├── __init__.py
│   ├── config.py                 # Settings from .env (Pydantic BaseSettings)
│   ├── llm_client.py             # Single LLM wrapper — all OpenAI calls go through here
│   ├── models/
│   │   ├── __init__.py
│   │   ├── employee.py           # Employee Pydantic model + SQLAlchemy ORM
│   │   ├── cruise_ship.py        # CruiseShip + ShipLanguage models
│   │   ├── shift_template.py     # ShiftTemplate model
│   │   ├── establishment.py      # EstablishmentSettings model
│   │   ├── schedule.py           # Schedule + Assignment models
│   │   └── enums.py              # All shared enums (RoleCapability, Season, Port, etc.)
│   ├── db/
│   │   ├── __init__.py
│   │   ├── database.py           # SQLAlchemy engine, session, Base
│   │   ├── migrations.py         # Schema creation / migration logic
│   │   └── seed.py               # Default shift templates, season configs
│   ├── ingestion/
│   │   ├── __init__.py
│   │   ├── csv_parser.py         # Parse and validate CSV/XLS uploads
│   │   └── validators.py         # Cross-field validation logic
│   ├── demand/
│   │   ├── __init__.py
│   │   ├── forecaster.py         # Cruise ship → staffing demand profiles
│   │   ├── seasonal_rules.py     # Season detection + staffing tables
│   │   └── language_matcher.py   # Ship language → required speakers
│   ├── solver/
│   │   ├── __init__.py
│   │   ├── scheduler.py          # Main scheduling engine (OR-Tools or custom)
│   │   ├── constraints.py        # Hard constraint definitions
│   │   ├── soft_constraints.py   # Soft constraint weights and evaluation
│   │   ├── transport.py          # Eidsdal carpooling logic
│   │   └── validator.py          # Post-generation constraint checker
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── advisor.py            # Schedule explanation + adjustment handler
│   │   └── prompts.py            # All LLM prompt templates
│   ├── export/
│   │   ├── __init__.py
│   │   ├── excel_export.py       # Generate .xlsx matching original format
│   │   └── pdf_export.py         # Generate PDF schedule
│   └── ui/
│       ├── __init__.py
│       ├── app.py                # Streamlit entry point (multipage setup)
│       ├── pages/
│       │   ├── 1_settings.py     # Establishment settings page
│       │   ├── 2_employees.py    # Employee management page
│       │   ├── 3_cruise_ships.py # Cruise ship management page
│       │   ├── 4_schedule.py     # Schedule generator + editor page
│       │   └── 5_export.py       # Export page
│       └── components/
│           ├── schedule_grid.py  # The main schedule grid component
│           ├── ship_calendar.py  # Cruise ship calendar view
│           └── chat_panel.py     # LLM chat sidebar
└── tests/
    ├── __init__.py
    ├── test_models.py
    ├── test_demand.py
    ├── test_solver.py
    ├── test_constraints.py
    └── test_transport.py
```

## Key Design Decisions

### Single LLM Entry Point
`src/llm_client.py` is the ONLY file that imports `openai`. Every other module calls functions from `llm_client.py`. The model name comes from `config.py` which reads `LLM_MODEL` from `.env`. To change models: edit `.env`, restart container.

### Pydantic + SQLAlchemy Dual Models
Each entity has both a Pydantic model (for validation and API) and a SQLAlchemy ORM model (for DB persistence). They live in the same file for co-location. The Pydantic model validates incoming data; the ORM model maps to the database table.

### Solver Independence
The solver module has zero LLM dependency. It takes typed Python objects in and returns typed Python objects out. The LLM layer sits on top and translates between human language and solver inputs/outputs.
