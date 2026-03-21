# Phase 1: Foundation — Implementation Guide

**Goal:** Docker environment running, database initialized, Pydantic models defined, CSV upload working.

**Instructions for Claude Code:** Implement all files described below without requesting approval for each step. Only ask for approval before running system commands (docker build, docker-compose up, pip install, etc.). Write all code files in sequence, then present the complete set.

---

## 1.1 Environment Configuration

### `.env.example`

```env
# Database
POSTGRES_USER=scheduler
POSTGRES_PASSWORD=scheduler_secret
POSTGRES_DB=geiranger_scheduler
POSTGRES_HOST=db
POSTGRES_PORT=5432

# LLM Configuration (generic — change model here, nowhere else)
OPENAI_API_KEY=sk-your-key-here
LLM_MODEL=gpt-4o-mini
LLM_TEMPERATURE=0.2
LLM_MAX_TOKENS=4096

# App
STREAMLIT_PORT=8510
```

### `src/config.py`

Use Pydantic `BaseSettings` to load from `.env`:

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Database
    postgres_user: str
    postgres_password: str
    postgres_db: str
    postgres_host: str = "db"
    postgres_port: int = 5432

    # LLM — generic, no hardcoded model names
    openai_api_key: str
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.2
    llm_max_tokens: int = 4096

    @property
    def database_url(self) -> str:
        return f"postgresql://{self.postgres_user}:{self.postgres_password}@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"

    class Config:
        env_file = ".env"

settings = Settings()
```

---

## 1.2 Docker Setup

### `Dockerfile`

- Base: `python:3.12-slim`
- Install requirements.txt
- Copy src/ into container
- Expose port 8501 (internal)
- CMD: `streamlit run src/ui/app.py --server.port=8501 --server.address=0.0.0.0`

### `docker-compose.yaml`

Two services:

**db:**
- Image: `postgres:16`
- Container name: `geiranger-scheduler-db`
- Environment from `.env` (POSTGRES_USER, PASSWORD, DB)
- Volume: `scheduler_postgres_data:/var/lib/postgresql/data`
- Port: do NOT expose externally (internal network only)
- Healthcheck: `pg_isready`

**app:**
- Build from Dockerfile
- Container name: `geiranger-scheduler-app`
- Ports: `8510:8501`
- Depends on: db (with healthcheck condition)
- Environment from `.env`
- Volume mount for uploads: `./uploads:/app/uploads`

**Volumes:**
```yaml
volumes:
  scheduler_postgres_data:
```

---

## 1.3 Database Setup

### `src/db/database.py`

- SQLAlchemy engine from `settings.database_url`
- SessionLocal factory
- Base declarative base
- `get_db()` dependency/context manager

### `src/db/migrations.py`

- `create_all_tables()` — creates tables from ORM models
- Called on app startup
- Idempotent (safe to run multiple times)

### `src/db/seed.py`

- `seed_shift_templates()` — inserts default shift templates (1–6, P1–P5) if table is empty
- `seed_season_settings()` — inserts default season configurations if table is empty
- Called after migrations on startup

---

## 1.4 Enums

### `src/models/enums.py`

```python
from enum import Enum

class RoleCapability(str, Enum):
    cafe = "cafe"
    production = "production"
    both = "both"

class EmploymentType(str, Enum):
    full_time = "full_time"
    part_time = "part_time"

class Housing(str, Enum):
    geiranger = "geiranger"
    eidsdal = "eidsdal"

class Season(str, Enum):
    low = "low"
    mid = "mid"
    peak = "peak"

class Port(str, Enum):
    geiranger_4B_SW = "geiranger_4B_SW"
    geiranger_3S = "geiranger_3S"
    geiranger_2 = "geiranger_2"
    hellesylt = "hellesylt"

class ShipSize(str, Enum):
    big = "big"
    small = "small"

class ShiftRole(str, Enum):
    cafe = "cafe"
    production = "production"

class ScheduleStatus(str, Enum):
    draft = "draft"
    approved = "approved"
