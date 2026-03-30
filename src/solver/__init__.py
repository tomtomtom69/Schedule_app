"""Schedule solver — Phase 3."""
from src.solver.scheduler import ScheduleGenerator
from src.solver.validator import Violation, validate_schedule
from src.solver.fallback import FallbackResult, StaffingGap, run_fallback_solve

__all__ = [
    "ScheduleGenerator", "validate_schedule", "Violation",
    "FallbackResult", "StaffingGap", "run_fallback_solve",
]
