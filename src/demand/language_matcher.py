"""Language matcher — Phase 2."""
from src.models.cruise_ship import CruiseShipRead
from src.models.employee import EmployeeRead


def get_required_languages(
    ships_on_date: list[CruiseShipRead],
    ship_language_map: dict[str, str],  # ship_name → language
) -> list[str]:
    """Determine which extra languages are needed on a given day.

    1. For each ship in port, use ship.extra_language if set;
       otherwise look up in ship_language_map.
    2. Filter out 'english' (spoken by everyone).
    3. Return deduplicated, sorted list of required languages.
    """
    langs: set[str] = set()
    for ship in ships_on_date:
        if ship.extra_language:
            lang = ship.extra_language.lower().strip()
        else:
            lang = ship_language_map.get(ship.ship_name, "").lower().strip()
        if lang and lang != "english":
            langs.add(lang)
    return sorted(langs)


def check_language_coverage(
    required_languages: list[str],
    available_employees: list[EmployeeRead],
) -> dict[str, bool]:
    """Check whether at least one available employee speaks each required language.

    Returns e.g. {"spanish": True, "german": False}.
    """
    all_spoken: set[str] = set()
    for emp in available_employees:
        for lang in emp.languages:
            all_spoken.add(lang.lower().strip())

    return {lang: (lang in all_spoken) for lang in required_languages}
