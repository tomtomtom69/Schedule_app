import logging

from sqlalchemy import text

from src.db.database import Base, engine

logger = logging.getLogger(__name__)

# Import all ORM models so they register with Base.metadata before any operation.
# IMPORTANT: every model file with an ORM class must be listed here.
from src.models import employee, cruise_ship, shift_template, establishment, schedule, daily_demand, staffing_rule, closed_day  # noqa: F401


def create_all_tables() -> None:
    """Create all database tables from ORM models. Idempotent — safe to run multiple times."""
    logger.info("Creating database tables if they do not exist...")
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables ready.")
    run_safe_migrations()


def run_safe_migrations() -> None:
    """Add new columns to existing tables without data loss.

    Uses 'ALTER TABLE … ADD COLUMN IF NOT EXISTS' which is idempotent on PostgreSQL.
    Safe to run on every startup — does nothing if columns already exist.
    """
    migrations = [
        # Section 1: employee date_of_birth
        "ALTER TABLE employees ADD COLUMN IF NOT EXISTS date_of_birth DATE",
        # Section 6: establishment staffing caps
        "ALTER TABLE establishment_settings ADD COLUMN IF NOT EXISTS max_cafe_per_day INTEGER NOT NULL DEFAULT 5",
        "ALTER TABLE establishment_settings ADD COLUMN IF NOT EXISTS max_prod_per_day INTEGER NOT NULL DEFAULT 4",
        # Fallback mode flag and relaxation report
        "ALTER TABLE schedules ADD COLUMN IF NOT EXISTS is_fallback BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE schedules ADD COLUMN IF NOT EXISTS fallback_notes TEXT",
        # Archive versioning
        "ALTER TABLE schedules ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1",
        # DB-editable staffing rules
        """CREATE TABLE IF NOT EXISTS staffing_rules (
            id SERIAL PRIMARY KEY,
            season VARCHAR NOT NULL,
            scenario VARCHAR NOT NULL,
            cafe_needed INTEGER NOT NULL,
            production_needed INTEGER NOT NULL,
            CONSTRAINT uq_staffing_rule_season_scenario UNIQUE (season, scenario)
        )""",
        # Closed days (shop not open — solver skips these dates)
        """CREATE TABLE IF NOT EXISTS closed_days (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            date DATE NOT NULL UNIQUE,
            year INTEGER NOT NULL,
            reason TEXT
        )""",
    ]
    try:
        with engine.connect() as conn:
            for sql in migrations:
                conn.execute(text(sql))
            conn.commit()
        logger.info("Safe migrations applied.")
    except Exception as exc:
        logger.warning("Safe migration failed (may be harmless if using SQLite): %s", exc)


def reset_all_tables() -> None:
    """Drop ALL tables managed by this app and recreate them from scratch.

    Use this when the schema has changed in a way that requires a full reset
    (e.g. removing a table, changing column types that PostgreSQL can't ALTER).
    WARNING: destroys all data.
    """
    logger.info("Dropping all tables...")
    Base.metadata.drop_all(bind=engine)
    logger.info("Recreating all tables...")
    Base.metadata.create_all(bind=engine)
    logger.info("All tables recreated.")
