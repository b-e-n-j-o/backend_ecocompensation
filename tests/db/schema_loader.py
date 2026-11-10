"""Charge test_schema.sql. Pytest et la CLI smoke importent load_schema."""
from __future__ import annotations

import logging
from pathlib import Path

import psycopg
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from tests.db.urls import with_database

log = logging.getLogger("test_schema")

SCHEMA_SQL = Path(__file__).resolve().parent / "test_schema.sql"
SMOKE_DB = "eco_test"
LEGACY_DB = "eco_legacy"

_RESET = """
DROP SCHEMA IF EXISTS ecocompensation CASCADE;
DROP SCHEMA IF EXISTS ecocompensation_results CASCADE;
DROP SCHEMA IF EXISTS auth CASCADE;
DROP TABLE IF EXISTS public.canopy_analysis CASCADE;
DROP TABLE IF EXISTS public.lidar_tiles CASCADE;
DROP TABLE IF EXISTS public.parcelles_personnes_morales CASCADE;
DROP TABLE IF EXISTS public.parcelles_personnes_morales_tmp CASCADE;
DROP TABLE IF EXISTS public.personnes_morales_tmp CASCADE;
"""


def _admin(url: str) -> Engine:
    return create_engine(with_database(url, "postgres"), isolation_level="AUTOCOMMIT")


def ensure_database(url: str, name: str) -> str:
    admin = _admin(url)
    with admin.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
        ).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
            log.info("CREATE DATABASE %s", name)
    admin.dispose()
    return with_database(url, name)


def ensure_postgis(url: str) -> None:
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
    engine.dispose()


def load_schema(base_url: str) -> str:
    """Recharge eco_test depuis test_schema.sql. Retourne l'URL de cette base."""
    if not SCHEMA_SQL.is_file():
        raise FileNotFoundError(f"{SCHEMA_SQL} absent. Lancer build_test_schema.py")
    target = ensure_database(base_url, SMOKE_DB)
    engine = create_engine(target, isolation_level="AUTOCOMMIT")
    raw = SCHEMA_SQL.read_text(encoding="utf-8")
    log.info("reset des schémas de %s", SMOKE_DB)
    with engine.connect() as conn:
        conn.exec_driver_sql(_RESET)
    raw_conn = engine.raw_connection()
    try:
        log.info("chargement %s (%d octets)", SCHEMA_SQL.name, len(raw))
        with psycopg.ClientCursor(raw_conn.driver_connection) as cur:
            cur.execute(raw)
        raw_conn.commit()
    finally:
        raw_conn.close()
        engine.dispose()
    log.info("schéma chargé dans %s", SMOKE_DB)
    return target


def prepare_legacy(base_url: str) -> str:
    """Base vide + PostGIS pour les tests qui recréent un schéma minimal."""
    target = ensure_database(base_url, LEGACY_DB)
    ensure_postgis(target)
    return target
