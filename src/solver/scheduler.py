"""Main scheduling engine — Phase 3.

Orchestrates the CP-SAT model build, solve, and result extraction.
"""
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from ortools.sat.python import cp_model

from src.demand.forecaster import DailyDemand
from src.models.employee import EmployeeRead
from src.models.enums import RoleCapability, ScheduleStatus, ShiftRole
from src.models.establishment import EstablishmentSettingsRead
from src.models.schedule import AssignmentRead, ScheduleRead
from src.models.shift_template import ShiftTemplateRead
from src.solver.constraints import (
    Variables,
    add_age_based_constraints,
    add_availability,
    add_cross_month_consecutive_constraint,
    add_daily_rest,
    add_daily_staffing_requirements,
    add_max_consecutive_working_days,
    add_max_days_per_calendar_week,
    add_max_staffing_caps,
    add_one_shift_per_day,
    add_opening_hours_coverage,
    add_role_capability,
    add_sunday_rest_constraints,
    add_two_consecutive_days_off_per_14,
    add_weekly_hour_limits,
    add_weekly_rest,
)
from src.solver.soft_constraints import add_soft_constraints
from src.solver.transport import add_eidsdal_transport_constraints

logger = logging.getLogger(__name__)

_SOLVER_TIMEOUT_SECONDS = 60


@dataclass
class SolveInfo:
    """Diagnostic information from the most recent solve() call."""
    status_name: str = "NOT_RUN"
    num_variables: int = 0
    num_days: int = 0
    num_employees_available: int = 0
    num_working_assignments: int = 0
    wall_time: float = 0.0
    objective_value: float = 0.0
    diagnostics: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_success(self) -> bool:
        return self.status_name in ("OPTIMAL", "FEASIBLE") and self.num_working_assignments > 0

    @property
    def is_empty_solution(self) -> bool:
        """True if solver said OK but produced zero working shifts."""
        return self.status_name in ("OPTIMAL", "FEASIBLE") and self.num_working_assignments == 0


