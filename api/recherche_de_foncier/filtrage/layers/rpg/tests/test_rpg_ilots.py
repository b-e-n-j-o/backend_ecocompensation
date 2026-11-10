"""
Tests du module rpg_ilots contre un PostGIS LOCAL jetable.

    TEST_PG_URL=postgresql+psycopg://postgres:test@127.0.0.1:54329/postgres pytest api/recherche_de_foncier/filtrage/layers/rpg/tests/test_rpg_ilots.py

psycopg 3 uniquement. La base eco_legacy est reconstruite par le test
(schéma minimal), distincte de eco_test qui porte le snapshot.
"""
import asyncio
import os
import sys
import types
from dataclasses import dataclass, field

import pytest
from sqlalchemy import create_engine, text

from tests.db.urls import assert_local_psycopg, with_database

_RAW = os.environ.get("TEST_PG_URL", "")
URL = with_database(assert_local_psycopg(_RAW), "eco_legacy") if _RAW else ""
pytestmark = pytest.mark.skipif(not URL, reason="TEST_PG_URL non défini")

PID = "11111111-1111-1111-1111-111111111111"
AID = "22222222-2222-2222-2222-222222222222"


def box(x0, y0, x1, y1):
    return f"ST_Multi(ST_MakeEnvelope({x0},{y0},{x1},{y1},2154))"


@pytest.fixture
def eng():
    e = create_engine(URL)
    with e.begin() as cx:
        cx.exec_driver_sql("""
        DROP SCHEMA IF EXISTS ecocompensation CASCADE; DROP SCHEMA IF EXISTS ecocompensation_results CASCADE;
        CREATE SCHEMA ecocompensation; CREATE SCHEMA ecocompensation_results;
        CREATE TABLE ecocompensation.aoi (id uuid, geom_2154 geometry, buffer_m double precision);
        CREATE TABLE ecocompensation.projects (id uuid, updated_at timestamptz);
        CREATE TABLE ecocompensation.cadastre_parcelles (
            idu text, code_dep text, code_insee text, section text, numero text, geom geometry);
        CREATE TABLE ecocompensation.rpg2025_ilots (
            code_dep text, num_ilot text, geom geometry, cat_dominante text, code_group_dominant bigint,
            code_cultu_dominant text, part_pp float, part_jachere float, part_vigne float,
            nb_pm int, pm_dominant text, pm_unique boolean);
        CREATE TABLE ecocompensation.rpg2025_ilot_membre (
            num_ilot text, idu text, code_dep text, part_parcelle float);
        CREATE TABLE ecocompensation.pm_ilot_membre (owner_key text, idu text);
        CREATE TABLE ecocompensation.vegetation_sur_cesbio (libelle_prio text, source text, geom geometry);
        CREATE TABLE ecocompensation.fauna (nom_vernaculaire text, geometry geometry);
        CREATE TABLE ecocompensation.observation_faune (nom_espece text, geom geometry);
        CREATE TABLE ecocompensation_results.parcelles (project_id uuid, idu text, zone_humide_ha float);
        """)
        cx.execute(text(f"""
        INSERT INTO ecocompensation.aoi VALUES ('{AID}', ST_MakeEnvelope(0,0,2000,1000,2154), 0);
        INSERT INTO ecocompensation.projects VALUES ('{PID}', now());
        -- îlot A : 3 parcelles de 1 ha (prairie, PM unique) ; îlot B : 2 parcelles dont 1 hors AOI ;
        -- îlot C : 1 seule parcelle (écarté, min_parcelles=2) ; îlot D : hors AOI
        INSERT INTO ecocompensation.rpg2025_ilots VALUES
          ('33','A',{box(0,0,300,100)},'PP',18,'PPH',1,0,0,1,'111111111',TRUE),
          ('33','B',{box(1900,0,2100,100)},'TA',1,'BTH',0,0,0,0,NULL,FALSE),
          ('33','C',{box(500,500,600,600)},'TA',1,'MIS',0,0,0,0,NULL,FALSE),
          ('33','D',{box(5000,0,5300,100)},'PP',18,'PPH',1,0,0,0,NULL,FALSE);
        INSERT INTO ecocompensation.cadastre_parcelles VALUES
          ('a1','33','33000','A','0001',{box(0,0,100,100)}),
          ('a2','33','33000','A','0002',{box(100,0,200,100)}),
          ('a3','33','33000','A','0003',{box(200,0,300,100)}),
          ('b1','33','33000','B','0001',{box(1900,0,2000,100)}),
          ('b2','33','33000','B','0002',{box(2010,0,2100,100)}),
          ('c1','33','33000','C','0001',{box(500,500,600,600)}),
          ('d1','33','33000','D','0001',{box(5000,0,5100,100)}),
          ('d2','33','33000','D','0002',{box(5100,0,5200,100)});
        INSERT INTO ecocompensation.rpg2025_ilot_membre VALUES
          ('A','a1','33',1),('A','a2','33',1),('A','a3','33',1),('B','b1','33',1),('B','b2','33',1),
          ('C','c1','33',1),('D','d1','33',1),('D','d2','33',1);
        INSERT INTO ecocompensation.pm_ilot_membre VALUES
          ('111111111','a1'),('111111111','a2'),('111111111','a3');
        INSERT INTO ecocompensation.vegetation_sur_cesbio VALUES ('Prairies', 'cesbio', {box(0,0,50,50)});
        INSERT INTO ecocompensation.fauna VALUES ('Cuivré', ST_SetSRID(ST_MakePoint(150,600),2154));
        INSERT INTO ecocompensation_results.parcelles VALUES ('{PID}','a1',0.4), ('{PID}','a2',0.2);
        """))
    yield e
    e.dispose()


