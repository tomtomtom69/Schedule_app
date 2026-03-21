"""
CSV/XLS ingestion: parse uploaded files and validate each row with Pydantic.
Accepts both .csv and .xlsx formats.
"""
import io
from datetime import date, time
from typing import Union

import pandas as pd
from pydantic import ValidationError

from src.models.cruise_ship import CruiseShipCreate, ShipLanguageCreate
from src.models.employee import EmployeeCreate


ParseResult = tuple[list, list[dict]]   # (valid_records, errors)


def _read_file(file) -> pd.DataFrame:
    """Accept a file-like object or UploadedFile; detect format by name/content."""
    name = getattr(file, "name", "")
    if name.endswith(".xlsx") or name.endswith(".xls"):
        return pd.read_excel(file)
    return pd.read_csv(file)


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def _coerce_languages(value) -> list[str]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [lang.strip() for lang in value.split(",") if lang.strip()]
    return []


def parse_employees_csv(file) -> ParseResult:
    """
    Parse employees CSV/XLS.
    Returns (list[EmployeeCreate], list of error dicts).
    """
    df = _read_file(file)
    df.columns = [c.strip().lower() for c in df.columns]

    records: list[EmployeeCreate] = []
    errors: list[dict] = []

    for idx, row in df.iterrows():
        row_num = idx + 2  # 1-indexed with header
        try:
            data = {
                "name": str(row.get("name", "")).strip(),
                "languages": _coerce_languages(row.get("languages", "")),
                "role_capability": str(row.get("role_capability", "")).strip(),
                "employment_type": str(row.get("employment_type", "")).strip(),
                "contracted_hours": float(row.get("contracted_hours", 0)),
                "housing": str(row.get("housing", "")).strip(),
                "driving_licence": _coerce_bool(row.get("driving_licence", False)),
                "availability_start": pd.to_datetime(row.get("availability_start")).date(),
                "availability_end": pd.to_datetime(row.get("availability_end")).date(),
                "preferences": None,
            }
            records.append(EmployeeCreate(**data))
        except (ValidationError, Exception) as e:
            errors.append({"row": row_num, "error": str(e)})

    return records, errors


def parse_cruise_ships_csv(file) -> ParseResult:
    """
    Parse cruise ships CSV/XLS.
    Returns (list[CruiseShipCreate], list of error dicts).
    """
    df = _read_file(file)
    df.columns = [c.strip().lower() for c in df.columns]

    records: list[CruiseShipCreate] = []
    errors: list[dict] = []

    for idx, row in df.iterrows():
        row_num = idx + 2
        try:
            arrival = pd.to_datetime(str(row.get("arrival_time", "")), format="%H:%M").time()
            departure = pd.to_datetime(str(row.get("departure_time", "")), format="%H:%M").time()
            data = {
                "ship_name": str(row.get("ship_name", "")).strip(),
                "date": pd.to_datetime(row.get("date")).date(),
                "arrival_time": arrival,
                "departure_time": departure,
                "port": str(row.get("port", "")).strip(),
                "size": str(row.get("size", "")).strip(),
                "good_ship": _coerce_bool(row.get("good_ship", False)),
                "extra_language": row.get("extra_language") or None,
            }
            records.append(CruiseShipCreate(**data))
        except (ValidationError, Exception) as e:
            errors.append({"row": row_num, "error": str(e)})

    return records, errors


def parse_ship_languages_csv(file) -> ParseResult:
    """
    Parse ship-language mapping CSV/XLS.
    Returns (list[ShipLanguageCreate], list of error dicts).
    """
    df = _read_file(file)
    df.columns = [c.strip().lower() for c in df.columns]

    records: list[ShipLanguageCreate] = []
    errors: list[dict] = []

    for idx, row in df.iterrows():
        row_num = idx + 2
        try:
            data = {
                "ship_name": str(row.get("ship_name", "")).strip(),
                "primary_language": str(row.get("primary_language", "")).strip(),
            }
            records.append(ShipLanguageCreate(**data))
        except (ValidationError, Exception) as e:
            errors.append({"row": row_num, "error": str(e)})

    return records, errors
