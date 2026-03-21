# Phase 6: Export & Polish — Implementation Guide

**Goal:** Excel and PDF export matching the original spreadsheet format, plus final UI polish and validation dashboard.

**Instructions for Claude Code:** Implement all files described below without requesting approval for each step. Only ask for approval before running system commands.

---

## 6.1 Excel Export

### `src/export/excel_export.py`

Generate an `.xlsx` file that matches the original `Vaktlista_Geiranger_Sjokolade` format as closely as possible.

```python
import openpyxl
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side

# Color definitions matching the original spreadsheet
COLORS = {
    "day_off": "FFB3B3",          # Red
    "part_time_available": "FFD699",  # Orange
    "header_bg": "D9E1F2",        # Light blue
    "cruise_row_bg": "E2EFDA",    # Light green
    "production_header": "F4B084", # Orange header
    "cafe_header": "9BC2E6",      # Blue header
}

def export_schedule_to_excel(
    schedule: Schedule,
    employees: list[EmployeeRead],
    demand: list[DailyDemand],
    shift_templates: list[ShiftTemplateRead],
    month: int,
    year: int,
) -> bytes:
    """
    Generate Excel workbook matching the original format.
    Returns bytes (write to file or send as download).
    
    Layout:
    - Row 1: "Geiranger Sjokolade {year}"
    - Row 2: "Vaktplan {month_name}"
    - Row 3: Opening hours
    - Row 5: blank
    - Row 6: Day names (Monday, Tuesday, ...)
    - Row 7: Day numbers (1, 2, 3, ...)
    - Rows 8+: Employee assignments (production group first, then café)
    - Gap row
    - Cruise info rows: Ship name, Ships in harbour, Time in harbour
    - Right side columns: Shift legend
    
    Split month into two halves if > 16 days (days 1-15, then 16-31),
    each as its own block vertically.
    """

def _style_cell(ws, row, col, value, cell_type):
    """Apply formatting based on cell type."""
    cell = ws.cell(row=row, column=col, value=value)
    
    if cell_type == "day_off":
        cell.fill = PatternFill(start_color=COLORS["day_off"], fill_type="solid")
    elif cell_type == "part_time_available":
        cell.fill = PatternFill(start_color=COLORS["part_time_available"], fill_type="solid")
    # ... etc

def _add_shift_legend(ws, start_row, start_col, shift_templates):
    """Add shift legend block to the right of the grid."""

def _add_cruise_info(ws, start_row, days, demand):
    """Add cruise ship rows below the employee grid."""
```

### Export Page Integration

In `src/ui/pages/5_export.py`:

```python
def render_export_page():
    st.title("📥 Export Schedule")
    
    # Month/year selector
    month = st.selectbox("Month", range(5, 11), format_func=lambda m: calendar.month_name[m])
    year = st.number_input("Year", value=2025)
    
    # Load schedule from DB
    schedule = load_schedule(year, month)
    
    if schedule:
        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader("Excel Export")
            excel_bytes = export_schedule_to_excel(schedule, ...)
            st.download_button(
                "📊 Download Excel",
                data=excel_bytes,
                file_name=f"Vaktplan_{calendar.month_name[month]}_{year}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        
        with col2:
            st.subheader("PDF Export")
            pdf_bytes = export_schedule_to_pdf(schedule, ...)
            st.download_button(
                "📄 Download PDF",
                data=pdf_bytes,
                file_name=f"Vaktplan_{calendar.month_name[month]}_{year}.pdf",
                mime="application/pdf",
            )
    else:
        st.warning("No schedule found for this month. Generate one first.")
```

---

## 6.2 PDF Export

### `src/export/pdf_export.py`

Use `reportlab` or `weasyprint` to generate a PDF version of the schedule.