def params(**kw):
    from rpg_ilots.params import IlotsRpgParams
    return IlotsRpgParams(**kw)


def test_build(eng):
    from rpg_ilots import build
    n = build.run(eng, PID, AID, params())
    assert n == 1                                   # A seulement (B n'a qu'1 parcelle dans l'AOI, C 1 parcelle)
    with eng.connect() as cx:
        r = cx.execute(text("SELECT * FROM ecocompensation_results.ilots_rpg")).mappings().one()
    assert r["num_ilot"] == "A" and r["idus"] == ["a1", "a2", "a3"] and r["nb_parcelles"] == 3
    assert r["surface_ha"] == pytest.approx(3.0) and r["pm_unique"] and r["cat_dominante"] == "PP"
    assert r["miller"] == pytest.approx(4 * 3.14159265 * 30000 / 800 ** 2, rel=1e-4)


def test_build_min_area_et_relance(eng):
    from rpg_ilots import build
    assert build.run(eng, PID, AID, params(min_area_ha=5)) == 0
    assert build.run(eng, PID, AID, params(min_parcelles=1)) == 3      # A, B (b1 seul), C
    assert build.run(eng, PID, AID, params(min_parcelles=1)) == 3      # relance idempotente


def test_enrich_et_pool(eng):
    from rpg_ilots import build, enrich, pool
    build.run(eng, PID, AID, params())
    enrich.run(eng, PID, ["Cuivré"])
    with eng.connect() as cx:
        r = cx.execute(text("SELECT veg_libelles, fauna_distances FROM ecocompensation_results.ilots_rpg")).one()
    assert r.veg_libelles == ["Prairies"] and r.fauna_distances == {"Cuivré": 500}

    ok = params(cesbio_libelles=["Prairies"], fauna_criteria=[{"species": "Cuivré", "dist_m": 600}])
    assert [il["num_ilot"] for il in pool.select_ilots(eng, PID, ok)] == ["A"]
    trop_loin = params(fauna_criteria=[{"species": "Cuivré", "dist_m": 400}])
    assert pool.select_ilots(eng, PID, trop_loin) == []
    mauvaise_veg = params(cesbio_libelles=["Forêts"])
    assert pool.select_ilots(eng, PID, mauvaise_veg) == []
    zh = pool.select_ilots(eng, PID, params(study_type="zones_humides_intra", min_zone_humide_ha=0.5))
    assert zh[0]["zone_humide_ha"] == pytest.approx(0.6)
    assert pool.select_ilots(eng, PID, params(study_type="zones_humides_intra", min_zone_humide_ha=1)) == []


