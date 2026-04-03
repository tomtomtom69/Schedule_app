"""
CSV/XLS ingestion: parse uploaded files and validate each row with Pydantic.
Accepts both .csv and .xlsx formats.

Supports two cruise-ship formats:
  1. Original spec format  — English headers: ship_name, date, arrival_time, …
  2. Norwegian Excel format — Headers: Fartøy, Ankomst, Avgang, Kai, Bruttotonn, Språk, Good ship
     Auto-detected from column names.
"""
import re
from collections import defaultdict
from datetime import date, datetime, time
from typing import Union

import pandas as pd
from pydantic import ValidationError

from src.models.cruise_ship import CruiseShipCreate
from src.models.employee import EmployeeCreate


ParseResult = tuple[list, list[dict]]             # (valid_records, errors)
EmployeeParseResult = tuple[list, list[dict], list[str]]  # adds per-row correction notes


# ── Generic helpers ───────────────────────────────────────────────────────────

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


# ── Norwegian cruise-ship format constants ────────────────────────────────────

_NORWEGIAN_MARKERS = {"fartøy", "fartoy", "ankomst", "avgang", "kai", "bruttotonn"}

_KAI_MAP: dict[str, str] = {
    "Pos. 4B/SW": "geiranger_4B_SW",
    "Pos. 3S":    "geiranger_3S",
    "Pos. 2":     "geiranger_2",
    "Hellesylt cruisekai": "hellesylt",
}

_LANG_CODES: dict[str, str] = {
    "g": "german",
    "i": "italian",
    "s": "spanish",
    "f": "french",
}

_GEIRANGER_PORTS = {"geiranger_4B_SW", "geiranger_3S", "geiranger_2"}


# ── Employee normalization constants ──────────────────────────────────────────

# Canonical map: cleaned input → enum string value
_ROLE_MAP: dict[str, str] = {
    "cafe":               "cafe",
    "café":               "cafe",
    "cafe":               "cafe",
    "caf":                "cafe",
    "kafe":               "cafe",
    "kafé":               "cafe",
    "production":         "production",
    "prod":               "production",
    "manager production": "production",
    "manager":            "production",
    "both":               "both",
    "begge":              "both",   # Norwegian
}

_EMPTYPE_MAP: dict[str, str] = {
    "full_time":  "full_time",
    "full-time":  "full_time",
    "full time":  "full_time",
    "fulltime":   "full_time",
    "ft":         "full_time",
    "part_time":  "part_time",
    "part-time":  "part_time",
    "part time":  "part_time",
    "parttime":   "part_time",
    "pt":         "part_time",
}

_MONTH_NAMES: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "may": 5, "mai": 5,              # Norwegian mai
    "jun": 6, "jul": 7, "aug": 8, "sep": 9,
    "okt": 10, "oct": 10,            # Norwegian okt
    "nov": 11, "des": 12, "dec": 12, # Norwegian des
}

_DOB_FORMATS = [
    "%Y-%m-%d",   # 2010-12-01
    "%d-%m-%Y",   # 01-12-2010
    "%d.%m.%Y",   # 01.12.2010
    "%d/%m/%Y",   # 01/12/2010
    "%Y/%m/%d",   # 2010/12/01
    "%d-%b-%Y",   # 01-Dec-2010
    "%d %b %Y",   # 01 Dec 2010
    "%d %B %Y",   # 01 December 2010
]


# ── Employee field normalization ──────────────────────────────────────────────

def _normalize_role(raw: str) -> tuple[str, str | None]:
    """Return (canonical_value, original_before_change_or_None)."""
    stripped = raw.strip()
    # Accent-fold for matching: café → cafe
    clean = stripped.lower().replace("é", "e").replace("è", "e").replace("ê", "e")
    mapped = _ROLE_MAP.get(clean)
    if mapped is None:
        return stripped, None      # unknown — let Pydantic raise the error
    note = stripped if mapped != stripped else None
    return mapped, note


def _normalize_emptype(raw: str) -> tuple[str, str | None]:
    """Return (canonical_value, original_before_change_or_None)."""
    stripped = raw.strip()
    clean = stripped.lower().replace("-", "_").replace(" ", "_")
    mapped = _EMPTYPE_MAP.get(clean) or _EMPTYPE_MAP.get(stripped.lower())
    if mapped is None:
        return stripped, None
    note = stripped if mapped != stripped else None
    return mapped, note


def _normalize_housing(raw: str) -> tuple[str, str | None]:
    """Return (canonical_value, original_before_change_or_None)."""
    stripped = raw.strip()
    clean = stripped.lower()
    if clean in ("geiranger", "eidsdal"):
        note = stripped if clean != stripped else None
        return clean, note
    return stripped, None          # unknown — let Pydantic raise


