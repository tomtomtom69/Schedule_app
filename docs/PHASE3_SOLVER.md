# Phase 3: Schedule Solver — Implementation Guide

**Goal:** Given daily demand profiles + employee roster + constraints, generate a valid monthly schedule.

**Instructions for Claude Code:** Implement all files described below without requesting approval for each step. Only ask for approval before running system commands. This is the most complex phase — read SPEC.md Sections 5 and 6 carefully.

---

## 3.1 Architecture Decision: OR-Tools CP-SAT

Use Google OR-Tools CP-SAT solver. It handles constraint-satisfaction problems natively and is free, fast, and well-documented for scheduling.

The approach:
1. Define boolean variables: `shift[e][d][s]` = 1 if employee `e` works shift `s` on day `d`
2. Add hard constraints (labor law, language, transport)
3. Add soft constraints as weighted objectives
4. Solve and extract the assignment matrix

---

## 3.2 Main Scheduler

### `src/solver/scheduler.py`

```python
from ortools.sat.python import cp_model

class ScheduleGenerator:
    def __init__(
        self,
        employees: list[EmployeeRead],
        demand: list[DailyDemand],
        shift_templates: list[ShiftTemplateRead],
        settings: EstablishmentSettingsRead,
    ):
        self.employees = employees
        self.demand = demand
        self.shifts = shift_templates
        self.settings = settings
        self.model = cp_model.CpModel()
        self.variables = {}  # (employee_id, date, shift_id) → BoolVar

    def build_model(self):
        """Build the complete CP-SAT model."""
        self._create_variables()
        self._add_hard_constraints()
        self._add_soft_constraints()

    def solve(self) -> Schedule | None:
        """Solve and return Schedule object or None if infeasible."""
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 30  # timeout
        status = solver.Solve(self.model)
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return self._extract_schedule(solver)
        return None

    def _create_variables(self):
        """
        For each employee × day × compatible shift, create a BoolVar.
        Skip combinations where:
        - Employee not available on that date
        - Shift role doesn't match employee capability
        - Day is outside operating season
        """

    def _extract_schedule(self, solver) -> Schedule:
        """Read solution values and build Schedule + Assignment objects."""
```

---

## 3.3 Hard Constraints

### `src/solver/constraints.py`

Each constraint is a function that adds clauses to the CP-SAT model.

```python
def add_one_shift_per_day(model, variables, employees, days):
    """Each employee works at most one shift per day."""
    # For each (employee, day): sum of all shift vars ≤ 1

def add_daily_staffing_requirements(model, variables, demand, shifts):
    """
    For each day, ensure enough staff per role.
    - sum of café shift assignments ≥ demand.cafe_needed
    - sum of production shift assignments ≥ demand.production_needed
    """

def add_max_shift_duration(model, variables, shifts):
    """Shifts are predefined templates, all ≤ 10h. Validate at model build time."""

def add_weekly_hour_limits(model, variables, employees, shifts, days):
    """
    For each employee, for each calendar week:
    - sum of assigned shift hours ≤ 48 (absolute max)
    - Track for averaging: flag if any week > 40h
    """

def add_daily_rest(model, variables, employees, shifts, days):
    """
    Min 11h between end of one shift and start of next.
    For each employee, for consecutive days (d, d+1):
    - If assigned shift s1 on day d and s2 on day d+1:
      s2.start - s1.end ≥ 11 hours
    - Implement as: incompatible shift pairs on consecutive days are forbidden
    """

def add_weekly_rest(model, variables, employees, days):
    """
    At least 35h continuous rest per week.
    Simplified: at least 1 full day off per 7-day window.
    (A day off followed by a late-start next day typically satisfies 35h.)
    More precise: track rest windows.
    """

def add_language_requirements(model, variables, demand, employees, shifts):
    """
    For each day with required languages:
    - For each required language L:
      sum of café-shift assignments for employees who speak L ≥ 1
    """

def add_role_capability(model, variables, employees, shifts):
    """
    Employee can only be assigned shifts matching their role_capability.
    - cafe employees: only café shifts (1-6)
    - production employees: only production shifts (P1-P5)
    - both: any shift
    """

def add_availability(model, variables, employees, days):
    """
    Employee can only work on days within their availability_start..availability_end.
    Set variable = 0 for all out-of-range days.
    """
```

---

## 3.4 Eidsdal Transport Constraints

### `src/solver/transport.py`

