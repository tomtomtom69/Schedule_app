# Phase 4: Streamlit UI — Implementation Guide

**Goal:** Complete Streamlit interface with all pages: settings, employee management, cruise ships, schedule generation/editing.

**Instructions for Claude Code:** Implement all files described below without requesting approval for each step. Only ask for approval before running system commands. Reference the August spreadsheet layout described in SPEC.md Section 9.2 for the schedule grid.

---

## 4.1 App Configuration

### `src/ui/app.py`

```python
import streamlit as st
from src.db.migrations import create_all_tables
from src.db.seed import seed_defaults

# Initialize DB on first run
create_all_tables()
seed_defaults()

st.set_page_config(
    page_title="Geiranger Sjokolade Scheduler",
    page_icon="🍫",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Sidebar navigation
st.sidebar.title("🍫 Geiranger Sjokolade")
st.sidebar.markdown("Staff Scheduling System")

# Home page content
st.title("Welcome")
st.write("Use the sidebar to navigate between sections.")
```

Streamlit multipage: pages in `src/ui/pages/` are auto-discovered and shown in sidebar.

---

## 4.2 Settings Page

### `src/ui/pages/1_settings.py`

**Sections:**

1. **Season Configuration**
   - Table showing current season definitions (low/mid/peak with date ranges)
   - Editable with `st.data_editor`
   - Save button writes to DB

2. **Opening Hours**
   - Per-season opening and closing times
   - Editable time inputs

3. **Shift Templates**
   - Table of all shifts (ID, role, start, end)
   - `st.data_editor` for inline editing
   - Add/remove shift capability
   - Validation: shift ≤ 10h, start < end

4. **Staffing Rules**
   - Per-season staffing tables (the STAFFING_RULES from Phase 2)
   - Editable grid showing production + café numbers for each condition
   - Save persists to DB, which the demand engine reads

5. **Eidsdal Transport Settings**
   - Number of cars (default: 2)
   - Seats per car (default: 5)
   - Editable in case circumstances change

---

## 4.3 Employees Page

### `src/ui/pages/2_employees.py`

**Sections:**

1. **Upload CSV/Excel**
   - `st.file_uploader` accepting .csv and .xlsx
   - Preview parsed data in a table before committing
   - Show validation errors per row (red highlighting)
   - "Import" button saves valid rows to DB
   - Option to replace all or append

2. **Employee List**
   - Table of all employees from DB
   - Columns: Name, Languages, Role, Employment Type, Hours, Housing, Licence, Availability
   - Color coding: Eidsdal employees highlighted
   - Drivers marked with icon

3. **Edit Employee**
   - Click row to expand edit form
   - All fields editable
   - Delete button with confirmation

4. **Quick Stats**
   - Total employees, full-time vs part-time count
   - Language coverage summary
   - Eidsdal employees count + drivers count
   - Warning if insufficient drivers for Eidsdal capacity

---

## 4.4 Cruise Ships Page

### `src/ui/pages/3_cruise_ships.py`

**Sections:**

1. **Upload Cruise Schedule**
   - `st.file_uploader` for cruise ship CSV/Excel
   - Preview and validation before import

2. **Upload Ship Language Mapping**
   - Separate uploader for the ship→language CSV
   - Shows current mapping table
   - Merge behavior: update existing, add new

3. **Monthly Calendar View**
   - Month selector
   - Calendar-style grid showing ships per day
   - Each day cell shows: ship name(s), port, time, size badge, good-ship badge
   - Color intensity based on number of ships

4. **Ship List**
   - Sortable/filterable table of all ships
   - Columns: Date, Ship Name, Port, Arrival, Departure, Size, Good Ship, Language
   - Edit capability per row

---

## 4.5 Schedule Page (Generator + Editor)

### `src/ui/pages/4_schedule.py`

This is the main page. It combines generation, display, editing, and LLM chat.

**Layout:** Two columns — main area (schedule grid) + right sidebar (LLM chat).

**Sections:**

1. **Month Selector**
   - Year + Month dropdowns (only May–October enabled)
   - "Generate Schedule" button
   - "Load Existing" if a saved schedule exists for that month

2. **Schedule Grid** (the core component — see 4.6)

