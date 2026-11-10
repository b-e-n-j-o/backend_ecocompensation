"""Infra PostGIS. Lancer depuis backend/ avec TEST_PG_URL vers le compose de test.

Les tables PPM du snapshot (public.parcelles_personnes_morales) sont dans la
même base : get_engine_ppm est renvoyé vers cet engine pour les tests db.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine

from tests.db.schema_loader import load_schema, prepare_legacy
from tests.db.urls import assert_local_psycopg

URL = os.environ.get("TEST_PG_URL", "")
if URL:
    assert_local_psycopg(URL)


@pytest.fixture(scope="session", autouse=True)
def _ensure_legacy_db():
    """Base PostGIS séparée pour les tests qui font DROP SCHEMA."""
    if URL:
        prepare_legacy(URL)
    yield


@pytest.fixture(scope="session")
def pg_url():
    if not URL:
        pytest.skip("TEST_PG_URL non défini")
    return load_schema(URL)


@pytest.fixture(scope="session")
def legacy_pg_url():
    if not URL:
        pytest.skip("TEST_PG_URL non défini")
    return prepare_legacy(URL)


@pytest.fixture(scope="session")
def pg_engine(pg_url):
    engine = create_engine(pg_url)
    assert engine.dialect.driver == "psycopg", engine.dialect.driver
    import db.db as db_mod

    previous = db_mod.get_engine_ppm
    db_mod.get_engine_ppm = lambda: engine
    yield engine
    db_mod.get_engine_ppm = previous
    engine.dispose()


@pytest.fixture
def db(pg_engine):
    """Transaction annulée en fin de test."""
    conn = pg_engine.connect()
    trans = conn.begin()
    try:
        yield conn
    finally:
        trans.rollback()
        conn.close()