@pytest.fixture
def fake_profiling(monkeypatch):
    """Simule api.recherche_de_foncier.filtrage.layers.uf_profiling (absent hors appli)."""
    mod = types.ModuleType("uf_profiling")
    mod.compute_subset_score_eco = lambda d, f, c, zone_humide_ha=0, study_type="": {"total_score": 4}
    mod.lookup_prospects_by_siren = lambda conn, s: {x: {"siren": x, "intersects_pm_database": True} for x in s}
    mod._empty_pm_payload = lambda siren=None, denomination=None: {"siren": siren}
    parts = "api.recherche_de_foncier.filtrage.layers.uf_profiling".split(".")
    for i in range(1, len(parts)):
        monkeypatch.setitem(sys.modules, ".".join(parts[:i]), types.ModuleType(parts[i - 1]))
    monkeypatch.setitem(sys.modules, ".".join(parts), mod)


def test_runner_complet(eng, fake_profiling):
    from rpg_ilots import runner

    @dataclass
    class FC:
        species: str
        dist_m: float

    @dataclass
    class Config:
        min_area_ha: float = 1.0
        miller_thresh: float | None = None
        cesbio_libelles: list = field(default_factory=list)
        fauna_criteria: list = field(default_factory=lambda: [FC("Cuivré", 1000)])
        search_mode: str = "faune_buffer"
        min_zone_humide_ha: float = 0.0

    events = []

    async def push(d):
        events.append(d)

    asyncio.run(runner.run_ilots_rpg_after_filter(eng, PID, AID, Config(), push))
    assert any(e["event"] == "phase:ilots_rpg_ready" for e in events)
    with eng.connect() as cx:
        res = cx.execute(text("SELECT last_results_ilots_rpg FROM ecocompensation.projects")).scalar_one()
    assert res["total_ilots"] == 1
    il = res["ilots"][0]
    assert il["rang"] == 1 and il["score_eco"]["total_score"] == 4
    assert il["pm_prospect"]["siren"] == "111111111"


def test_api_results_et_geojson(eng):
    from rpg_ilots import api, runner
    from rpg_ilots.params import IlotsRpgParams
    assert api.get_results(eng, PID) is None
    assert api.get_geojson(eng, PID)["features"] == []
    runner.run_sync(eng, PID, AID, IlotsRpgParams(), profiling=False)
    res = api.get_results(eng, PID)
    assert res["total_ilots"] == 1
    gj = api.get_geojson(eng, PID)
    kinds = [f["properties"]["kind"] for f in gj["features"]]
    assert kinds == ["ilot_rpg", "uf", "parcelle", "parcelle", "parcelle"]
    f = next(x for x in gj["features"] if x["properties"]["kind"] == "uf")
    assert f["id"] == "uf:A" and f["geometry"]["type"] == "MultiPolygon"
    assert f["properties"]["rang"] == 1 and f["properties"]["pm_unique"] is True
    assert f["properties"]["cat_dominante"] == "PP" and f["properties"]["score_ratio"] == 0
    parcs = [x["properties"] for x in gj["features"] if x["properties"]["kind"] == "parcelle"]
    assert {x["idu"] for x in parcs} == {"a1", "a2", "a3"}
    assert all(x["num_ilot"] == "A" for x in gj["features"])
    a1 = next(x for x in parcs if x["idu"] == "a1")
    assert a1["section"] == "A" and a1["numero"] == "0001" and a1["in_pool"] is True
    assert a1["proprietaire"] == "111111111" and a1["owner_index"] == 0
    lon, lat = f["geometry"]["coordinates"][0][0][0]
    assert -10 < lon < 10 and -90 < lat < 90 and (lon, lat) != (0, 0)
