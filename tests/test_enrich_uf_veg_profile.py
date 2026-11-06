"""Surface CESBIO d'un sous-ensemble = somme des surfaces par parcelle.

Un polygone de végétation chevauche deux parcelles. Deux sous-ensembles
partagent la parcelle du milieu. veg_profile.area_m2 doit coller à
ST_Area(ST_Intersection(géométrie du sous-ensemble, végétation)), à 1 m².

    TEST_PG_URL=postgresql+psycopg://postgres:test@127.0.0.1/kertest \\
        pytest tests/test_enrich_uf_veg_profile.py
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

URL = os.environ.get("TEST_PG_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_PG_URL non défini")
if URL and not any(h in URL for h in ("@localhost", "@127.0.0.1")):
    raise RuntimeError("TEST_PG_URL doit pointer sur une base locale jetable")

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
            idu text,
            geom_2154 geometry(Geometry, 2154)
        );
        CREATE TABLE ecocompensation_results.sous_ensembles (
            project_id uuid,
            subset_id text,
            idus text[],
            geom_2154 geometry(Geometry, 2154),
            veg_libelles text[] NOT NULL DEFAULT '{}',
            veg_profile jsonb NOT NULL DEFAULT '{}'
        );
        """)
        cx.execute(text("""
            INSERT INTO ecocompensation_results.unites_foncieres (project_id, idu, geom_2154)
            VALUES
              (CAST(:pid AS uuid), 'A', ST_MakeEnvelope(0, 0, 10, 10, 2154)),
              (CAST(:pid AS uuid), 'B', ST_MakeEnvelope(10, 0, 20, 10, 2154)),
              (CAST(:pid AS uuid), 'C', ST_MakeEnvelope(0, 10, 10, 20, 2154));

            INSERT INTO ecocompensation.vegetation_sur_cesbio (libelle_prio, source, geom)
            VALUES ('Prairies', 'cesbio', ST_MakeEnvelope(5, 0, 15, 8, 2154));

            INSERT INTO ecocompensation_results.sous_ensembles
                (project_id, subset_id, idus, geom_2154)
            VALUES
              (CAST(:pid AS uuid), 'ss1', ARRAY['A','B'],
               (SELECT ST_Union(geom_2154) FROM ecocompensation_results.unites_foncieres
                WHERE idu IN ('A','B'))),
              (CAST(:pid AS uuid), 'ss2', ARRAY['B','C'],
               (SELECT ST_Union(geom_2154) FROM ecocompensation_results.unites_foncieres
                WHERE idu IN ('B','C')));
        """), {"pid": PID})
    yield engine
    engine.dispose()


def test_veg_profile_egal_intersection_sous_ensemble(eng):
    from api.recherche_de_foncier.filtrage.layers.enrich_uf import _SQL_VEG_BATCH

    with eng.begin() as cx:
        cx.execute(text(_SQL_VEG_BATCH), {"pid": PID, "batch": ["ss1", "ss2"]})
        rows = cx.execute(text("""
            SELECT
                ss.subset_id,
                (ss.veg_profile->'Prairies'->>'area_m2')::float AS area_m2,
                ST_Area(ST_Intersection(
                    ss.geom_2154,
                    (SELECT ST_Union(v.geom) FROM ecocompensation.vegetation_sur_cesbio v)
                )) AS ref_m2
            FROM ecocompensation_results.sous_ensembles ss
            WHERE ss.project_id = CAST(:pid AS uuid)
            ORDER BY ss.subset_id
        """), {"pid": PID}).mappings().all()

    assert [r["subset_id"] for r in rows] == ["ss1", "ss2"]
    for row in rows:
        assert row["area_m2"] == pytest.approx(row["ref_m2"], abs=1.0)
    assert rows[0]["area_m2"] == pytest.approx(80.0, abs=1.0)
    assert rows[1]["area_m2"] == pytest.approx(40.0, abs=1.0)