def _coerce_bool_noted(value) -> tuple[bool, str | None]:
    """
    Parse bool from various representations.
    Returns (bool_value, original_string_if_coerced_else_None).
    """
    if isinstance(value, bool):
        return value, None
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "1", "yes"):
            result = True
        elif s in ("false", "0", "no"):
            result = False
        else:
            result = bool(value.strip())
        canonical = "true" if result else "false"
        note = value.strip() if s != canonical else None
        return result, note
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        result = bool(int(value))
        return result, str(int(value))
    return bool(value), None


def _parse_dob(value) -> date | None:
    """
    Parse date-of-birth from:
      - Python date/datetime/pandas Timestamp
      - Integer or numeric string → treated as age (approximate: Jan 1 of birth year)
      - Date strings: YYYY-MM-DD, DD-MM-YYYY, DD.MM.YYYY, D MMM YYYY, '1 dec 2010', etc.
    Returns None if value is blank or unparseable.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    # Numeric (int/float) not bool → treat as age
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        age = int(value)
        if 10 <= age <= 100:
            return date(date.today().year - age, 1, 1)
        return None

    s = str(value).strip()
    if not s or s.lower() in ("nan", "none", "nat", ""):
        return None

    # Numeric string → age
    if re.match(r"^\d{1,3}$", s):
        age = int(s)
        if 10 <= age <= 100:
            return date(date.today().year - age, 1, 1)

    # Try explicit strptime formats
    for fmt in _DOB_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass

    # "D/DD MMM/MMMM YYYY" with Norwegian month names (case-insensitive)
    m = re.match(r"^(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})$", s)
    if m:
        day = int(m.group(1))
        month_num = _MONTH_NAMES.get(m.group(2).lower()[:3])
        year = int(m.group(3))
        if month_num:
            try:
                return date(year, month_num, day)
            except ValueError:
                pass

    # pandas dayfirst fallback (handles many locale variants)
    try:
        return pd.to_datetime(s, dayfirst=True).date()
    except Exception:
        return None


def _normalize_languages(value) -> tuple[list[str], list[str]]:
    """
    Normalize language list: lowercase, semicolon-or-comma split, auto-add english.
    Returns (normalized_list, list_of_human_readable_corrections).
    """
    if isinstance(value, list):
        raw_langs = [str(l).strip() for l in value if str(l).strip()]
    elif isinstance(value, str):
        # Accept semicolons or commas
        raw_langs = [lang.strip() for lang in re.split(r"[;,]", value) if lang.strip()]
    else:
        raw_langs = []

    corrections: list[str] = []
    normalised: list[str] = []
    for lang in raw_langs:
        lower = lang.lower()
        if lower != lang:
            corrections.append(f"language '{lang}' → '{lower}'")
        normalised.append(lower)

    if "english" not in normalised:
        normalised = ["english"] + normalised
        corrections.append("added 'english'")

    return normalised, corrections


def _is_blank_row(row: pd.Series, name_col: str) -> bool:
    """True if the row's name cell is empty/NaN — treat as a blank row to skip."""
    val = row.get(name_col)
    if val is None:
        return True
    if isinstance(val, float) and pd.isna(val):
        return True
    return str(val).strip() == ""


# ── Norwegian cruise-ship format helpers ──────────────────────────────────────

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
    try:
        return datetime.strptime(s, "%d.%m.%Y %H:%M")
    except ValueError:
        pass
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


def _parse_date_field(value, field_name: str) -> date:
    """
    Parse a date field (e.g. availability_start/end).
    Reuses _parse_dob which tries ISO format first, avoiding dayfirst ambiguity.
    Raises ValueError if the value cannot be parsed.
    """
    d = _parse_dob(value)
    if d is None:
        raise ValueError(f"Cannot parse {field_name!r} from {value!r}")
    return d


def _in_operating_season(d: date) -> bool:
    """True if d is within May 1 – Oct 15 (the operating season)."""
    return date(d.year, 5, 1) <= d <= date(d.year, 10, 15)


# ── Norwegian cruise-ship parser ──────────────────────────────────────────────

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

    candidates: list[dict] = []

    for idx, row in df.iterrows():
        row_num = idx + 2

        kai_raw = row.get(kai_col, "") if kai_col else ""
        if kai_raw is None or (isinstance(kai_raw, float) and pd.isna(kai_raw)):
            continue
        kai_str = str(kai_raw).strip()
        if not kai_str:
            continue

        port = _KAI_MAP.get(kai_str)
        if port is None:
            continue

        ship_name = str(row.get(fartoy_col, "") or "").strip()
        if not ship_name:
            continue

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
        if not _in_operating_season(arrival_date):
            continue

        arrival_time   = ankomst_dt.time()
        departure_time = avgang_dt.time()

        raw_t = row.get(tonn_col) if tonn_col else None
        if raw_t is None or (isinstance(raw_t, float) and pd.isna(raw_t)):
            tonn = ship_tonnage.get(ship_name.upper(), 0.0)
        else:
            try:
                tonn = float(raw_t)
            except (ValueError, TypeError):
                tonn = ship_tonnage.get(ship_name.upper(), 0.0)
        size = "big" if tonn >= 100_000 else "small"

        good_raw = row.get(good_col, "") if good_col else ""
        if good_raw is None or (isinstance(good_raw, float) and pd.isna(good_raw)):
            good_ship = False
        else:
            good_ship = str(good_raw).strip().lower() == "x"

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

