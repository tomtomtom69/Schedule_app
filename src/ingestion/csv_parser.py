"""
CSV/XLS ingestion: parse uploaded files and validate each row with Pydantic.
Accepts both .csv and .xlsx formats.

Supports two cruise-ship formats:
  1. Original spec format  — English headers: ship_name, date, arrival_time, …
  2. Norwegian Excel format — Headers: Fartøy, Ankomst, Avgang, Kai, Bruttotonn, Språk, Good ship
     Auto-detected from column names.
"""
import io
from collections import defaultdict
from datetime import date, datetime, time
from typing import Union

import pandas as pd
from pydantic import ValidationError

from src.models.cruise_ship import CruiseShipCreate
from src.models.employee import EmployeeCreate


ParseResult = tuple[list, list[dict]]   # (valid_records, errors)


# ── Helpers ───────────────────────────────────────────────────────────────────

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


# ── Norwegian format detection & constants ───────────────────────────────────

_NORWEGIAN_MARKERS = {"fartøy", "fartoy", "ankomst", "avgang", "kai", "bruttotonn"}

# Kai column value → Port enum string
_KAI_MAP: dict[str, str] = {
    "Pos. 4B/SW": "geiranger_4B_SW",
    "Pos. 3S":    "geiranger_3S",
    "Pos. 2":     "geiranger_2",
    "Hellesylt cruisekai": "hellesylt",
}

# Språk single-letter codes → language name
_LANG_CODES: dict[str, str] = {
    "g": "german",
    "i": "italian",
    "s": "spanish",
    "f": "french",
}

_GEIRANGER_PORTS = {"geiranger_4B_SW", "geiranger_3S", "geiranger_2"}


def _is_norwegian_format(df: pd.DataFrame) -> bool:
    cols = {c.strip().lower() for c in df.columns}
    return bool(cols & _NORWEGIAN_MARKERS)


def _find_col(df: pd.DataFrame, *candidates: str) -> str | None:
    """Return the first DataFrame column whose normalised name matches a candidate."""
    for c in candidates:
        for col in df.columns:
            if col.strip().lower() == c.lower():
                return col
    return None


def _parse_dt_field(val) -> datetime:
    """
    Parse an Ankomst/Avgang cell value.
    Handles: pandas Timestamp, Python datetime, or string 'DD.MM.YYYY HH:MM'.
    """
    if isinstance(val, datetime):
        return val
    if isinstance(val, pd.Timestamp):
        return val.to_pydatetime()
    s = str(val).strip()
    # Norwegian format
    try:
        return datetime.strptime(s, "%d.%m.%Y %H:%M")
    except ValueError:
        pass
    # ISO / pandas fallback
    return pd.to_datetime(s).to_pydatetime()


def _parse_sprak(val) -> str | None:
    """
    Map a Språk cell to a comma-separated list of named languages.
    e.g. 'i,s' → 'italian,spanish', 'g' → 'german'.
    Returns None for blank / unrecognised codes.
    """
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    raw = str(val).strip()
    if not raw:
        return None
    langs = [
        _LANG_CODES[code.strip().lower()]
        for code in raw.split(",")
        if code.strip().lower() in _LANG_CODES
    ]
    return ",".join(langs) if langs else None


def _in_operating_season(d: date) -> bool:
    """True if d is within May 1 – Oct 15 (the operating season)."""
    return date(d.year, 5, 1) <= d <= date(d.year, 10, 15)


# ── Norwegian format parser ───────────────────────────────────────────────────

