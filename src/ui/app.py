import logging
import streamlit as st

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Run DB setup on first load (idempotent)
try:
    from src.db.migrations import create_all_tables
    from src.db.seed import seed_defaults

    create_all_tables()
    seed_defaults()
except Exception as e:
    st.error(f"Database initialisation error: {e}")
    logger.exception("DB init failed")

st.set_page_config(
    page_title="Geiranger Sjokolade Scheduler",
    page_icon="🍫",
    layout="wide",
)

st.title("🍫 Geiranger Sjokolade — Staff Scheduler")
st.write("Use the sidebar to navigate between pages.")

st.markdown(
    """
    ### Welcome

    This application generates monthly staff schedules for Geiranger Sjokolade,
    taking cruise ship arrivals, employee capabilities, and Norwegian labour law
    into account.

    **Operating season:** 1 May – 15 October

    ---

    **Pages:**
    - **Settings** — Configure seasons, opening hours, and shift templates
    - **Employees** — Upload and manage employee data
    - **Cruise Ships** — Upload cruise schedules and ship-language mappings
    - **Schedule** — Generate and edit monthly schedules
    - **Export** — Download schedules as Excel or PDF
    """
)