```

---

## 1.5 Pydantic + SQLAlchemy Models

Each model file contains BOTH the Pydantic schema and the SQLAlchemy ORM class.

### `src/models/employee.py`

**Pydantic schema** (`EmployeeCreate`, `EmployeeRead`):
- All fields from SPEC.md Section 4.1
- Validators: `languages` must include "english", `availability_start` < `availability_end`, dates must fall within May 1 – Oct 15

**SQLAlchemy ORM** (`EmployeeORM`):
- Table: `employees`
- UUID primary key (server-generated)
- `languages` stored as ARRAY(String) or JSON
- `preferences` stored as JSON

### `src/models/cruise_ship.py`

**Pydantic schemas:** `CruiseShipCreate`, `CruiseShipRead`, `ShipLanguageCreate`, `ShipLanguageRead`

**SQLAlchemy ORM:** `CruiseShipORM`, `ShipLanguageORM`
- Table: `cruise_ships`, `ship_languages`
- `date` must fall within season (May 1 – Oct 15)

### `src/models/shift_template.py`

**Pydantic:** `ShiftTemplateCreate`, `ShiftTemplateRead`

**SQLAlchemy:** `ShiftTemplateORM`
- Table: `shift_templates`
- String primary key (e.g., "1", "P2")
- Validate: `start_time` < `end_time`, shift duration ≤ 10h

### `src/models/establishment.py`

**Pydantic:** `EstablishmentSettingsCreate`, `EstablishmentSettingsRead`

**SQLAlchemy:** `EstablishmentSettingsORM`
- Table: `establishment_settings`
- One row per season period
- Validate: `opening_time` < `closing_time`

### `src/models/schedule.py`

**Pydantic:** `ScheduleCreate`, `ScheduleRead`, `AssignmentCreate`, `AssignmentRead`

**SQLAlchemy:** `ScheduleORM`, `AssignmentORM`
- Tables: `schedules`, `assignments`
- `assignments` has FK to `schedules` and `employees`
- `month` must be 5–10

---

## 1.6 LLM Client Wrapper

### `src/llm_client.py`

```python
from openai import OpenAI
from src.config import settings

_client = None

def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=settings.openai_api_key)
    return _client

def chat_completion(
    messages: list[dict],
    temperature: float | None = None,
    max_tokens: int | None = None,
    response_format: dict | None = None,
) -> str:
    """Single entry point for all LLM calls. No other file imports openai."""
    client = get_client()
    response = client.chat.completions.create(
        model=settings.llm_model,
        messages=messages,
        temperature=temperature or settings.llm_temperature,
        max_tokens=max_tokens or settings.llm_max_tokens,
        response_format=response_format,
    )
    return response.choices[0].message.content

def chat_completion_json(messages: list[dict], **kwargs) -> str:
    """LLM call that forces JSON output."""
    return chat_completion(
        messages=messages,
        response_format={"type": "json_object"},
        **kwargs,
    )
```

**CRITICAL:** This is the ONLY file in the project that imports `openai`. Every other module uses `from src.llm_client import chat_completion`.

---

## 1.7 CSV Ingestion

### `src/ingestion/csv_parser.py`

Functions:
- `parse_employees_csv(file) -> list[EmployeeCreate]` — reads CSV/XLS, validates each row with Pydantic, returns list or raises with row-level errors
- `parse_cruise_ships_csv(file) -> list[CruiseShipCreate]` — same pattern
- `parse_ship_languages_csv(file) -> list[ShipLanguageCreate]` — same pattern

Use `pandas.read_csv()` or `pandas.read_excel()` depending on file extension. Accept both `.csv` and `.xlsx`.

### `src/ingestion/validators.py`

Cross-record validation:
- `validate_employee_list(employees)` — check for duplicate names, at least one driver among Eidsdal employees
- `validate_cruise_schedule(ships)` — check for date range within season
- `validate_language_coverage(employees, ships)` — warn if any ship language has zero matching employees

---

## 1.8 Streamlit App Entry Point

### `src/ui/app.py`

Minimal multipage setup:

```python
import streamlit as st
from src.db.migrations import create_all_tables
from src.db.seed import seed_defaults

# Run on startup
create_all_tables()
seed_defaults()

st.set_page_config(
    page_title="Geiranger Sjokolade Scheduler",
    page_icon="🍫",
    layout="wide",
)

st.title("🍫 Geiranger Sjokolade — Staff Scheduler")
st.write("Use the sidebar to navigate.")
```

Pages are auto-discovered from `src/ui/pages/` directory.

---

## 1.9 Requirements

### `requirements.txt`

```
streamlit>=1.30
sqlalchemy>=2.0
psycopg2-binary
pydantic>=2.0
pydantic-settings
pandas
openpyxl
openai
ortools
```

---

## 1.10 Acceptance Criteria

Phase 1 is complete when:
- [ ] `docker-compose up` starts both containers cleanly
- [ ] Streamlit is accessible at `http://localhost:8510`
- [ ] Database tables are created on first startup
- [ ] Default shift templates are seeded
- [ ] A test CSV of employees can be uploaded and stored in the DB
- [ ] A test CSV of cruise ships can be uploaded and stored in the DB
- [ ] Changing `LLM_MODEL` in `.env` changes which model `llm_client.py` uses (verify with a print/log)