def _parse_norwegian_cruise_format(df: pd.DataFrame) -> ParseResult:
    """
    Parse the real Norwegian cruise-ship Excel format.

    Rules:
    - Skip rows where Kai is blank (summary/placeholder rows).
    - Derive date + arrival_time from Ankomst 'DD.MM.YYYY HH:MM'.
    - Derive departure_time from Avgang (time portion only).
    - Derive size: Bruttotonn ≥ 100 000 → 'big', else 'small'.
      If Bruttotonn is blank for a row, fall back to the best known
      tonnage for that ship name across the whole sheet.
    - Good ship: 'x' (case-insensitive) → True, blank → False.
    - extra_language: all recognised Språk codes → comma-separated language names.
    - Deduplicate (ship_name, date): prefer Geiranger port over Hellesylt.
    - Skip dates outside the operating season silently (May 1 – Oct 15).
    """
    fartoy_col   = _find_col(df, "fartøy",    "fartoy")
    sprak_col    = _find_col(df, "språk",     "sprak")
    good_col     = _find_col(df, "good ship")
    ankomst_col  = _find_col(df, "ankomst")
    avgang_col   = _find_col(df, "avgang")
    kai_col      = _find_col(df, "kai")
    tonn_col     = _find_col(df, "bruttotonn")

    # Build per-ship tonnage lookup from any row that has a value
    ship_tonnage: dict[str, float] = {}
    if tonn_col:
        for _, row in df.iterrows():
            name = str(row.get(fartoy_col, "") or "").strip().upper()
            raw_t = row.get(tonn_col)
            if name and raw_t is not None and not (isinstance(raw_t, float) and pd.isna(raw_t)):
                try:
                    t = float(raw_t)
                    if t > 0:
                        ship_tonnage[name] = t
                except (ValueError, TypeError):
                    pass

    # First pass: collect all valid candidate rows
    candidates: list[dict] = []

    for idx, row in df.iterrows():
        row_num = idx + 2

        # Skip rows with no Kai value
        kai_raw = row.get(kai_col, "") if kai_col else ""
        if kai_raw is None or (isinstance(kai_raw, float) and pd.isna(kai_raw)):
            continue
        kai_str = str(kai_raw).strip()
        if not kai_str:
            continue

        port = _KAI_MAP.get(kai_str)
        if port is None:
            continue  # unknown berth — skip silently

        ship_name = str(row.get(fartoy_col, "") or "").strip()
        if not ship_name:
            continue

        # Parse Ankomst / Avgang
        ankomst_raw = row.get(ankomst_col) if ankomst_col else None
        avgang_raw  = row.get(avgang_col)  if avgang_col  else None
        if ankomst_raw is None or (isinstance(ankomst_raw, float) and pd.isna(ankomst_raw)):
            continue
        if avgang_raw is None or (isinstance(avgang_raw, float) and pd.isna(avgang_raw)):
            continue
        try:
            ankomst_dt = _parse_dt_field(ankomst_raw)
            avgang_dt  = _parse_dt_field(avgang_raw)
        except Exception:
            continue

        arrival_date = ankomst_dt.date()

        # Skip dates outside operating season silently
        if not _in_operating_season(arrival_date):
            continue

        arrival_time   = ankomst_dt.time()
        departure_time = avgang_dt.time()

        # Resolve tonnage
        raw_t = row.get(tonn_col) if tonn_col else None
        if raw_t is None or (isinstance(raw_t, float) and pd.isna(raw_t)):
            tonn = ship_tonnage.get(ship_name.upper(), 0.0)
        else:
            try:
                tonn = float(raw_t)
            except (ValueError, TypeError):
                tonn = ship_tonnage.get(ship_name.upper(), 0.0)
        size = "big" if tonn >= 100_000 else "small"

        # Good ship
        good_raw = row.get(good_col, "") if good_col else ""
        if good_raw is None or (isinstance(good_raw, float) and pd.isna(good_raw)):
            good_ship = False
        else:
            good_ship = str(good_raw).strip().lower() == "x"

        # Language
        sprak_raw = row.get(sprak_col) if sprak_col else None
        extra_language = _parse_sprak(sprak_raw)

        candidates.append({
            "ship_name":      ship_name,
            "arrival_date":   arrival_date,
            "port":           port,
            "arrival_time":   arrival_time,
            "departure_time": departure_time,
            "size":           size,
            "good_ship":      good_ship,
            "extra_language": extra_language,
            "_row_num":       row_num,
        })

    # Deduplicate: group by (ship_name, arrival_date), prefer Geiranger over Hellesylt
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for c in candidates:
        groups[(c["ship_name"], c["arrival_date"])].append(c)

    records: list[CruiseShipCreate] = []
    errors:  list[dict] = []

    for (ship_name, arrival_date), group in groups.items():
        geiranger_entries = [c for c in group if c["port"] in _GEIRANGER_PORTS]
        best = (geiranger_entries or group)[0]

        try:
            rec = CruiseShipCreate(
                ship_name      = best["ship_name"],
                date           = best["arrival_date"],
                arrival_time   = best["arrival_time"],
                departure_time = best["departure_time"],
                port           = best["port"],
                size           = best["size"],
                good_ship      = best["good_ship"],
                extra_language = best["extra_language"],
            )
            records.append(rec)
        except (ValidationError, Exception) as e:
            errors.append({"row": best["_row_num"], "error": str(e)})

    return records, errors