```python
def export_schedule_to_pdf(
    schedule: Schedule,
    employees: list[EmployeeRead],
    demand: list[DailyDemand],
    shift_templates: list[ShiftTemplateRead],
    month: int,
    year: int,
) -> bytes:
    """
    Generate PDF matching the grid layout.
    Landscape orientation for readability.
    Include:
    - Schedule grid with colors
    - Shift legend
    - Cruise ship info
    - Summary statistics page
    """
```

Add `reportlab` or `weasyprint` to `requirements.txt`.

---

## 6.3 Validation Dashboard

Enhance the summary panel on the schedule page:

### Constraint Violation Display

```python
def render_violation_panel(violations: list[Violation]):
    """
    Display violations grouped by severity.
    
    Errors (red): hard constraint violations — must be fixed
    Warnings (yellow): soft constraint issues — review recommended
    
    Each violation shows:
    - Icon (🔴 or 🟡)
    - Constraint name
    - Employee name
    - Date
    - Human-readable message
    - "Fix" button (asks LLM for suggestion)
    """
    
    errors = [v for v in violations if v.severity == "error"]
    warnings = [v for v in violations if v.severity == "warning"]
    
    if errors:
        st.error(f"🔴 {len(errors)} constraint violation(s) — must be resolved")
        for v in errors:
            with st.expander(f"{v.constraint}: {v.employee} — {v.date}"):
                st.write(v.message)
                if st.button(f"Get fix suggestion", key=f"fix_{v.employee}_{v.date}"):
                    # Ask LLM for a fix
                    suggestion = advisor.chat(f"Fix this violation: {v.message}")
                    st.write(suggestion["text"])
    
    if warnings:
        st.warning(f"🟡 {len(warnings)} warning(s)")
        for v in warnings:
            with st.expander(f"{v.constraint}: {v.employee}"):
                st.write(v.message)
```

### Employee Summary Table

```python
def render_employee_summary(schedule, employees, shift_templates):
    """
    Table showing per-employee stats for the month:
    - Total hours worked
    - Days worked
    - Days off
    - Overtime hours (if any)
    - Max consecutive days worked
    - Weekly breakdown
    
    Highlight rows where overtime is flagged.
    """
```

### Coverage Heatmap

```python
def render_coverage_heatmap(schedule, demand):
    """
    Visual showing staffing vs demand per day.
    Green: fully staffed
    Yellow: slightly understaffed
    Red: significantly understaffed
    Blue: overstaffed
    """
```

---

## 6.4 Final Polish

### UI Improvements
- Loading spinners during schedule generation
- Success/error toast messages
- Responsive layout (works on different screen sizes)
- Consistent color theme throughout

### Error Handling
- Graceful handling of DB connection failures
- Useful error messages for CSV parse failures
- LLM timeout handling (show retry button)
- Solver infeasibility message (explain why no valid schedule exists)

### Data Management
- "Reset Database" button in settings (with confirmation)
- "Delete Schedule" option per month
- Backup/restore capability (export all data as JSON)

---

## 6.5 Updated Requirements

### `requirements.txt` (final)

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
reportlab
pillow
```

---

## 6.6 Acceptance Criteria

Phase 6 is complete when:
- [ ] Excel export produces .xlsx matching the original spreadsheet format
- [ ] Excel has correct colors: red for day off, orange for part-time
- [ ] Excel includes shift legend on the right
- [ ] Excel includes cruise ship info at the bottom
- [ ] PDF export produces a readable landscape schedule
- [ ] PDF includes summary statistics page
- [ ] Download buttons work in Streamlit
- [ ] Violation panel shows errors and warnings with clear messages
- [ ] "Get fix suggestion" button queries LLM and displays response
- [ ] Employee summary table shows hours, days, overtime per employee
- [ ] Coverage heatmap shows over/under-staffing per day
- [ ] Error handling: graceful messages for DB issues, CSV errors, LLM timeouts
- [ ] The complete application works end-to-end:
  - Upload employees → Upload ships → Configure settings → Generate schedule → Review → Edit → Export
