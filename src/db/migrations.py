import logging

from src.db.database import Base, engine

logger = logging.getLogger(__name__)


def create_all_tables() -> None:
    """Create all database tables from ORM models. Idempotent — safe to run multiple times."""
    # Import all ORM models so they register with Base.metadata
    from src.models import employee, cruise_ship, shift_template, establishment, schedule, daily_demand  # noqa: F401

    logger.info("Creating database tables if they do not exist...")
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables ready.")