# ── Public parsers ────────────────────────────────────────────────────────────

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
            # Optional date_of_birth column
            dob_raw = row.get("date_of_birth") if "date_of_birth" in df.columns else None
            if dob_raw is not None and not (isinstance(dob_raw, float) and pd.isna(dob_raw)):
                try:
                    dob = pd.to_datetime(str(dob_raw)).date()
                except Exception:
                    dob = None
            else:
                dob = None

            data = {
                "name":               str(row.get("name", "")).strip(),
                "languages":          _coerce_languages(row.get("languages", "")),
                "role_capability":    str(row.get("role_capability", "")).strip(),
                "employment_type":    str(row.get("employment_type", "")).strip(),
                "contracted_hours":   float(row.get("contracted_hours", 0)),
                "housing":            str(row.get("housing", "")).strip(),
                "driving_licence":    _coerce_bool(row.get("driving_licence", False)),
                "availability_start": pd.to_datetime(row.get("availability_start")).date(),
                "availability_end":   pd.to_datetime(row.get("availability_end")).date(),
                "preferences":        None,
                "date_of_birth":      dob,
            }
            records.append(EmployeeCreate(**data))
        except (ValidationError, Exception) as e:
            errors.append({"row": row_num, "error": str(e)})

    return records, errors


def parse_cruise_ships_csv(file) -> ParseResult:
    """
    Parse cruise ships CSV/XLS.

    Auto-detects format:
      - Norwegian Excel (Fartøy / Ankomst / Kai / Bruttotonn …) → _parse_norwegian_cruise_format
      - Original spec CSV  (ship_name / date / arrival_time …)  → original row-by-row parser

    Returns (list[CruiseShipCreate], list of error dicts).
    """
    df = _read_file(file)

    if _is_norwegian_format(df):
        return _parse_norwegian_cruise_format(df)

    # ── Original spec format ──────────────────────────────────────────────────
    df.columns = [c.strip().lower() for c in df.columns]

    records: list[CruiseShipCreate] = []
    errors: list[dict] = []

    for idx, row in df.iterrows():
        row_num = idx + 2
        try:
            arrival   = pd.to_datetime(str(row.get("arrival_time",   "")), format="%H:%M").time()
            departure = pd.to_datetime(str(row.get("departure_time", "")), format="%H:%M").time()
            data = {
                "ship_name":      str(row.get("ship_name", "")).strip(),
                "date":           pd.to_datetime(row.get("date")).date(),
                "arrival_time":   arrival,
                "departure_time": departure,
                "port":           str(row.get("port", "")).strip(),
                "size":           str(row.get("size", "")).strip(),
                "good_ship":      _coerce_bool(row.get("good_ship", False)),
                "extra_language": row.get("extra_language") or None,
            }
            records.append(CruiseShipCreate(**data))
        except (ValidationError, Exception) as e:
            errors.append({"row": row_num, "error": str(e)})

    return records, errors


