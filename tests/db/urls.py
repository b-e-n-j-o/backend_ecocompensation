"""URLs de test. Toute cible non locale est refusée. psycopg2 est refusé."""
from __future__ import annotations

from sqlalchemy.engine import make_url

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def assert_local_psycopg(url: str) -> str:
    if not url:
        return ""
    parsed = make_url(url)
    host = (parsed.host or "").lower()
    if host not in LOCAL_HOSTS:
        raise RuntimeError(f"TEST_PG_URL doit pointer sur une base locale jetable, pas {host!r}")
    driver = parsed.drivername  # postgresql+psycopg
    if driver != "postgresql+psycopg":
        raise RuntimeError(
            f"driver interdit pour les tests : {driver!r}. Utiliser postgresql+psycopg (psycopg 3)."
        )
    return url


def with_database(url: str, database: str) -> str:
    return make_url(assert_local_psycopg(url)).set(database=database).render_as_string(hide_password=False)
