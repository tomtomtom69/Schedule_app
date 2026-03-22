import logging

from src.db.database import Base, engine

logger = logging.getLogger(__name__)

# Import all ORM models so they register with Base.metadata before any operation.
# IMPORTANT: every model file with an ORM class must be listed here.
from src.models import employee, cruise_ship, shift_template, establishment, schedule, daily_demand  # noqa: F401


def create_all_tables() -> None:
    """Create all database tables from ORM models. Idempotent — safe to run multiple times."""
    logger.info("Creating database tables if they do not exist...")
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables ready.")


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
