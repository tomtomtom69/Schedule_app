# Geiranger Sjokolade — Scheduling Application Specification

**Version:** 1.1 — March 2026
**Status:** Requirements complete, ready for implementation

---

## 1. Overview

A local scheduling application for **Geiranger Sjokolade**, a business that combines chocolate manufacturing and a café/shop in Geiranger, Norway. The business is heavily influenced by cruise ship arrivals, which dramatically affect staffing needs. The application generates monthly staff schedules that respect Norwegian labor law, employee capabilities, transport logistics, and cruise ship demand.

**Operating season:** 1 May – 15 October (closed outside this period).

---

## 2. Tech Stack

| Component          | Technology                        |
|--------------------|-----------------------------------|
| Containerization   | Docker + docker-compose           |
| UI                 | Streamlit (port **8510**)         |
| Backend            | Python (pure, no LangChain)       |
| Data validation    | Pydantic                          |
| Database           | PostgreSQL                        |
| LLM                | OpenAI-compatible API (model configurable via `.env`) |
| Web scraping       | Playwright (future: cruise data)  |
| Config             | `.env` (API keys, DB credentials, LLM model) |

**No login required.** Runs locally in Docker with persistent DB storage.

### Docker Configuration

| Setting            | Value                             |
|--------------------|-----------------------------------|
| Streamlit port     | **8510** (host) → 8501 (container)|
| Postgres volume    | **scheduler_postgres_data**       |
| Container prefix   | **geiranger-scheduler-**          |

### LLM Configuration (Generic)

All LLM calls use environment variables — no model names hardcoded anywhere:

```env
OPENAI_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini          # Change to gpt-4o or any compatible model
LLM_TEMPERATURE=0.2
LLM_MAX_TOKENS=4096
```

A single `llm_client.py` module wraps all OpenAI calls. Every other module imports from it. To switch models, change `.env` only.

---

## 3. Architecture — Four Layers

### Layer 1: Data Ingestion
- Upload and parse CSV/XLS files (employees, cruise ships, ship languages)
- Pydantic validation on all incoming data
- Store validated data in PostgreSQL

### Layer 2: Demand Forecasting (Pure Python)
- Translate cruise ship arrivals into staffing demand profiles per day
- Apply seasonal staffing rules (see Section 8)
- Determine language requirements per day
- No LLM involvement — deterministic math

### Layer 3: Schedule Engine (Constraint Solver)
- Pure Python constraint-satisfaction optimizer (OR-Tools or custom)
- Hard constraints (labor law) are unbreakable
- Soft constraints (preferences, language, carpooling) are weighted objectives
- Generates a full month schedule in one pass

### Layer 4: LLM Advisory Layer (OpenAI)
- Explains proposed schedules in plain language
- Handles natural-language adjustment requests ("swap Maria and Erik on Thursday")
- Reasons about edge cases and trade-offs
- Conversational interface for iterative refinement

---

## 4. Data Models

### 4.1 Employee

| Field              | Type                  | Description                                      |
|--------------------|-----------------------|--------------------------------------------------|
| id                 | UUID                  | Unique identifier                                |
| name               | str                   | Full name                                        |
| languages          | list[str]             | Languages spoken (all speak English by default). Extras: Spanish, German, Italian |
| role_capability    | enum                  | `cafe`, `production`, `both`                     |
| employment_type    | enum                  | `full_time`, `part_time`                         |
| contracted_hours   | float                 | Contracted hours per week                        |
| housing            | enum                  | `geiranger`, `eidsdal`                           |
| driving_licence    | bool                  | Has driving licence (relevant for Eidsdal transport) |
| availability_start | date                  | First available working date (seasonal)          |
| availability_end   | date                  | Last available working date (seasonal)           |
| preferences        | dict (optional)       | Personal restrictions/preferences (e.g., "no Mondays", "max 5 days/week") |

**Notes:**
- Employees with `role_capability = both` primarily do production, but flex to café in off-peak.
- Part-time employees are only used when full-timers cannot cover demand.

### 4.2 Cruise Ship Arrival

| Field              | Type                  | Description                                      |
|--------------------|-----------------------|--------------------------------------------------|
| id                 | UUID                  | Unique identifier                                |
| ship_name          | str                   | Name of the cruise ship                          |
| date               | date                  | Date of arrival                                  |
| arrival_time       | time                  | Time ship arrives in port                        |
| departure_time     | time                  | Time ship departs                                |
| port               | enum                  | `geiranger_4B_SW`, `geiranger_3S`, `geiranger_2`, `hellesylt` |
| size               | enum                  | `big`, `small`                                   |
| good_ship          | bool                  | Flag: historically good for business             |
| extra_language     | str (optional)        | Primary non-English language (e.g., "spanish")   |

**Notes:**
- Ship-to-language mapping uploaded as a separate CSV.
- Multiple ships can arrive on the same day.
- Hellesylt ships trigger approximately half the staffing impact of Geiranger ships.

### 4.3 Ship Language Mapping (separate upload)

