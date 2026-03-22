"""Language matcher — Phase 2."""
from src.models.cruise_ship import CruiseShipRead
from src.models.employee import EmployeeRead


def get_required_languages(
    ships_on_date: list[CruiseShipRead],
) -> list[str]:
    """Determine which extra languages are needed on a given day.

    Reads extra_language directly from each ship record (may be a
    comma-separated string like 'italian,spanish').
    Filters out 'english' (spoken by everyone).
    Returns a deduplicated, sorted list of required languages.
    """
    langs: set[str] = set()
    for ship in ships_on_date:
        if ship.extra_language:
            for lang in ship.extra_language.split(","):
                lang = lang.strip()
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