def parse_employees_csv(file) -> EmployeeParseResult:
    """
    Parse employees CSV/XLS with tolerant normalization.

    Accepts:
    - 'age' or 'date_of_birth' column (or both; 'date_of_birth' wins)
    - Date formats: YYYY-MM-DD, DD-MM-YYYY, D MMM YYYY, '1 dec 2010', Norwegian months
    - role_capability: 'Cafe'/'café'/'Caf' → 'cafe', 'Production'/'Manager Production' → 'production'
    - employment_type: 'part-time'/'Part-time'/'part_time' → 'part_time'
    - driving_licence: 1/0, true/false, yes/no (case-insensitive)
    - housing: case-insensitive 'Geiranger'/'Eidsdal'
    - languages: comma or semicolon separated; auto-adds 'english'; normalizes to lowercase
    - Blank rows (empty name) are skipped silently

    Returns (valid_records, errors, correction_notes).
    correction_notes is a list of human-readable strings describing auto-corrections made.
    """
    df = _read_file(file)
    df.columns = [c.strip().lower() for c in df.columns]

    # Resolve date-of-birth vs age column (prefer date_of_birth if both present)
    dob_col  = "date_of_birth" if "date_of_birth" in df.columns else None
    age_col  = "age" if "age" in df.columns else None
    dob_source = dob_col or age_col  # which column to read from

    records:     list[EmployeeCreate] = []
    errors:      list[dict] = []
    all_notes:   list[str] = []

    for idx, row in df.iterrows():
        row_num = idx + 2  # 1-indexed with header row

        # Skip blank rows silently
        if _is_blank_row(row, "name"):
            continue

        row_corrections: list[str] = []

        try:
            # ── role_capability ───────────────────────────────────────────────
            role_raw = str(row.get("role_capability", "")).strip()
            role_val, role_note = _normalize_role(role_raw)
            if role_note:
                row_corrections.append(f"role '{role_note}' → '{role_val}'")

            # ── employment_type ───────────────────────────────────────────────
            emptype_raw = str(row.get("employment_type", "")).strip()
            emptype_val, emptype_note = _normalize_emptype(emptype_raw)
            if emptype_note:
                row_corrections.append(f"employment_type '{emptype_note}' → '{emptype_val}'")

            # ── housing ───────────────────────────────────────────────────────
            housing_raw = str(row.get("housing", "")).strip()
            housing_val, housing_note = _normalize_housing(housing_raw)
            if housing_note:
                row_corrections.append(f"housing '{housing_note}' → '{housing_val}'")

            # ── driving_licence ───────────────────────────────────────────────
            licence_val, licence_note = _coerce_bool_noted(row.get("driving_licence", False))
            if licence_note:
                row_corrections.append(f"driving_licence '{licence_note}' → '{'true' if licence_val else 'false'}'")

            # ── languages ────────────────────────────────────────────────────
            lang_val, lang_corrections = _normalize_languages(row.get("languages", ""))
            row_corrections.extend(lang_corrections)

            # ── date_of_birth / age ───────────────────────────────────────────
            dob = None
            if dob_source:
                raw_dob = row.get(dob_source)
                dob = _parse_dob(raw_dob)
                if age_col and not dob_col:
                    # Using 'age' column — note the approximation
                    raw_dob_s = str(raw_dob).strip() if raw_dob is not None else ""
                    if dob and raw_dob_s and not (isinstance(raw_dob, float) and pd.isna(raw_dob)):
                        row_corrections.append(f"age '{raw_dob_s}' → date_of_birth ~{dob}")

            # ── availability dates ────────────────────────────────────────────
            avail_start = _parse_date_field(row.get("availability_start"), "availability_start")
            avail_end   = _parse_date_field(row.get("availability_end"),   "availability_end")

            data = {
                "name":               str(row.get("name", "")).strip(),
                "languages":          lang_val,
                "role_capability":    role_val,
                "employment_type":    emptype_val,
                "contracted_hours":   float(row.get("contracted_hours", 0)),
                "housing":            housing_val,
                "driving_licence":    licence_val,
                "availability_start": avail_start,
                "availability_end":   avail_end,
                "preferences":        None,
                "date_of_birth":      dob,
            }
            records.append(EmployeeCreate(**data))

            if row_corrections:
                name_label = data["name"] or f"row {row_num}"
                all_notes.append(
                    f"Row {row_num} ({name_label}) — Normalized: {', '.join(row_corrections)}"
                )

        except (ValidationError, Exception) as e:
            errors.append({"row": row_num, "error": str(e)})

    return records, errors, all_notes


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
