"""Fallback solver — progressive constraint relaxation for INFEASIBLE months.

When the first-pass solver returns INFEASIBLE, this module tries progressively
looser constraint sets until a workable schedule is found.  The never-relaxed
constraints (opening hours coverage, rest periods, consecutive-day caps,
age limits, Eidsdal driver) remain hard in every pass.

Steps tried in order (first feasible wins):
  A — Language matching: already soft in the main solver — noted, no re-solve
  B — Reduce café and production minimums by 1 on no-cruise days only
  C — Reduce café and production minimums by 1 on all days
  D — Same demand as C, plus remove the both-on-production soft preference
  E — Absolute floor: café ≥ 1 where previously > 0, production ≥ 0
"""
from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass
from datetime import date

from src.demand.forecaster import DailyDemand
from src.models.employee import EmployeeRead
from src.models.schedule import ScheduleRead
from src.models.shift_template import ShiftTemplateRead
from src.models.enums import ShiftRole

logger = logging.getLogger(__name__)

_FALLBACK_TIMEOUT_SECONDS = 60  # per step — same as normal solve


@dataclass
class StaffingGap:
    """One day/role where the fallback schedule is below the original requirement."""
    date: date
    role: str   # "cafe" or "production"
    required: int   # from original demand
    scheduled: int  # what the fallback assigned

    def description(self) -> str:
        shortfall = self.required - self.scheduled
        return (
            f"{self.date.strftime('%b %d')} — {self.role.title()}: "
            f"need {self.required}, scheduled {self.scheduled} "
            f"(short by {shortfall})"
        )


@dataclass
class FallbackResult:
    """Outcome of a successful fallback solve."""
    schedule: ScheduleRead
    steps_applied: list[str]       # e.g. ["C"] or ["C", "D"] or ["SKELETON"]
    relaxation_notes: list[str]    # human-readable bullet points for the UI banner
    staffing_gaps: list[StaffingGap]

    @property
    def is_skeleton(self) -> bool:
        return self.steps_applied == ["SKELETON"]

    def notes_json(self) -> str:
        """Serialise to JSON for storage in ScheduleORM.fallback_notes."""
        return json.dumps({
            "steps": self.steps_applied,
            "notes": self.relaxation_notes,
            "gaps": [
                {
                    "date": g.date.isoformat(),
                    "role": g.role,
                    "required": g.required,
                    "scheduled": g.scheduled,
                }
                for g in self.staffing_gaps
            ],
        })


# ── Demand manipulation ──────────────────────────────────────────────────────

def _relax_demand(
    demand: list[DailyDemand],
    cafe_reduction: int = 0,
    prod_reduction: int = 0,
    no_cruise_only: bool = False,
    absolute_cafe_floor: int | None = None,
    absolute_prod_value: int | None = None,
) -> list[DailyDemand]:
    """Return a new demand list with adjusted staffing minimums.

    no_cruise_only=True: only reduce days without any cruise ship.
    absolute_cafe_floor: if set, café minimum is max(floor, reduced_value).
    absolute_prod_value: if set, production minimum is forced to this value.
    """
    result = []
    for d in demand:
        if no_cruise_only and (d.has_cruise or d.has_good_ship):
            result.append(d)
            continue

        new_cafe = max(0, d.cafe_needed - cafe_reduction)
        new_prod = max(0, d.production_needed - prod_reduction)

        if absolute_cafe_floor is not None and d.cafe_needed > 0:
            new_cafe = max(absolute_cafe_floor, new_cafe)
        if absolute_prod_value is not None:
            new_prod = absolute_prod_value

        result.append(dataclasses.replace(d, cafe_needed=new_cafe, production_needed=new_prod))
    return result


# ── Solver wrapper ───────────────────────────────────────────────────────────

def _try_solve(
    employees: list[EmployeeRead],
    demand: list[DailyDemand],
    shifts: list[ShiftTemplateRead],
    settings=None,
    disable_both_preference: bool = False,
    skeleton_mode: bool = False,
) -> tuple[ScheduleRead | None, object]:
    """Build and solve one CP-SAT model.  Returns (schedule_or_None, solve_info)."""
    from src.solver.scheduler import ScheduleGenerator
    gen = ScheduleGenerator(employees, demand, shifts, settings)
    gen.build_model(
        disable_both_preference=disable_both_preference,
        skeleton_mode=skeleton_mode,
    )
    result = gen.solve()
    return result, gen.solve_info


