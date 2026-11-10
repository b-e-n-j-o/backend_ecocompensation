#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Filtrage de bout en bout sur l'emprise GEMFI (shapefile zippé).

Crée un projet temporaire, lance filter_pipeline.run avec le preset du panneau,
affiche le funnel (futur benchmark) puis supprime le projet.

    python3 tests/test_filter_gemfi.py
    python3 tests/test_filter_gemfi.py --no-cleanup
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid
import zipfile
from pathlib import Path

import geopandas as gpd
from shapely.ops import unary_union
from sqlalchemy import text

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from db import get_engine
from api.recherche_de_foncier.filtrage.layers.filter_pipeline import (
    FaunaCriterion,
    FilterConfig,
    run,
)

GEOM_ZIP = Path(
    "/Volumes/T7/Travaux_Freelance/KERELIA/CUAs/COMPENSATION_PARCELLE/"
    "COMPENSATION_ECO/DATA_ECOCOMPENSATION/DATA_INTERNE/PROJETS/test_GEMFI.zip"
)
BUFFER_M = 10_000
TARGET_EPSG = 2154

CESBIO = [
    "Colza",
    "Céréales à pailles",
    "Lande herbacée",
    "Lande ligneuse",
    "Landes ligneuses",
    "Maïs",
    "Prairies",
    "Protéagineux",
    "Soja",
    "Tournesol",
    "Tubercules/racines",
    "Verger",
    "Vergers",
    "Vigne",
    "Vignes",
]


def banner(msg: str) -> None:
    print(f"\n{'─' * 60}\n  {msg}\n{'─' * 60}")


def _read_emprise(zip_path: Path) -> gpd.GeoDataFrame:
    if not zip_path.is_file():
        raise SystemExit(f"Shapefile introuvable : {zip_path}")
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".shp")]
    if not names:
        raise SystemExit(f"Aucun .shp dans {zip_path}")
    gdf = gpd.read_file(f"zip://{zip_path}!{names[0]}")
    if gdf.empty or gdf.geometry.isna().all():
        raise SystemExit("Le shapefile ne contient aucune géométrie")
    if gdf.crs is None or gdf.crs.to_epsg() != TARGET_EPSG:
        gdf = gdf.to_crs(epsg=TARGET_EPSG)
    return gdf


def setup_project(engine, gdf: gpd.GeoDataFrame) -> tuple[str, str]:
    geom_wkt = unary_union(gdf.geometry.values).wkt
    project_id = str(uuid.uuid4())
    with engine.begin() as conn:
        aoi_id = str(conn.execute(
            text("""
                INSERT INTO ecocompensation.aoi (id, code_insee, buffer_m, geom_2154, project_id)
                VALUES (:pid, 'GEMFI', :bm, ST_Buffer(ST_GeomFromText(:wkt, 2154), :bm), :pid)
                RETURNING id
            """),
            {"pid": project_id, "bm": BUFFER_M, "wkt": geom_wkt},
        ).scalar_one())
        conn.execute(
            text("""
                INSERT INTO ecocompensation.projects (id, name, aoi_id, status)
                VALUES (:pid, 'TEST_FILTER_GEMFI', :aid, 'created')
            """),
            {"pid": project_id, "aid": aoi_id},
        )
    return project_id, aoi_id


def build_config() -> FilterConfig:
    return FilterConfig(
        min_area_ha=4,
        miller_thresh=0.4,
        cesbio_libelles=list(CESBIO),
        fauna_criteria=[
            FaunaCriterion(species="Crapaud calamite (Le)", dist_m=600),
            FaunaCriterion(species="Pipit rousseline", dist_m=600),
        ],
        fauna_combine="or",
        search_mode="exclude_source_buffer",
        zone_humide_mode="ignore",
        zones_humides_probables_mode="ignore",
        excluded_layers=["geomce", "preemption_ens", "ens", "natura_2000", "znieff_1"],
        troncons_hydros_max_dist_m=None,
        surfaces_hydros_max_dist_m=None,
        same_bassin_versant=False,
        include_unites_foncieres=True,
        include_ilots_rpg=False,
    )


def _print_enrichment(engine, project_id: str, idus: list[str]) -> None:
    """Contrôle enchaîné : les parcelles retenues ont bien des attributs enrichis."""
    sample = idus[:20]
    with engine.begin() as conn:
        rows = conn.execute(
            text("""
                SELECT idu, veg_libelles, fauna_distances
                FROM ecocompensation_results.parcelles
                WHERE project_id = CAST(:pid AS uuid)
                  AND idu = ANY(:idus)
            """),
            {"pid": project_id, "idus": sample},
        ).mappings().all()
    with_veg = sum(1 for r in rows if r["veg_libelles"])
    with_fauna = sum(1 for r in rows if r["fauna_distances"] and r["fauna_distances"] != {})
    print(f"  enrichissement        : {len(rows)} parcelles relues")
    print(f"    avec végétation     : {with_veg}")
    print(f"    avec distances faune: {with_fauna}")
    if rows and with_fauna == 0 and with_veg == 0:
        raise SystemExit("Enrichissement vide sur les parcelles retenues")


