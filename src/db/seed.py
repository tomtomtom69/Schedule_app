import logging
from datetime import date, time

from src.db.database import db_session
from src.models.establishment import EstablishmentSettingsORM
from src.models.shift_template import ShiftTemplateORM

logger = logging.getLogger(__name__)

DEFAULT_SHIFTS = [
    # Café shifts
    {"id": "1",  "role": "cafe",       "label": "VAKT SHOP 1", "start_time": time(8, 0),  "end_time": time(16, 0)},
    {"id": "2",  "role": "cafe",       "label": "VAKT SHOP 2", "start_time": time(9, 30), "end_time": time(17, 30)},
    {"id": "3",  "role": "cafe",       "label": "VAKT SHOP 3", "start_time": time(11, 0), "end_time": time(19, 0)},
    {"id": "4",  "role": "cafe",       "label": "VAKT SHOP 4", "start_time": time(12, 0), "end_time": time(20, 0)},
    {"id": "5",  "role": "cafe",       "label": "VAKT SHOP 5", "start_time": time(13, 0), "end_time": time(21, 0)},
    {"id": "6",  "role": "cafe",       "label": "VAKT SHOP 6", "start_time": time(10, 0), "end_time": time(17, 0)},
    # Production shifts
    {"id": "P1", "role": "production", "label": "PROD 1",      "start_time": time(8, 0),  "end_time": time(16, 0)},
    {"id": "P2", "role": "production", "label": "PROD 2",      "start_time": time(9, 30), "end_time": time(17, 30)},
    {"id": "P3", "role": "production", "label": "PROD 3",      "start_time": time(11, 0), "end_time": time(19, 0)},
    {"id": "P4", "role": "production", "label": "PROD 4",      "start_time": time(12, 0), "end_time": time(20, 0)},
    {"id": "P5", "role": "production", "label": "PROD 5",      "start_time": time(13, 0), "end_time": time(21, 0)},
]

DEFAULT_SEASONS = [
    {
        "season": "low",
        "date_range_start": date(2026, 5, 1),
        "date_range_end": date(2026, 5, 31),
        "opening_time": time(10, 0),
        "closing_time": time(17, 0),
        "production_start": time(8, 0),
        "max_cafe_per_day": 5,
        "max_prod_per_day": 4,
    },
    {
        "season": "mid",
        "date_range_start": date(2026, 6, 1),
        "date_range_end": date(2026, 6, 15),
        "opening_time": time(9, 0),
        "closing_time": time(18, 0),
        "production_start": time(8, 0),
        "max_cafe_per_day": 5,
        "max_prod_per_day": 4,
    },
    {
        "season": "peak",
        "date_range_start": date(2026, 6, 16),
        "date_range_end": date(2026, 8, 31),
        "opening_time": time(8, 30),
        "closing_time": time(20, 15),
        "production_start": time(8, 0),
        "max_cafe_per_day": 5,
        "max_prod_per_day": 4,
    },
    {
        "season": "low",
        "date_range_start": date(2026, 9, 1),
        "date_range_end": date(2026, 10, 15),
        "opening_time": time(10, 0),
        "closing_time": time(18, 0),
        "production_start": time(8, 0),
        "max_cafe_per_day": 5,
        "max_prod_per_day": 4,
    },
]


def seed_shift_templates() -> None:
    with db_session() as db:
        count = db.query(ShiftTemplateORM).count()
        if count > 0:
            logger.info("Shift templates already seeded (%d rows), skipping.", count)
            return
        for shift in DEFAULT_SHIFTS:
            db.add(ShiftTemplateORM(**shift))
        logger.info("Seeded %d shift templates.", len(DEFAULT_SHIFTS))


def seed_season_settings() -> None:
    with db_session() as db:
        count = db.query(EstablishmentSettingsORM).count()
        if count > 0:
            logger.info("Season settings already seeded (%d rows), skipping.", count)
            return
        for season in DEFAULT_SEASONS:
            db.add(EstablishmentSettingsORM(**season))
        logger.info("Seeded %d season configurations.", len(DEFAULT_SEASONS))


def seed_defaults() -> None:
    seed_shift_templates()
    seed_season_settings()
