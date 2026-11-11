"""Profil parcelle : la somme des % de classes reste ≤ 100,5.

Une dalle entièrement dans la parcelle compte pour ST_Area(dalle).
Une dalle qui déborde compte pour l'intersection, pas pour la surface de la dalle.
Le pourcentage est cette surface divisée par ST_Area(parcelle).

    TEST_PG_URL=postgresql+psycopg://postgres:test@127.0.0.1:54329/postgres \\
        pytest tests/test_profile_cesbio_pct.py

La base eco_legacy est séparée du schéma de smoke : ce test la reconstruit.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from tests.db.urls import assert_local_psycopg, with_database

_RAW = os.environ.get("TEST_PG_URL", "")
URL = with_database(assert_local_psycopg(_RAW), "eco_legacy") if _RAW else ""
pytestmark = pytest.mark.skipif(not URL, reason="TEST_PG_URL non défini")

PID = "44444444-4444-4444-4444-444444444444"
RID = "55555555-5555-5555-5555-555555555555"


@pytest.fixture
def eng():
    engine = create_engine(URL)
    with engine.begin() as cx:
        cx.exec_driver_sql("""
        DROP SCHEMA IF EXISTS ecocompensation CASCADE;
        DROP SCHEMA IF EXISTS ecocompensation_results CASCADE;
        CREATE SCHEMA ecocompensation;
        CREATE SCHEMA ecocompensation_results;
        CREATE TABLE ecocompensation.vegetation_sur_cesbio (
            libelle_prio text,
            source text,
            geom geometry(Geometry, 2154)
        );
        CREATE TABLE ecocompensation_results.parcelles (
            project_id uuid,
            idu text,
            geom_2154 geometry(Geometry, 2154)
        );
        CREATE TABLE ecocompensation_results.parcelles_pool (
            project_id uuid,
            run_id uuid,
            idu text
        );
        """)
        cx.execute(text("""
            INSERT INTO ecocompensation_results.parcelles (project_id, idu, geom_2154)
            VALUES (CAST(:pid AS uuid), 'P1', ST_MakeEnvelope(0, 0, 100, 100, 2154));
            INSERT INTO ecocompensation_results.parcelles_pool (project_id, run_id, idu)
            VALUES (CAST(:pid AS uuid), CAST(:rid AS uuid), 'P1');
            INSERT INTO ecocompensation.vegetation_sur_cesbio (libelle_prio, source, geom)
            VALUES
              ('Forêt fermée de conifères', 'bdtopo', ST_MakeEnvelope(0, 0, 99.5, 100, 2154)),
              ('Forêt ouverte', 'cesbio', ST_MakeEnvelope(99.5, 0, 100, 100, 2154)),
              ('Forêt ouverte', 'cesbio', ST_MakeEnvelope(200, 0, 400, 100, 2154));
        """), {"pid": PID, "rid": RID})
    yield engine
    engine.dispose()


def test_somme_pct_parcelle_inferieure_a_100(eng):
    from api.recherche_de_foncier.enrichir.pool.profilers.profile_cesbio import _SQL_PROFILE_VEG_BATCH

    with eng.begin() as cx:
        rows = cx.execute(
            text(_SQL_PROFILE_VEG_BATCH),
            {"pid": PID, "rid": RID, "idus": ["P1"]},
        ).mappings().all()

    by_label = {r["libelle_prio"]: float(r["pct"]) for r in rows}
    assert by_label["Forêt fermée de conifères"] == pytest.approx(99.5, abs=0.05)
    assert by_label["Forêt ouverte"] == pytest.approx(0.5, abs=0.05)
    assert sum(by_label.values()) <= 100.5
    assert "Forêt ouverte" in by_label
    assert len(by_label) == 2