3. **Summary Panel** (below grid)
   - Per-employee stats: total hours, days worked, overtime flag
   - Coverage summary: days with gaps highlighted
   - Constraint violations: errors (red) and warnings (yellow)

4. **Action Buttons**
   - Save Draft
   - Approve Schedule
   - Export (→ redirects to export page)
   - Regenerate (with option to lock certain assignments)

---

## 4.6 Schedule Grid Component

### `src/ui/components/schedule_grid.py`

This is the most important UI component. It must match the Excel spreadsheet format.

**Structure:**

```
|          | Mon 1 | Tue 2 | Wed 3 | ... | Sat 16 | ... | Thu 31 |  Shift Legend     |
|----------|-------|-------|-------|-----|--------|-----|--------|-------------------|
| PRODUCTION                                                     |                   |
| Aina     |  P2   |  P2   |       | ... |  P5    | ... |        | 1: Shop 8-16      |
| Marta    |       |  P5   |  P5   | ... |  P5    | ... |  P2    | 2: Shop 9:30-17:30|
| CAFÉ                                                           | 3: Shop 11-19     |
| Vanna    |   5   |   5   |   5   | ... |   5    | ... |        | 4: Shop 12-20     |
| Aniol    |   5   |       |       | ... |   2    | ... |   5    | 5: Shop 13-21     |
| ...      |       |       |       |     |        |     |        | 6: Shop 10-17     |
|----------|-------|-------|-------|-----|--------|-----|--------|                   |
| CRUISE INFO                                                    | P1: Prod 8-16     |
| Ship     |       |       |       | Costa|       |     |        | P2: Prod 9:30-17:30|
| Count    |       |       |       |  1  |        |     |        | ...               |
| Time     |       |       |       |11-19|        |     |        |                   |
```

**Cell rendering:**
- Shift number/code as text
- Background colors:
  - Red (#FFB3B3): day off
  - Orange (#FFD699): part-time employee, available but not scheduled
  - Orange + number: part-time employee, scheduled
  - Light green (#B3FFB3): normal assignment
  - White/empty: not available or not applicable

**Interaction:**
- Click cell → dropdown to select shift or mark as day off
- Use `st.data_editor` or custom HTML component
- Changes saved to session state, then to DB on "Save"

**Implementation approach:**
Build the grid as a pandas DataFrame, then render with `st.dataframe` or `st.data_editor` with custom cell styling. For colors, use Streamlit's column_config or build an HTML table with `st.markdown(html, unsafe_allow_html=True)`.

If `st.data_editor` styling is too limited, fall back to a custom HTML/CSS grid rendered via `st.components.v1.html()`.

---

## 4.7 Ship Calendar Component

### `src/ui/components/ship_calendar.py`

```python
def render_ship_calendar(year: int, month: int, ships: list[CruiseShipRead]):
    """
    Render a monthly calendar view with ship arrivals.
    Each day cell shows ship names, times, and badges.
    Days with ships are highlighted.
    """
```

---

## 4.8 LLM Chat Panel

### `src/ui/components/chat_panel.py`

Right sidebar on the schedule page:

```python
def render_chat_panel(schedule: Schedule, employees: list, demand: list):
    """
    Chat interface for LLM-powered schedule adjustments.
    
    Features:
    - Text input for natural language requests
    - Message history in session state
    - LLM receives current schedule state as context
    - LLM responses include specific adjustment suggestions
    - "Apply" button next to each suggestion
    """
```

This component is fully implemented in Phase 5. In Phase 4, create the UI shell (text input, message display) with a placeholder response.

---

## 4.9 Acceptance Criteria

Phase 4 is complete when:
- [ ] All 5 pages load without errors
- [ ] Settings page: can edit and save season configs, shift templates, staffing rules
- [ ] Employees page: can upload CSV, see employee list, edit individual employees
- [ ] Cruise ships page: can upload ship CSV + language CSV, see calendar view
- [ ] Schedule page: can select month, click "Generate", see the grid
- [ ] Grid shows employee rows grouped by role with shift codes
- [ ] Grid shows cruise ship info at the bottom
- [ ] Grid shows shift legend on the right
- [ ] Cell colors work: red for day off, orange for part-time available
- [ ] Clicking a cell allows changing the shift assignment
- [ ] Summary stats panel shows hours per employee and coverage
- [ ] Chat panel shell is present (placeholder, functional in Phase 5)
