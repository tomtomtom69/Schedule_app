"""Main scheduling engine — Phase 3.

Orchestrates the CP-SAT model build, solve, and result extraction.
"""
import logging
import uuid
from datetime import date, datetime

from ortools.sat.python import cp_model

from src.demand.forecaster import DailyDemand
from src.models.employee import EmployeeRead
from src.models.enums import RoleCapability, ScheduleStatus, ShiftRole
from src.models.establishment import EstablishmentSettingsRead
from src.models.schedule import AssignmentRead, ScheduleRead
from src.models.shift_template import ShiftTemplateRead
from src.solver.constraints import (
    Variables,
    add_availability,
    add_daily_rest,
    add_daily_staffing_requirements,
    add_language_requirements,
    add_one_shift_per_day,
    add_role_capability,
    add_weekly_hour_limits,
    add_weekly_rest,
)
from src.solver.soft_constraints import add_soft_constraints
from src.solver.transport import add_eidsdal_transport_constraints

logger = logging.getLogger(__name__)

_SOLVER_TIMEOUT_SECONDS = 30


class ScheduleGenerator:
    """Builds and solves a CP-SAT scheduling model for one month.

    Usage::

        gen = ScheduleGenerator(employees, demand, shift_templates, settings)
        gen.build_model()
        schedule = gen.solve()   # returns ScheduleRead or None if infeasible
    """

    def __init__(
        self,
        employees: list[EmployeeRead],
        demand: list[DailyDemand],
        shift_templates: list[ShiftTemplateRead],
        settings: EstablishmentSettingsRead | list[EstablishmentSettingsRead] | None = None,
    ) -> None:
        self.employees = employees
        self.demand = demand
        self.shifts = shift_templates
        self.settings = settings
        self.model = cp_model.CpModel()
        self.variables: Variables = {}

        # Derived state populated during build
        self._days: list[date] = sorted(d.date for d in demand)
        self._demand_map: dict[date, DailyDemand] = {d.date: d for d in demand}

    # ── Public interface ─────────────────────────────────────────────────────

    def build_model(self) -> None:
        """Build the complete CP-SAT model: variables + hard + soft constraints."""
        self._create_variables()
        self._add_hard_constraints()
        self._add_soft_constraints()
        logger.info(
            "Model built: %d variables, %d days, %d employees, %d shift templates",
            len(self.variables),
            len(self._days),
            len(self.employees),
            len(self.shifts),
        )

    def solve(self) -> ScheduleRead | None:
        """Solve the model and return a ScheduleRead, or None if infeasible/timeout."""
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = _SOLVER_TIMEOUT_SECONDS
        solver.parameters.log_search_progress = False

        logger.info("Starting CP-SAT solve (timeout=%ds)…", _SOLVER_TIMEOUT_SECONDS)
        status = solver.Solve(self.model)
        logger.info("Solver status: %s", solver.StatusName(status))

        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            logger.info(
                "Objective value: %.0f  |  wall time: %.2fs",
                solver.ObjectiveValue(),
                solver.WallTime(),
            )
            return self._extract_schedule(solver)

        logger.warning("No feasible solution found (status=%s)", solver.StatusName(status))
        return None

    # ── Model building (private) ─────────────────────────────────────────────

    def _create_variables(self) -> None:
        """Create one BoolVar per compatible (employee, day, shift) triple.

        Skips combinations where:
        - Employee is not available on that date
        - Shift role doesn't match employee's role_capability
        """
        for emp in self.employees:
            for d in self._days:
                if not (emp.availability_start <= d <= emp.availability_end):
                    continue
                for shift in self.shifts:
                    if not self._shift_compatible(emp, shift):
                        continue
                    var_name = f"x_{emp.id}_{d.isoformat()}_{shift.id}"
                    var = self.model.NewBoolVar(var_name)
                    self.variables[(emp.id, d, shift.id)] = var

    @staticmethod
    def _shift_compatible(emp: EmployeeRead, shift: ShiftTemplateRead) -> bool:
        """Return True if the employee's role_capability allows this shift."""
        if emp.role_capability == RoleCapability.cafe:
            return shift.role == ShiftRole.cafe
        if emp.role_capability == RoleCapability.production:
            return shift.role == ShiftRole.production
        # RoleCapability.both: any shift
        return True

    def _add_hard_constraints(self) -> None:
        """Add all hard constraints to the CP-SAT model."""
        add_one_shift_per_day(self.model, self.variables, self.employees, self.shifts, self._days)
        add_daily_staffing_requirements(
            self.model, self.variables, self._demand_map,
            self.employees, self.shifts, self._days,
        )
        add_weekly_hour_limits(
            self.model, self.variables, self.employees, self.shifts, self._days
        )
        add_daily_rest(self.model, self.variables, self.employees, self.shifts, self._days)
        add_weekly_rest(self.model, self.variables, self.employees, self.shifts, self._days)
        add_language_requirements(
            self.model, self.variables, self._demand_map,
            self.employees, self.shifts, self._days,
        )
        add_role_capability(self.model, self.variables, self.employees, self.shifts)
        add_availability(self.model, self.variables, self.employees, self._days)
        add_eidsdal_transport_constraints(
            self.model, self.variables, self.employees, self.shifts, self._days
        )

    def _add_soft_constraints(self) -> None:
        """Add the weighted objective function (soft constraints)."""
        add_soft_constraints(
            self.model, self.variables, self.employees, self.shifts,
            self._days, self._demand_map,
        )

    # ── Solution extraction (private) ────────────────────────────────────────

    def _extract_schedule(self, solver: cp_model.CpSolver) -> ScheduleRead:
        """Read solver values and build a ScheduleRead with all assignments."""
        schedule_id = uuid.uuid4()
        assignments: list[AssignmentRead] = []

        # Track which (emp, day) combinations have working shifts
        emp_day_worked: set[tuple] = set()

        for (emp_id, d, shift_id), var in self.variables.items():
            if solver.Value(var):
                assignments.append(AssignmentRead(
                    id=uuid.uuid4(),
                    schedule_id=schedule_id,
                    employee_id=emp_id,
                    date=d,
                    shift_id=shift_id,
                    is_day_off=False,
                ))
                emp_day_worked.add((emp_id, d))

        # Add explicit day-off records for available employees not assigned
        for emp in self.employees:
            for d in self._days:
                if not (emp.availability_start <= d <= emp.availability_end):
                    continue
                if (emp.id, d) not in emp_day_worked:
                    assignments.append(AssignmentRead(
                        id=uuid.uuid4(),
                        schedule_id=schedule_id,
                        employee_id=emp.id,
                        date=d,
                        shift_id="off",
                        is_day_off=True,
                    ))

        # Determine month/year from demand
        if self._days:
            month = self._days[0].month
            year = self._days[0].year
        else:
            today = date.today()
            month, year = today.month, today.year

        return ScheduleRead(
            id=schedule_id,
            month=month,
            year=year,
            status=ScheduleStatus.draft,
            created_at=datetime.utcnow(),
            assignments=sorted(assignments, key=lambda a: (a.date, str(a.employee_id))),
        )