```python
# Constants
EIDSDAL_CARS = 2
SEATS_PER_CAR = 5
MAX_EIDSDAL_WORKERS = EIDSDAL_CARS * SEATS_PER_CAR  # 10

def add_eidsdal_transport_constraints(model, variables, employees, shifts, days):
    """
    For Eidsdal-housed employees:
    
    1. MAX CAPACITY: At most 10 Eidsdal employees working on any given day.
       For each day: sum of Eidsdal employee assignments ≤ 10
    
    2. DRIVER REQUIREMENT: At least 1 driver per car in use.
       Simplified: if N Eidsdal employees work, need ceil(N/5) drivers.
       - If 1-5 Eidsdal workers: ≥ 1 driver among them
       - If 6-10 Eidsdal workers: ≥ 2 drivers among them
    
    3. SHIFT GROUPING (soft constraint — see soft_constraints.py):
       Prefer grouping Eidsdal employees into shifts with similar start/end times
       to minimize car trips. Ideal: all Eidsdal workers on same shift or 
       shifts that start/end within 30 min of each other.
    """

def add_driver_requirement(model, variables, employees, shifts, days):
    """
    For each day:
    - Let eidsdal_working = list of BoolVars for Eidsdal employees
    - Let eidsdal_drivers = subset where driving_licence=True
    - If sum(eidsdal_working) > 0: sum(eidsdal_drivers working) ≥ 1
    - If sum(eidsdal_working) > 5: sum(eidsdal_drivers working) ≥ 2
    
    Implement with implications:
    - Create auxiliary BoolVar: car2_needed = (sum(eidsdal_working) > 5)
    - sum(eidsdal_drivers_working) ≥ 1 + car2_needed
    """
```

---

## 3.5 Soft Constraints (Objective Function)

### `src/solver/soft_constraints.py`

Soft constraints are encoded as weighted terms in the objective function (maximize).

```python
WEIGHTS = {
    "full_time_preference": 10,
    "eidsdal_grouping": 8,
    "employee_preferences": 5,
    "fair_distribution": 5,
    "minimize_overtime": 3,
}

def add_soft_constraints(model, variables, employees, shifts, days, demand):
    """Add all soft constraints to the model's objective."""
    objective_terms = []
    
    objective_terms += prefer_full_time(model, variables, employees, shifts, days)
    objective_terms += group_eidsdal_shifts(model, variables, employees, shifts, days)
    objective_terms += respect_preferences(model, variables, employees, days)
    objective_terms += distribute_hours_fairly(model, variables, employees, shifts, days)
    objective_terms += minimize_overtime(model, variables, employees, shifts, days)
    
    model.Maximize(sum(objective_terms))

def prefer_full_time(model, variables, employees, shifts, days):
    """
    Reward assigning full-time employees.
    Penalize assigning part-time employees.
    Each full-time assignment: +WEIGHT
    Each part-time assignment: -WEIGHT
    """

def group_eidsdal_shifts(model, variables, employees, shifts, days):
    """
    Reward Eidsdal employees being on the same or adjacent shifts.
    For each day, for each pair of Eidsdal employees both working:
    - If same shift: +WEIGHT
    - If adjacent shifts (start times within 30min): +WEIGHT/2
    - If far apart shifts: 0
    """

def respect_preferences(model, variables, employees, days):
    """
    Penalize assignments that violate employee preferences.
    E.g., if employee prefers no Mondays: -WEIGHT for each Monday assignment.
    """

def distribute_hours_fairly(model, variables, employees, shifts, days):
    """
    Minimize variance in total hours among employees of same type.
    Use min-max approach: minimize the difference between the most-worked
    and least-worked employee.
    """

def minimize_overtime(model, variables, employees, shifts, days):
    """
    Penalize weekly hours > 40 for any employee.
    Small penalty per overtime hour to prefer normal schedules.
    """
```

---

## 3.6 Post-Generation Validator

### `src/solver/validator.py`

After the solver produces a schedule, run a pure-Python validation pass. This catches anything the solver might have approximated and provides human-readable violation reports.

```python
@dataclass
class Violation:
    severity: str  # "error" (hard) or "warning" (soft)
    constraint: str
    employee: str
    date: date | None
    message: str

def validate_schedule(
    schedule: Schedule,
    employees: list[EmployeeRead],
    demand: list[DailyDemand],
    shift_templates: list[ShiftTemplateRead],
) -> list[Violation]:
    """
    Run all validation checks and return list of violations.
    
    Hard constraint checks (severity="error"):
    - Max shift duration
    - Weekly hour limits (48h absolute)
    - Daily rest (11h between shifts)
    - Weekly rest (35h continuous)
    - Language coverage
    - Eidsdal driver requirement
    - Role capability match
    
    Soft constraint checks (severity="warning"):
    - Part-timer used when full-timer available
    - Eidsdal employees on mismatched shifts
    - Employee preference violations
    - Uneven hour distribution
    - Overtime (weekly > 40h)
    """
```

---

## 3.7 Acceptance Criteria

Phase 3 is complete when:
- [ ] Solver produces a valid month schedule for a test dataset (5 employees, 10 ship days)
- [ ] All hard constraints pass validation (zero errors)
- [ ] Changing demand (adding/removing ships) changes the output appropriately
- [ ] Language matching works: Spanish ship → Spanish speaker assigned to café
- [ ] Eidsdal transport: max 10 Eidsdal workers/day, driver in each car
- [ ] Solver respects 48h/week absolute max and 11h daily rest
- [ ] Solver completes within 30 seconds for realistic data (15 employees, 31 days)
- [ ] Validator produces a clean report with zero errors for valid schedules
- [ ] Validator correctly flags violations when constraints are manually broken