class ScheduleGenerator:
    """Builds and solves a CP-SAT scheduling model for one month.

    Usage::

        gen = ScheduleGenerator(employees, demand, shift_templates, settings)
        gen.build_model()
        schedule = gen.solve()   # returns ScheduleRead or None if infeasible
        info = gen.solve_info    # SolveInfo with diagnostics
    """

    def __init__(
        self,
        employees: list[EmployeeRead],
        demand: list[DailyDemand],
        shift_templates: list[ShiftTemplateRead],
        settings: EstablishmentSettingsRead | list[EstablishmentSettingsRead] | None = None,
        closed_days: "set[date] | None" = None,
    ) -> None:
        self.employees = employees
        self.demand = demand
        self.shifts = shift_templates
        self.settings = settings
        self.model = cp_model.CpModel()
        self.variables: Variables = {}
        self.solve_info = SolveInfo()

        # Closed days are excluded from all scheduling logic — they act as gaps that
        # naturally break consecutive-day windows.
        _closed = closed_days or set()

        # Derived state populated during build
        self._days: list[date] = sorted(d.date for d in demand if d.date not in _closed)
        self._demand_map: dict[date, DailyDemand] = {d.date: d for d in demand if d.date not in _closed}
        # Previous month's working dates — loaded during build_model for cross-month constraints
        self._prev_month_working: dict[str, set[date]] = {}

        # Log closed-days summary so the UI diagnostics can confirm they were applied
        if _closed:
            logger.info(
                "Closed days excluded from model (%d total): %s",
                len(_closed),
                ", ".join(str(d) for d in sorted(_closed)),
            )
        logger.info(
            "Open days this month: %d  (demand entries before closed-day filter: %d)",
            len(self._days),
            len(demand),
        )
        # Log staffing minimums from demand so the user can verify rules loaded correctly
        if self._demand_map:
            for d in sorted(self._demand_map)[:3]:  # first 3 days as sample
                dd = self._demand_map[d]
                logger.info(
                    "Demand sample %s (%s): cafe_needed=%d, production_needed=%d",
                    d, dd.season.value if hasattr(dd.season, "value") else dd.season,
                    dd.cafe_needed, dd.production_needed,
                )

    # ── Public interface ─────────────────────────────────────────────────────

    def build_model(
        self,
        disable_both_preference: bool = False,
        skeleton_mode: bool = False,
    ) -> None:
        """Build the complete CP-SAT model: variables + hard + soft constraints.

        disable_both_preference: passed through to soft constraints; used in
        fallback Step D to allow 'both' employees to cover café freely.

        skeleton_mode: when True (fallback Pass 3), drops complex constraints
        (opening hours coverage, two-consecutive-days-off-per-14, Sunday rest,
        max staffing caps) and replaces the full soft objective with a simple
        "minimize total working assignments" to give employees maximum rest.
        Only the absolute legal minimums are enforced.
        """
        self._disable_both_preference = disable_both_preference
        self._skeleton_mode = skeleton_mode
        # Load establishment settings if not provided — needed for coverage constraints
        if self.settings is None:
            self.settings = self._load_settings()

        # Log the distinct staffing minimums visible in demand (confirms DB rules loaded)
        if self._demand_map:
            seen: set[tuple] = set()
            for dd in self._demand_map.values():
                seen.add((
                    dd.season.value if hasattr(dd.season, "value") else str(dd.season),
                    dd.cafe_needed,
                    dd.production_needed,
                ))
            rules_lines = [f"{s}: cafe≥{c}, prod≥{p}" for s, c, p in sorted(seen)]
            logger.info("Staffing minimums from demand (DB rules applied): %s", " | ".join(rules_lines))

        # Load previous month's working dates for cross-month consecutive-day constraint
        if self._days:
            y, m = self._days[0].year, self._days[0].month
            self._prev_month_working = self._load_prev_month_working(y, m)
            n_prev = sum(1 for v in self._prev_month_working.values() if v)
            logger.info(
                "Previous month schedule loaded: %d employee(s) with working dates in %d-%02d",
                n_prev, y, m - 1 if m > 1 else 12,
            )

        self._create_variables()
        self._pre_flight_checks()
        self._add_hard_constraints()
        if skeleton_mode:
            self._add_skeleton_objective()
        else:
            self._add_soft_constraints()

        n_vars = len(self.variables)
        n_days = len(self._days)
        n_emps = len(self.employees)
        n_avail = self._count_available_employees()
        logger.info(
            "Model built: %d variables, %d days, %d employees (%d available this month), %d shifts",
            n_vars, n_days, n_emps, n_avail, len(self.shifts),
        )
        self.solve_info.num_variables = n_vars
        self.solve_info.num_days = n_days
        self.solve_info.num_employees_available = n_avail

    def solve(self) -> Optional[ScheduleRead]:
        """Solve the model and return a ScheduleRead, or None if infeasible/timeout."""
        if len(self.variables) == 0:
            msg = (
                "No scheduling variables created — this means no employee is available "
                f"during the selected month. "
                f"Check employee availability dates (currently covering: "
                f"{self._availability_summary()})."
            )
            self.solve_info.status_name = "NO_VARIABLES"
            self.solve_info.diagnostics.append(msg)
            logger.error(msg)
            return None

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = _SOLVER_TIMEOUT_SECONDS
        solver.parameters.log_search_progress = False

        logger.info("Starting CP-SAT solve (timeout=%ds, variables=%d)…",
                    _SOLVER_TIMEOUT_SECONDS, len(self.variables))
        status = solver.Solve(self.model)
        status_name = solver.StatusName(status)
        logger.info("Solver status: %s  |  wall time: %.2fs", status_name, solver.WallTime())

        self.solve_info.status_name = status_name
        self.solve_info.wall_time = solver.WallTime()

        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            self.solve_info.objective_value = solver.ObjectiveValue()
            schedule = self._extract_schedule(solver)
            working = sum(1 for a in schedule.assignments if not a.is_day_off)
            self.solve_info.num_working_assignments = working
            logger.info("Objective: %.0f | working assignments: %d", solver.ObjectiveValue(), working)

            if working == 0:
                msg = (
                    "Solver returned a feasible solution but assigned ZERO working shifts. "
                    "All hard constraints were trivially satisfied (no variables active). "
                    "Most likely cause: employee availability dates do not overlap with the "
                    f"scheduled month. Availability window summary: {self._availability_summary()}."
                )
                self.solve_info.diagnostics.append(msg)
                logger.warning(msg)
                return None

            return schedule

        # Infeasible or unknown
        self.solve_info.diagnostics.extend(self._generate_infeasibility_hints())
        if status == cp_model.INFEASIBLE:
            logger.error("Model is INFEASIBLE. %s", "; ".join(self.solve_info.diagnostics))
        elif status == cp_model.UNKNOWN:
            logger.warning("Solver timed out after %.1fs without finding a solution.", solver.WallTime())
            self.solve_info.diagnostics.insert(0,
                f"Solver timed out after {solver.WallTime():.0f}s. "
                "Try reducing the month or simplifying constraints.")
        return None

    # ── Model building (private) ─────────────────────────────────────────────

    def _create_variables(self) -> None:
        """Create one BoolVar per compatible (employee, day, shift) triple."""
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
        if emp.role_capability == RoleCapability.cafe:
            return shift.role == ShiftRole.cafe
        if emp.role_capability == RoleCapability.production:
            return shift.role == ShiftRole.production
        return True

    def _pre_flight_checks(self) -> None:
        """Run sanity checks and populate warnings before solving."""
        if not self._days:
            self.solve_info.warnings.append("No demand days found for selected month/season.")
            return

        month_start = self._days[0]
        month_end = self._days[-1]

        # Check employees who have NO availability overlap with this month
        unavailable = [
            e.name for e in self.employees
            if e.availability_end < month_start or e.availability_start > month_end
        ]
        if unavailable:
            self.solve_info.warnings.append(
                f"{len(unavailable)} employee(s) have availability dates that don't overlap "
                f"with {month_start.strftime('%B %Y')}: {', '.join(unavailable[:5])}"
                + (f" ... and {len(unavailable)-5} more" if len(unavailable) > 5 else "")
            )

        # Check language coverage gaps
        demand_langs: dict[str, list[date]] = {}
        for d in self._days:
            dd = self._demand_map.get(d)
            if dd:
                for lang in dd.languages_required:
                    demand_langs.setdefault(lang, []).append(d)

        for lang, days in demand_langs.items():
            speakers = [
                e for e in self.employees
                if any(l.lower().strip() == lang for l in e.languages)
                and e.availability_start <= days[0] <= e.availability_end
            ]
            if not speakers:
                self.solve_info.warnings.append(
                    f"No '{lang}' speaker found among available employees — "
                    f"language requirement on {len(days)} day(s) will be treated as soft."
                )

        # Check staffing capacity
        cafe_emps_count = sum(
            1 for e in self.employees
            if e.role_capability in (RoleCapability.cafe, RoleCapability.both)
            and e.availability_start <= month_start <= e.availability_end
        )
        prod_emps_count = sum(
            1 for e in self.employees
            if e.role_capability in (RoleCapability.production, RoleCapability.both)
            and e.availability_start <= month_start <= e.availability_end
        )
        max_cafe = max((self._demand_map[d].cafe_needed for d in self._days if d in self._demand_map), default=0)
        max_prod = max((self._demand_map[d].production_needed for d in self._days if d in self._demand_map), default=0)
        if cafe_emps_count < max_cafe:
            self.solve_info.warnings.append(
                f"Peak café demand is {max_cafe} but only {cafe_emps_count} café-capable "
                "employee(s) are available at month start — may be infeasible."
            )
        if prod_emps_count < max_prod:
            self.solve_info.warnings.append(
                f"Peak production demand is {max_prod} but only {prod_emps_count} production-capable "
                "employee(s) are available at month start — may be infeasible."
            )

    def _add_hard_constraints(self) -> None:
        skeleton = getattr(self, "_skeleton_mode", False)

        add_one_shift_per_day(self.model, self.variables, self.employees, self.shifts, self._days)
        add_daily_staffing_requirements(
            self.model, self.variables, self._demand_map,
            self.employees, self.shifts, self._days,
        )
        # Section 2 + 8: use worked_hours coefficients; cap adults at 37.5h/week
        add_weekly_hour_limits(self.model, self.variables, self.employees, self.shifts, self._days)
        add_daily_rest(self.model, self.variables, self.employees, self.shifts, self._days)
        # Rolling 7-day ≤ 6 (35h weekly rest)
        add_weekly_rest(self.model, self.variables, self.employees, self.shifts, self._days)
        # Constraint 1: explicit Mon–Sun calendar-week cap
        add_max_days_per_calendar_week(
            self.model, self.variables, self.employees, self.shifts, self._days
        )
        # Constraint 2a: explicit consecutive-days cap within month (gap-aware rolling 7-day windows)
        add_max_consecutive_working_days(
            self.model, self.variables, self.employees, self.shifts, self._days
        )
        # Constraint 2b: cross-month consecutive-days constraint
        n_cross = add_cross_month_consecutive_constraint(
            self.model, self.variables, self.employees, self.shifts,
            self._days, self._prev_month_working,
        )
        logger.info("Cross-month consecutive-day constraints added: %d", n_cross)

        if not skeleton:
            # Constraint 3: ≥2 consecutive days off in every rolling 14-day window
            # (dropped in skeleton mode — too heavy, not absolute legal minimum)
            add_two_consecutive_days_off_per_14(
                self.model, self.variables, self.employees, self.shifts, self._days
            )

        add_role_capability(self.model, self.variables, self.employees, self.shifts)
        add_availability(self.model, self.variables, self.employees, self._days)
        add_eidsdal_transport_constraints(
            self.model, self.variables, self.employees, self.shifts, self._days
        )
        # Section 3: age-based shift restrictions
        add_age_based_constraints(
            self.model, self.variables, self.employees, self.shifts, self._days
        )

        if not skeleton:
            # Section 7: Sunday/holiday rest rules (soft-ish legal requirement; dropped
            # in skeleton mode to maximise feasibility)
            add_sunday_rest_constraints(
                self.model, self.variables, self.employees, self.shifts, self._days
            )

        settings_list = self.settings if isinstance(self.settings, list) else (
            [self.settings] if self.settings else []
        )

        if not skeleton:
            # Section 6: maximum staffing caps per day
            max_cafe = min((s.max_cafe_per_day for s in settings_list), default=5)
            max_prod = min((s.max_prod_per_day for s in settings_list), default=4)
            add_max_staffing_caps(
                self.model, self.variables, self._demand_map,
                self.employees, self.shifts, self._days,
                default_max_cafe=max_cafe,
                default_max_prod=max_prod,
            )

        # Opening hours coverage is ALWAYS enforced — "never relax" category alongside
        # legal rest periods and consecutive-day caps.  Even in skeleton mode the café
        # must cover every 1-hour slot from opening_time to closing_time with ≥1
        # employee.  The constraint already guards against empty cov_vars per slot so
        # it stays feasible when headcount is at its absolute minimum.
        add_opening_hours_coverage(
            self.model, self.variables, self.employees, self.shifts,
            self._days, self._demand_map, settings_list,
        )

    def _add_skeleton_objective(self) -> None:
        """Skeleton mode objective: maximise total working assignments.

        Opening hours coverage is enforced as a hard constraint even in skeleton mode,
        so the solver must cover every operating-hour slot.  This objective then fills
        remaining capacity to give employees as many shifts as possible (contracted
        hours) rather than the previous minimise-assignments approach that left
        employees severely under-rostered.
        """
        all_vars = list(self.variables.values())
        if all_vars:
            self.model.Maximize(sum(all_vars))

    def _add_soft_constraints(self) -> None:
        add_soft_constraints(
            self.model, self.variables, self.employees, self.shifts,
            self._days, self._demand_map,
            disable_both_preference=getattr(self, "_disable_both_preference", False),
        )

    @staticmethod
    def _load_prev_month_working(year: int, month: int) -> dict[str, set[date]]:
        """Load the previous month's working dates per employee from the DB.

        Returns a dict of str(employee_id) → set[date] (working days only, no day-offs).
        Returns {} silently if no previous schedule exists or the DB is unavailable.
        """
        try:
            from src.db.database import db_session
            from src.models.schedule import ScheduleORM

            prev_month = month - 1
            prev_year = year
            if prev_month == 0:
                prev_month = 12
                prev_year -= 1

            with db_session() as db:
                # Prefer the approved version; fall back to latest non-archived draft
                orm = (
                    db.query(ScheduleORM)
                    .filter_by(year=prev_year, month=prev_month, status="approved")
                    .order_by(ScheduleORM.version.desc())
                    .first()
                )
                if orm is None:
                    orm = (
                        db.query(ScheduleORM)
                        .filter(
                            ScheduleORM.year == prev_year,
                            ScheduleORM.month == prev_month,
                            ScheduleORM.status != "archived",
                        )
                        .order_by(ScheduleORM.version.desc())
                        .first()
                    )
                if not orm:
                    logger.info(
                        "No saved schedule for %d-%02d — cross-month carry-in assumed 0 for all employees.",
                        prev_year, prev_month,
                    )
                    return {}

                result: dict[str, set[date]] = {}
                for a in orm.assignments:
                    if not a.is_day_off:
                        result.setdefault(str(a.employee_id), set()).add(a.date)
                logger.info(
                    "Loaded previous month (%d-%02d): %d employee(s) with working assignments.",
                    prev_year, prev_month, len(result),
                )
                return result
        except Exception as exc:
            logger.warning("Could not load previous month schedule: %s", exc)
            return {}

    @staticmethod
    def _load_settings() -> list[EstablishmentSettingsRead]:
        """Load all EstablishmentSettings rows from DB. Returns empty list on error."""
        try:
            from src.db.database import db_session
            from src.models.establishment import EstablishmentSettingsORM
            from src.models.enums import Season as SeasonEnum

            with db_session() as db:
                rows = db.query(EstablishmentSettingsORM).all()
                return [
                    EstablishmentSettingsRead(
                        id=r.id,
                        season=SeasonEnum(r.season),
                        date_range_start=r.date_range_start,
                        date_range_end=r.date_range_end,
                        opening_time=r.opening_time,
                        closing_time=r.closing_time,
                        production_start=r.production_start,
                        max_cafe_per_day=getattr(r, "max_cafe_per_day", 5) or 5,
                        max_prod_per_day=getattr(r, "max_prod_per_day", 4) or 4,
                    )
                    for r in rows
                ]
        except Exception as exc:
            logger.warning("Could not load EstablishmentSettings: %s", exc)
            return []

    # ── Diagnostics (private) ─────────────────────────────────────────────────

    def _count_available_employees(self) -> int:
        if not self._days:
            return 0
        month_start, month_end = self._days[0], self._days[-1]
        return sum(
            1 for e in self.employees
            if not (e.availability_end < month_start or e.availability_start > month_end)
        )

    def _availability_summary(self) -> str:
        if not self.employees:
            return "no employees"
        starts = [e.availability_start for e in self.employees]
        ends = [e.availability_end for e in self.employees]
        return f"{min(starts)} – {max(ends)}"

    def _generate_infeasibility_hints(self) -> list[str]:
        hints = []
        if not self._days:
            return ["No demand days — is the selected month within the operating season (May–Oct)?"]

        month_start = self._days[0]

        # Check language coverage
        for d in self._days:
            dd = self._demand_map.get(d)
            if not dd:
                continue
            for lang in dd.languages_required:
                speakers_on_day = [
                    e for e in self.employees
                    if any(l.lower().strip() == lang for l in e.languages)
                    and (e.id, d, next(
                        (s.id for s in self.shifts if s.role == ShiftRole.cafe), None
                    )) in self.variables
                ]
                if not speakers_on_day:
                    hints.append(
                        f"{d}: no '{lang}' speaker available for café — "
                        "add a speaker or make language matching soft."
                    )

        # Summarise production gap
        prod_emps = [
            e for e in self.employees
            if e.role_capability in (RoleCapability.production, RoleCapability.both)
            and e.availability_start <= month_start <= e.availability_end
        ]
        max_prod = max(
            (self._demand_map[d].production_needed for d in self._days if d in self._demand_map),
            default=0,
        )
        if len(prod_emps) < max_prod:
            hints.append(
                f"Production shortfall: peak demand={max_prod} but only "
                f"{len(prod_emps)} production-capable employee(s) available."
            )

        # Check for cafe/production dual-role conflict: a day where the sum of
        # café_needed + prod_needed exceeds available workers, because the only
        # production-capable employees are also the only way to hit café minimum.
        for d in self._days:
            dd = self._demand_map.get(d)
            if not dd or (dd.cafe_needed == 0 and dd.production_needed == 0):
                continue
            avail = [
                e for e in self.employees
                if e.availability_start <= d <= e.availability_end
            ]
            n_cafe_capable = sum(
                1 for e in avail
                if e.role_capability in (RoleCapability.cafe, RoleCapability.both)
            )
            n_prod_capable = sum(
                1 for e in avail
                if e.role_capability in (RoleCapability.production, RoleCapability.both)
            )
            n_total = len(avail)
            # If café_needed + prod_needed > total workers, impossible by pigeonhole
            if n_total < dd.cafe_needed + dd.production_needed:
                hints.append(
                    f"{d}: impossible — need {dd.cafe_needed} café + "
                    f"{dd.production_needed} production = {dd.cafe_needed + dd.production_needed} workers "
                    f"but only {n_total} employee(s) available. "
                    "Close this day or reduce staffing rules for this season."
                )

        if not hints:
            hints.append(
                "Constraints conflict — most likely weekly rest + staffing minimums + "
                "limited employees over a full month. Consider reducing staffing minimums "
                "or adding more employees."
            )
        return hints

    # ── Solution extraction (private) ────────────────────────────────────────

    def _extract_schedule(self, solver: cp_model.CpSolver) -> ScheduleRead:
        schedule_id = uuid.uuid4()
        assignments: list[AssignmentRead] = []
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