def run_unites_foncieres(engine, project_id: str, aoi_id: str, config: FilterConfig) -> None:
    """Même phase que l'orchestrateur après le filtre parcelles."""
    from filter_orchestrator import _run_uf_after_filter

    banner("Unités foncières")
    t_uf = time.perf_counter()
    errors: list[str] = []

    async def _push(data: dict) -> None:
        if data.get("event") == "error":
            errors.append(str(data.get("message") or "erreur UF"))
        msg = data.get("message") or ""
        if msg:
            print(f"  {msg}")

    asyncio.run(_run_uf_after_filter(engine, project_id, aoi_id, config, _push))
    if errors:
        raise SystemExit(" ; ".join(errors))

    with engine.begin() as conn:
        n_uf = conn.execute(
            text("""
                SELECT COUNT(*) FROM ecocompensation_results.unites_foncieres
                WHERE project_id = CAST(:pid AS uuid)
            """),
            {"pid": project_id},
        ).scalar_one()
        n_ss = conn.execute(
            text("""
                SELECT COUNT(*) FROM ecocompensation_results.sous_ensembles
                WHERE project_id = CAST(:pid AS uuid)
            """),
            {"pid": project_id},
        ).scalar_one()
        stored = conn.execute(
            text("""
                SELECT last_results_uf->>'total_uf' AS total_uf,
                       last_results_uf->>'total_sous_ensembles' AS total_ss,
                       jsonb_array_length(COALESCE(last_results_uf->'unites_foncieres', '[]'::jsonb)) AS n_pool
                FROM ecocompensation.projects
                WHERE id = CAST(:pid AS uuid)
            """),
            {"pid": project_id},
        ).mappings().one()
    print(f"  unités foncières en base : {n_uf}")
    print(f"  sous-ensembles en base   : {n_ss}")
    print(f"  pool UF (total_uf)       : {stored['total_uf']}")
    print(f"  sous-ensembles du pool   : {stored['total_ss']}")
    print(f"  unités dans la réponse   : {stored['n_pool']}")
    print(f"  durée UF                 : {time.perf_counter() - t_uf:.1f}s")


def cleanup(engine, project_id: str, aoi_id: str) -> None:
    banner("Nettoyage")
    with engine.begin() as conn:
        for tbl in ("parcelles", "sous_ensembles", "unites_foncieres"):
            n = conn.execute(
                text(f"DELETE FROM ecocompensation_results.{tbl} WHERE project_id = CAST(:pid AS uuid)"),
                {"pid": project_id},
            ).rowcount
            print(f"  {tbl}: {n} lignes supprimées")
        conn.execute(
            text("DELETE FROM ecocompensation.projects WHERE id = CAST(:pid AS uuid)"),
            {"pid": project_id},
        )
        conn.execute(
            text("DELETE FROM ecocompensation.aoi WHERE id = CAST(:aid AS uuid)"),
            {"aid": aoi_id},
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-cleanup", action="store_true")
    ap.add_argument("--zip", default=str(GEOM_ZIP))
    args = ap.parse_args()

    gdf = _read_emprise(Path(args.zip))
    banner(f"Emprise GEMFI — {len(gdf)} entité(s), buffer {BUFFER_M / 1000:.0f} km")
    engine = get_engine()
    project_id, aoi_id = setup_project(engine, gdf)
    print(f"  project_id={project_id}\n  aoi_id={aoi_id}")

    t0 = time.perf_counter()
    config = build_config()
    try:
        result = run(engine, project_id, aoi_id, config, cb=print)
    except BaseException:
        if not args.no_cleanup:
            cleanup(engine, project_id, aoi_id)
        raise

    banner("Benchmark parcelles")
    print(f"  parcelles tuilées     : {result.n_tiled}")
    print(f"  parcelles après filtre: {result.n_after_filter}")
    print(f"  purgées               : {result.n_purged}")
    print(f"  durée                 : {result.duration_s}s (mur {time.perf_counter() - t0:.1f}s)")
    if result.error:
        print(f"  erreur filtre         : {result.error}")
    if result.n_after_filter == 0:
        print("  enrichissement        : non lancé (aucune parcelle en sortie)")
    elif result.enrich_error:
        print(f"  enrichissement        : échec non bloquant — {result.enrich_error}")
    else:
        _print_enrichment(engine, project_id, result.surviving_idus)
    for step in result.funnel:
        label = step.get("label") or step.get("key") or step.get("step")
        print(f"    {step.get('count', '?'):>6}  {label}")

    if result.error or result.n_tiled == 0:
        if not args.no_cleanup:
            cleanup(engine, project_id, aoi_id)
        raise SystemExit(1)

    try:
        run_unites_foncieres(engine, project_id, aoi_id, config)
    except BaseException:
        if not args.no_cleanup:
            cleanup(engine, project_id, aoi_id)
        raise

    if args.no_cleanup:
        print(f"\n  projet conservé : {project_id}")
    else:
        cleanup(engine, project_id, aoi_id)
    print(f"\n  filtrage fonctionnel — durée totale {time.perf_counter() - t0:.1f}s")


if __name__ == "__main__":
    main()