| Field              | Type                  | Description                                      |
|--------------------|-----------------------|--------------------------------------------------|
| ship_name          | str                   | Name of the cruise ship                          |
| primary_language   | str                   | Main guest language (e.g., "german", "spanish")  |

### 4.4 Shift Template

| Field              | Type                  | Description                                      |
|--------------------|-----------------------|--------------------------------------------------|
| id                 | str                   | Shift identifier (1–6 for café, P1–P5 for production) |
| role               | enum                  | `cafe`, `production`                             |
| label              | str                   | Display label (e.g., "VAKT SHOP 1")              |
| start_time         | time                  | Shift start                                      |
| end_time           | time                  | Shift end                                        |

**Current shift templates (from spreadsheet):**

| ID  | Role       | Start | End   |
|-----|------------|-------|-------|
| 1   | café       | 08:00 | 16:00 |
| 2   | café       | 09:30 | 17:30 |
| 3   | café       | 11:00 | 19:00 |
| 4   | café       | 12:00 | 20:00 |
| 5   | café       | 13:00 | 21:00 |
| 6   | café       | 10:00 | 17:00 |
| P1  | production | 08:00 | 16:00 |
| P2  | production | 09:30 | 17:30 |
| P3  | production | 11:00 | 19:00 |
| P4  | production | 12:00 | 20:00 |
| P5  | production | 13:00 | 21:00 |

**Special codes:** Ø = opening duties, K = packing, C = cleaning (3h end of shift), i = introduction/training.

Shift templates are configurable — the business owner can modify start/end times per season.

### 4.5 Establishment Settings

| Field              | Type                  | Description                                      |
|--------------------|-----------------------|--------------------------------------------------|
| season             | enum                  | `low`, `mid`, `peak`                             |
| date_range_start   | date                  | Season period start                              |
| date_range_end     | date                  | Season period end                                |
| opening_time       | time                  | Café opening time                                |
| closing_time       | time                  | Café closing time                                |
| production_start   | time                  | Production starts (= first shift start time)     |

**Season periods:**
- **Low season:** May, September, 1–15 October
- **Mid season:** 1–15 June
- **Peak season:** 16 June – 31 August

**Opening hours vary by season (configurable):**
- May: ~10:00–17:00
- Early June: transitional
- Peak (late June–Aug): 08:30–20:15 (changes late August to 10:00–19:00)
- September: 10:00–18:00
- October (1–15): 10:00–17:00

**Closed:** 16 October – 30 April

### 4.6 Schedule (output)

| Field              | Type                  | Description                                      |
|--------------------|-----------------------|--------------------------------------------------|
| id                 | UUID                  | Unique identifier                                |
| month              | int                   | Month number (5–10 only)                         |
| year               | int                   | Year                                             |
| assignments        | list[Assignment]      | All shift assignments for the month              |
| status             | enum                  | `draft`, `approved`                              |
| created_at         | datetime              | When generated                                   |
| modified_at        | datetime              | Last manual edit                                 |

### 4.7 Assignment (single cell in the grid)

| Field              | Type                  | Description                                      |
|--------------------|-----------------------|--------------------------------------------------|
| employee_id        | UUID                  | Reference to employee                            |
| date               | date                  | The day                                          |
| shift_id           | str                   | Which shift (e.g., "2", "P5", "off")             |
| is_day_off         | bool                  | Explicitly marked day off                        |
| notes              | str (optional)        | E.g., "intro", "cleaning", "chocotasting"        |

---

## 5. Constraint Rules

### 5.1 Hard Constraints (Norwegian Labor Law — Arbeidsmiljøloven)

| Rule                            | Value                              |
|---------------------------------|------------------------------------|
| Normal working day              | 7.5 hours                          |
| Max normal shift                | 9 hours                            |
| Absolute max shift              | 10 hours                           |
| Normal weekly max               | 40 hours                           |
| Absolute weekly max             | 48 hours                           |
| Averaging (genomsnittsmetoden)  | Can go to 50h/week if average stays ≤40h over reference period |
| Min daily rest                  | 11 hours between shifts            |
| Min weekly rest                 | 35 hours continuous                |

### 5.2 Hard Constraints (Business-Specific)

| Rule                            | Value                              |
|---------------------------------|------------------------------------|
| Language matching               | If a ship with extra_language is in port, at least 1 speaker of that language must be on a café shift during that time window |
| Eidsdal transport               | Max 2 cars, max 5 people per car (= max 10 Eidsdal workers per shift window). Each car must have ≥1 driver (driving_licence = true) |
| Role capability                 | Employee can only be assigned to shifts matching their role_capability |

### 5.3 Soft Constraints (Weighted Preferences)

| Rule                            | Priority | Description                    |
|---------------------------------|----------|--------------------------------|
| Full-time preference            | High     | Fill with full-timers first; part-time only for gaps |
| Eidsdal carpooling grouping     | High     | Group Eidsdal employees into overlapping shifts to minimize car trips |
| Employee preferences            | Medium   | Respect personal restrictions (preferred days off, max days in a row, etc.) |
| Fair distribution               | Medium   | Spread hours roughly evenly among same-type employees |
| Minimize overtime               | Low      | Prefer normal hours over averaging method |

