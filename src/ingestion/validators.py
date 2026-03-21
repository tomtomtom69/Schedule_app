"""
Cross-record validation logic.
These run after individual Pydantic validation, checking business rules
that span multiple records.
"""
from datetime import date

from src.models.cruise_ship import CruiseShipCreate
from src.models.employee import EmployeeCreate

SEASON_START = date(2000, 5, 1)
SEASON_END = date(2000, 10, 15)


ValidationWarnings = list[str]


def validate_employee_list(employees: list[EmployeeCreate]) -> ValidationWarnings:
    """
    Cross-record employee validation.
    Returns a list of warning messages (empty = all good).
    """
    warnings: ValidationWarnings = []

    # Check for duplicate names
    names = [e.name for e in employees]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        warnings.append(f"Duplicate employee names detected: {', '.join(sorted(duplicates))}")

    # Check that at least one Eidsdal employee has a driving licence
    eidsdal_employees = [e for e in employees if e.housing == "eidsdal"]
    if eidsdal_employees:
        drivers = [e for e in eidsdal_employees if e.driving_licence]
        if not drivers:
            warnings.append(
                "No Eidsdal employee has a driving licence. "
                "At least one driver is required per car for transport."
            )

    return warnings


def validate_cruise_schedule(ships: list[CruiseShipCreate]) -> ValidationWarnings:
    """
    Cross-record cruise schedule validation.
    Returns a list of warning messages.
    """
    warnings: ValidationWarnings = []

    for ship in ships:
        d = ship.date
        season_start = date(d.year, 5, 1)
        season_end = date(d.year, 10, 15)
        if not (season_start <= d <= season_end):
            warnings.append(
                f"Ship '{ship.ship_name}' on {d} is outside the operating season (May 1 – Oct 15)."
            )

    return warnings


def validate_language_coverage(
    employees: list[EmployeeCreate],
    ships: list[CruiseShipCreate],
) -> ValidationWarnings:
    """
    Warn if any ship requires a language that no employee speaks.
    """
    warnings: ValidationWarnings = []

    all_employee_languages: set[str] = set()
    for emp in employees:
        for lang in emp.languages:
            all_employee_languages.add(lang.lower().strip())

    ship_languages: set[str] = set()
    for ship in ships:
        if ship.extra_language:
            ship_languages.add(ship.extra_language.lower().strip())

    for lang in ship_languages:
        if lang not in all_employee_languages and lang != "english":
            warnings.append(
                f"No employee speaks '{lang}', but cruise ships require it. "
                "Language coverage constraint may not be satisfiable."
            )

    return warnings
