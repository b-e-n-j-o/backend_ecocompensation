"""Occupation du sol d'une unité foncière = somme des intersections parcelle par parcelle.

Deux sous-ensembles partagent la parcelle du milieu. La surface n'est comptée
qu'une fois, sur l'unité foncière, pas sur chaque sous-ensemble.
"""

    TEST_PG_URL=postgresql+psycopg://postgres:test@127.0.0.1:54329/postgres \\
        pytest tests/test_enrich_uf_veg_profile.py

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

PID = "33333333-3333-3333-3333-333333333333"


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
        CREATE TABLE ecocompensation_results.unites_foncieres (
            project_id uuid,
            uf_id text,
            idu text,
            geom_2154 geometry(Geometry, 2154),
            veg_libelles text[] NOT NULL DEFAULT '{}',
            veg_profile jsonb NOT NULL DEFAULT '{}'
        );
        CREATE TABLE ecocompensation_results.sous_ensembles (
            project_id uuid,
            uf_id text,
            subset_id text,
            idus text[],
            geom_2154 geometry(Geometry, 2154)
        );
        """)
        cx.execute(text("""
            INSERT INTO ecocompensation_results.unites_foncieres (project_id, uf_id, idu, geom_2154)
            VALUES
              (CAST(:pid AS uuid), 'uf1', 'A', ST_MakeEnvelope(0, 0, 10, 10, 2154)),
              (CAST(:pid AS uuid), 'uf1', 'B', ST_MakeEnvelope(10, 0, 20, 10, 2154)),
              (CAST(:pid AS uuid), 'uf1', 'C', ST_MakeEnvelope(0, 10, 10, 20, 2154));

            INSERT INTO ecocompensation.vegetation_sur_cesbio (libelle_prio, source, geom)
            VALUES ('Prairies', 'cesbio', ST_MakeEnvelope(5, 0, 15, 8, 2154));

            INSERT INTO ecocompensation_results.sous_ensembles
                (project_id, uf_id, subset_id, idus, geom_2154)
            VALUES
              (CAST(:pid AS uuid), 'uf1', 'ss1', ARRAY['A','B'],
               (SELECT ST_Union(geom_2154) FROM ecocompensation_results.unites_foncieres
                WHERE idu IN ('A','B'))),
              (CAST(:pid AS uuid), 'uf1', 'ss2', ARRAY['B','C'],
               (SELECT ST_Union(geom_2154) FROM ecocompensation_results.unites_foncieres
                WHERE idu IN ('B','C')));
        """), {"pid": PID})
    yield engine
    engine.dispose()


def test_veg_profile_somme_parcelles_une_fois(eng):
    from api.recherche_de_foncier.filtrage.layers.enrich_uf import _SQL_VEG_BATCH

    with eng.begin() as cx:
        cx.execute(text(_SQL_VEG_BATCH), {"pid": PID, "batch": ["uf1"]})
        row = cx.execute(text("""
            SELECT DISTINCT
                (veg_profile->'Prairies'->>'area_m2')::float AS area_m2
            FROM ecocompensation_results.unites_foncieres
            WHERE project_id = CAST(:pid AS uuid)
        """), {"pid": PID}).mappings().one()

    # A ∩ prairie = 40 m², B ∩ prairie = 40 m², C = 0. B n'est pas compté deux fois.
    assert row["area_m2"] == pytest.approx(80.0, abs=1.0)