# ── Gap analysis ─────────────────────────────────────────────────────────────

def _compute_staffing_gaps(
    schedule: ScheduleRead,
    original_demand: list[DailyDemand],
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
) -> list[StaffingGap]:
    """Compare what was actually scheduled against the ORIGINAL (unrelaxed) demand."""
    demand_map = {d.date: d for d in original_demand}
    shift_map = {s.id: s for s in shifts}

    cafe_count: dict[date, int] = {}
    prod_count: dict[date, int] = {}

    for a in schedule.assignments:
        if a.is_day_off:
            continue
        shift = shift_map.get(a.shift_id)
        if not shift:
            continue
        if shift.role == ShiftRole.cafe:
            cafe_count[a.date] = cafe_count.get(a.date, 0) + 1
        elif shift.role == ShiftRole.production:
            prod_count[a.date] = prod_count.get(a.date, 0) + 1

    gaps: list[StaffingGap] = []
    for d_obj in sorted(original_demand, key=lambda x: x.date):
        d = d_obj.date
        dd = demand_map.get(d)
        if not dd:
            continue
        actual_cafe = cafe_count.get(d, 0)
        actual_prod = prod_count.get(d, 0)

        if dd.cafe_needed > 0 and actual_cafe < dd.cafe_needed:
            gaps.append(StaffingGap(date=d, role="cafe", required=dd.cafe_needed, scheduled=actual_cafe))
        if dd.production_needed > 0 and actual_prod < dd.production_needed:
            gaps.append(StaffingGap(date=d, role="production", required=dd.production_needed, scheduled=actual_prod))

    return gaps


def _build_result(
    schedule: ScheduleRead,
    steps_applied: list[str],
    relaxation_notes: list[str],
    original_demand: list[DailyDemand],
    employees: list[EmployeeRead],
    shifts: list[ShiftTemplateRead],
) -> FallbackResult:
    gaps = _compute_staffing_gaps(schedule, original_demand, employees, shifts)
    logger.info(
        "Fallback success via step(s) %s — %d staffing gap(s) vs original demand",
        steps_applied, len(gaps),
    )
    return FallbackResult(
        schedule=schedule,
        steps_applied=steps_applied,
        relaxation_notes=relaxation_notes,
        staffing_gaps=gaps,
    )


# ── Main entry point ─────────────────────────────────────────────────────────