---

## 6. Eidsdal Transport Rules (Detail)

- **2 cars**, each seats **5 people**
- Each car requires **at least 1 person with a driving licence**
- Employees housed in Eidsdal must travel together — schedule should group their shifts so start/end times overlap enough for shared transport
- Employees housed in Geiranger walk; no transport constraint
- Max Eidsdal employees working simultaneously: **10** (2 cars × 5 seats)

---

## 7. Cruise Ship Impact Rules

### Port Impact

| Port                          | Staffing Impact                      |
|-------------------------------|--------------------------------------|
| Geiranger (4B/SW, 3S, 2)     | Full impact (see seasonal rules)     |
| Hellesylt cruisekai           | Half impact (tourists come by bus)   |

### Multiple Ships
When multiple ships are in port on the same day, staffing impact stacks (but practically capped by available employees and shift slots).

---

## 8. Seasonal Staffing Rules

### Low Season: May, September, 1–15 October

| Condition                     | Production | Café |
|-------------------------------|------------|------|
| No cruise, weekday            | 1          | 2    |
| No cruise, Saturday           | 1          | 3    |
| With cruise ship              | 1          | 3    |
| With good ship                | 1          | 4    |

### Mid Season: 1–15 June

| Condition                     | Production | Café |
|-------------------------------|------------|------|
| No cruise                     | 1          | 2    |
| With cruise ship              | 1          | 3    |
| With good ship                | 1          | 4    |

### Peak Season: 16 June – 31 August

| Condition                     | Production | Café |
|-------------------------------|------------|------|
| No cruise                     | 2          | 3    |
| With cruise ship              | 3          | 4    |
| With good ship                | 3          | 5    |

---

## 9. User Interface (Streamlit)

### 9.1 Pages / Sections

1. **Settings** — Configure establishment parameters: seasons, opening hours, shift templates, staffing rules
2. **Employees** — Upload CSV, view/edit employee list, set attributes
3. **Cruise Ships** — Upload cruise schedule CSV, upload ship-language mapping CSV, view calendar
4. **Schedule Generator** — Select month, generate schedule, view grid
5. **Schedule Editor** — Interactive grid (like the Excel spreadsheet), manual edits, LLM chat for adjustments
6. **Export** — Download as Excel or PDF

### 9.2 Schedule Grid (Target Output)

Based on the existing Excel format:
- **Rows:** Employee names (grouped: production staff, then café staff)
- **Columns:** Days of the month with day-of-week headers
- **Cells:** Shift number/code (1–6, P1–P5, special codes)
- **Colors:** Red = day off, Orange = part-time employee available (number present = booked to work)
- **Bottom section:** Cruise ship info per day (ship name, ships in harbour count, time in harbour)
- **Right pane:** Shift legend with times
- **Summary stats:** Hours per employee, coverage gaps, constraint violations

### 9.3 Interactive Features

- Click a cell to change assignment
- LLM chat panel for natural-language adjustments
- Validation indicators (warnings for soft constraint violations, errors for hard constraint violations)
- Re-generate with modified parameters

---

## 10. Data Upload Formats

### Employees CSV

```
name,languages,role_capability,employment_type,contracted_hours,housing,driving_licence,availability_start,availability_end
Aina,"english,spanish",both,full_time,37.5,eidsdal,true,2025-05-01,2025-10-15
Jonathan,"english,german",cafe,full_time,37.5,geiranger,false,2025-06-01,2025-09-30
```

### Cruise Ships CSV

```
ship_name,date,arrival_time,departure_time,port,size,good_ship
Costa Diadema,2025-08-04,11:30,19:30,geiranger_4B_SW,big,false
MSC Euribia,2025-08-12,12:00,21:00,geiranger_3S,big,true
```

### Ship Language Mapping CSV

```
ship_name,primary_language
Costa Diadema,italian
AIDA,german
MSC Euribia,spanish
```

---

## 11. Schedule Output Includes

- **Grid view** — the monthly schedule (matching the existing Excel format)
- **Summary statistics** — total hours per employee, days worked, overtime flag, coverage per day
- **Constraint report** — any violations or warnings
- **Export** — downloadable as Excel (.xlsx) and PDF

---

## 12. Implementation Phases

### Phase 1: Foundation
- Docker setup (Postgres + Streamlit)
- Pydantic models
- Database schema and migrations
- CSV upload and parsing

### Phase 2: Demand Engine
- Seasonal rule engine
- Cruise ship impact calculator
- Daily staffing demand profiles

### Phase 3: Schedule Solver
- Constraint-satisfaction engine (hard constraints)
- Soft constraint weighting
- Eidsdal transport grouping logic

### Phase 4: UI
- Streamlit pages (settings, employees, ships, schedule)
- Schedule grid rendering with colors
- Manual edit capability

### Phase 5: LLM Integration
- OpenAI advisory layer
- Natural-language schedule adjustments
- Schedule explanation

### Phase 6: Export & Polish
- Excel export (matching original format)
- PDF export
- Validation dashboard
