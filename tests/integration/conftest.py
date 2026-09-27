import psycopg
import pytest

from ap_agent.config import Settings


@pytest.fixture(scope="session")
def settings():
    s = Settings()
    try:
        psycopg.connect(s.database_url.get_secret_value(), connect_timeout=2).close()
    except psycopg.OperationalError:
        pytest.skip("database not running; start it with `docker compose up -d --wait`")
    return s