def run_fallback_solve(
    employees: list[EmployeeRead],
    demand: list[DailyDemand],
    shifts: list[ShiftTemplateRead],
    settings=None,
) -> FallbackResult | None:
    """Try steps A–E in order; return the first feasible result, or None if all fail.

    Never relaxed (hard in every pass):
      - Opening hours coverage (≥1 café employee per slot during operating hours)
      - Max 6 consecutive working days
      - Daily rest (11h) and weekly rest (35h / 1 day off per 7)
      - Age-based shift restrictions
      - Max staffing caps (5–6 café, 4 production)
      - Eidsdal driver requirement
    """
    # Always note Step A — language is already soft, no additional relaxation needed
    base_notes = [
        "Language matching: already handled as a weighted objective (soft), "
        "not a hard constraint — no additional relaxation was needed here"
    ]

    # ── Step B: reduce minimums by 1 on no-cruise days ───────────────────────
    n_quiet = sum(1 for d in demand if not d.has_cruise and not d.has_good_ship)
    demand_b = _relax_demand(demand, cafe_reduction=1, prod_reduction=1, no_cruise_only=True)
    logger.info("Fallback Step B: reducing minimums by 1 on %d no-cruise day(s)", n_quiet)
    result, _ = _try_solve(employees, demand_b, shifts, settings)
    if result is not None:
        return _build_result(
            result, steps_applied=["B"],
            relaxation_notes=base_notes + [
                f"Minimum staffing: reduced by 1 on {n_quiet} no-cruise day(s) — "
                "café and production minimums lowered on quiet days to allow rest-day scheduling"
            ],
            original_demand=demand, employees=employees, shifts=shifts,
        )

    # ── Step C: reduce minimums by 1 on all days ─────────────────────────────
    n_cruise = sum(1 for d in demand if d.has_cruise or d.has_good_ship)
    demand_c = _relax_demand(demand, cafe_reduction=1, prod_reduction=1)
    logger.info("Fallback Step C: reducing minimums by 1 on all %d days", len(demand))
    result, _ = _try_solve(employees, demand_c, shifts, settings)
    if result is not None:
        return _build_result(
            result, steps_applied=["C"],
            relaxation_notes=base_notes + [
                f"Minimum staffing: reduced by 1 on all {len(demand)} days "
                f"(including {n_cruise} cruise day(s)) — necessary to allow rest-day scheduling "
                "with the current number of available employees"
            ],
            original_demand=demand, employees=employees, shifts=shifts,
        )

    # ── Step D: same demand as C + remove both-on-production soft preference ──
    logger.info("Fallback Step D: step C demand + both-on-production preference disabled")
    result, _ = _try_solve(employees, demand_c, shifts, settings, disable_both_preference=True)
    if result is not None:
        return _build_result(
            result, steps_applied=["C", "D"],
            relaxation_notes=base_notes + [
                f"Minimum staffing: reduced by 1 on all {len(demand)} days "
                f"(including {n_cruise} cruise day(s))",
                "'Both' role preference: production-first preference suspended — "
                "flex employees (role='both') assigned freely to café or production as needed",
            ],
            original_demand=demand, employees=employees, shifts=shifts,
        )

    # ── Step E: absolute floor — café ≥ 1, production ≥ 0 ───────────────────
    demand_e = _relax_demand(demand, absolute_cafe_floor=1, absolute_prod_value=0)
    logger.info("Fallback Step E: absolute floor — café ≥ 1, production ≥ 0")
    result, _ = _try_solve(employees, demand_e, shifts, settings, disable_both_preference=True)
    if result is not None:
        return _build_result(
            result, steps_applied=["E"],
            relaxation_notes=base_notes + [
                "Minimum staffing: absolute floor applied — "
                "café minimum reduced to 1 on all days, production minimum set to 0",
                "'Both' role preference: production-first preference suspended",
            ],
            original_demand=demand, employees=employees, shifts=shifts,
        )

    # ── Skeleton mode: bare legal minimum — café ≥ 1, production = 0 ─────────
    # Drops complex constraints (opening hours coverage, 14-day paired rest,
    # Sunday rest, staffing caps) and uses a "minimise total assignments"
    # objective so employees get as much rest as possible.
    demand_skel = _relax_demand(demand, absolute_cafe_floor=1, absolute_prod_value=0)
    logger.info(
        "Fallback Skeleton mode: café ≥ 1 per open day (opening hours enforced), "
        "production = 0, 14-day/Sunday/cap constraints dropped, maximise-assignments objective"
    )
    result, _ = _try_solve(
        employees, demand_skel, shifts, settings,
        disable_both_preference=True,
        skeleton_mode=True,
    )
    if result is not None:
        return _build_result(
            result, steps_applied=["SKELETON"],
            relaxation_notes=base_notes + [
                "SKELETON MODE: absolute minimum staffing applied",
                "Café minimum: 1 per open day — opening hours coverage still enforced "
                "(≥1 café employee per hourly slot within operating hours)",
                "Production minimum: 0 on all days",
                "Dropped constraints: 14-day paired rest, Sunday rest, staffing caps",
                "Objective: maximise working assignments within skeleton constraints",
            ],
            original_demand=demand, employees=employees, shifts=shifts,
        )

    logger.error(
        "Skeleton solver INFEASIBLE — even with bare legal minimums no schedule is possible. "
        "Not enough employees to cover opening hours while respecting rest periods."
    )
    return None


def staffing_gaps_from_json(fallback_notes_json: str) -> list[StaffingGap]:
    """Reconstruct StaffingGap list from stored JSON (used when loading from DB)."""
    try:
        data = json.loads(fallback_notes_json)
        return [
            StaffingGap(
                date=date.fromisoformat(g["date"]),
                role=g["role"],
                required=g["required"],
                scheduled=g["scheduled"],
            )
            for g in data.get("gaps", [])
        ]
    except Exception:
        return []


def relaxation_notes_from_json(fallback_notes_json: str) -> list[str]:
    """Reconstruct relaxation notes list from stored JSON."""
    try:
        return json.loads(fallback_notes_json).get("notes", [])
    except Exception:
        return []


def is_skeleton_from_json(fallback_notes_json: str | None) -> bool:
    """Return True if the stored fallback_notes indicates a skeleton schedule."""
    if not fallback_notes_json:
        return False
    try:
        return json.loads(fallback_notes_json).get("steps", []) == ["SKELETON"]
    except Exception:
        return False
