#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_uf_pipeline.py
====================

Test standalone du pipeline Unités Foncières (personnes morales).
Peut réutiliser un project_id existant (--project-id) ou en créer un nouveau.

Étapes :
  1. (optionnel) Création AOI + projet de test
  2. aoi_to_sub_uf            → unites_foncieres + sous_ensembles
  4. Filtre : surface_ha + miller (colonnes) + CESBIO EXISTS + Fauna DWithin
  5. Affichage pool UF parallèle

Usage :
    python3 tests/test_uf_pipeline.py
    python3 tests/test_uf_pipeline.py --project-id <uuid>   # réutilise projet existant
    python3 tests/test_uf_pipeline.py --no-cleanup
"""
from __future__ import annotations

import argparse, sys, time, uuid
from pathlib import Path

import geopandas as gpd
from shapely.ops import unary_union
from sqlalchemy import text

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from db import get_engine, get_engine_ppm
from layers.common.aoi_to_sub_uf import run as run_sub_uf

GEOM_ZIP        = BACKEND_DIR / "geometrie_test.zip"
BUFFER_M        = 10_000
MIN_AREA_HA     = 7.0   # aligné avec aoi_to_sub_uf / filter_v2
MILLER_THRESH   = 0.39
CESBIO_LIBELLES = ["Forêts de conifères", "Forêts de feuillus"]
FAUNA_SPECIES   = "Tarier pâtre"
FAUNA_DIST_M    = 1000.0
TARGET_EPSG     = 2154


def banner(msg): print(f"\n{'─'*60}\n  {msg}\n{'─'*60}")

# ── Setup projet ──────────────────────────────────────────────────────────────

def _extract_code_insee(gdf):
    for col in ("code_insee", "InseeCom", "insee", "INSEE_COM"):
        if col in gdf.columns:
            val = gdf[col].dropna().iloc[0]
            if val is not None:
                return str(val).strip()
    return "TEST"

def setup_project(engine, buffer_m: int) -> tuple[str, str]:
    banner(f"Création AOI (buffer {buffer_m/1000:.0f} km) + projet de test")
    gdf = gpd.read_file(f"zip://{GEOM_ZIP}")
    code_insee = _extract_code_insee(gdf)
    if gdf.crs is None or gdf.crs.to_epsg() != TARGET_EPSG:
        gdf = gdf.to_crs(epsg=TARGET_EPSG)
    geom_wkt = unary_union(gdf.geometry.values).wkt
    project_id = str(uuid.uuid4())
    with engine.begin() as conn:
        aoi_id = str(conn.execute(text("""
            INSERT INTO ecocompensation.aoi (id, code_insee, buffer_m, geom_2154, project_id)
            VALUES (:pid, :ci, :bm, ST_Buffer(ST_GeomFromText(:wkt, 2154), :bm), :pid)
            RETURNING id
        """), {"pid": project_id, "ci": code_insee, "bm": buffer_m, "wkt": geom_wkt}).scalar_one())
        conn.execute(text("""
            INSERT INTO ecocompensation.projects (id, name, aoi_id, status)
            VALUES (:pid, 'TEST_UF_PIPELINE', :aid, 'created')
        """), {"pid": project_id, "aid": aoi_id})
    print(f"  project_id={project_id}\n  aoi_id={aoi_id}")
    return project_id, aoi_id

# ── Fetch UF + sous-ensembles ─────────────────────────────────────────────────

def fetch_uf(engine, project_id: str, aoi_id: str) -> tuple[int, int]:
    banner("Fetch UF + sous-ensembles (PPM, une passe)")
    engine_ppm = get_engine_ppm()
    def log(m): print(f"  {m}")
    t0 = time.perf_counter()
    n_ss = run_sub_uf(
        engine, project_id, aoi_id, log,
        engine_ppm=engine_ppm,
        min_area_ha=MIN_AREA_HA,
    )
    print(f"  → {n_ss:,} sous-ensembles en {round(time.perf_counter()-t0,1)}s")
    with engine.begin() as conn:
        n_uf = conn.execute(text("""
            SELECT COUNT(*) FROM ecocompensation_results.unites_foncieres
            WHERE project_id = :pid
        """), {"pid": project_id}).scalar_one()
    if n_ss == 0:
        print("  Aucune UF — pas de données PPM dans ce buffer ou filtre surface trop strict.")
        return int(n_uf or 0), 0
    return int(n_uf or 0), n_ss

# ── Filtrage sur sous_ensembles ───────────────────────────────────────────────

def filter_sous_ensembles(engine, project_id: str) -> list[dict]:
    banner("Filtrage sous-ensembles (surface + Miller + CESBIO + Fauna)")

    # surface_ha et miller sont des colonnes précalculées → pas de ST_Area
    params = {
        "project_id":      project_id,
        "min_area_ha":     MIN_AREA_HA,
        "miller_th":       MILLER_THRESH,
        "cesbio_libelles": CESBIO_LIBELLES,
        "fauna_species":   FAUNA_SPECIES,
        "fauna_dist_m":    FAUNA_DIST_M,
    }

    clauses_steps = [
        ("Tous sous-ensembles",
         ["ss.project_id = :project_id"]),
        (f"Surface ≥ {MIN_AREA_HA} ha",
         ["ss.project_id = :project_id",
          "ss.surface_ha >= :min_area_ha"]),
        (f"Miller ≥ {MILLER_THRESH}",
         ["ss.project_id = :project_id",
          "ss.surface_ha >= :min_area_ha",
          "ss.miller >= :miller_th"]),
        ("CESBIO forêts (EXISTS)",
         ["ss.project_id = :project_id",
          "ss.surface_ha >= :min_area_ha",
          "ss.miller >= :miller_th",
          """EXISTS (
              SELECT 1 FROM ecocompensation.vegetation_sur_cesbio v
              WHERE v.libelle_prio = ANY(:cesbio_libelles)
                AND ss.geom_2154 && v.geom
                AND ST_Intersects(ss.geom_2154, v.geom)
          )"""]),
        (f"Fauna ≤ {FAUNA_DIST_M:.0f} m",
         ["ss.project_id = :project_id",
          "ss.surface_ha >= :min_area_ha",
          "ss.miller >= :miller_th",
          """EXISTS (
              SELECT 1 FROM ecocompensation.vegetation_sur_cesbio v
              WHERE v.libelle_prio = ANY(:cesbio_libelles)
                AND ss.geom_2154 && v.geom
                AND ST_Intersects(ss.geom_2154, v.geom)
          )""",
          """EXISTS (
              SELECT 1 FROM ecocompensation.fauna f
              WHERE f.nom_vernaculaire = :fauna_species
                AND ST_DWithin(ss.geom_2154, f.geometry, :fauna_dist_m)
          )"""]),
    ]

    with engine.begin() as conn:
        for label, clauses in clauses_steps:
            where = " AND ".join(f"({c})" for c in clauses)
            t0 = time.perf_counter()
            n = conn.execute(text(
                f"SELECT COUNT(*) FROM ecocompensation_results.sous_ensembles ss WHERE {where}"
            ), params).scalar_one()
            dt = round(time.perf_counter() - t0, 2)
            print(f"  {label:<38} → {n:>6,}  [{dt}s]")
            if n == 0 and len(clauses) > 1:
                print("  ⚠️  Pool UF vide.")
                return []

        final_where = " AND ".join(f"({c})" for c in clauses_steps[-1][1])
        rows = conn.execute(text(f"""
            SELECT
                ss.subset_id,
                ss.uf_id,
                ss.k,
                ss.idus,
                ROUND(ss.surface_ha::numeric, 2)    AS surface_ha,
                ROUND(ss.miller::numeric, 3)         AS miller,
                ROUND((ss.dist_centre_m / 1000.0)::numeric, 1) AS dist_km,
                ss.denomination,
                ss.siren,
                (
                    SELECT array_agg(DISTINCT v.libelle_prio)
                           FILTER (WHERE v.libelle_prio IS NOT NULL)
                    FROM ecocompensation.vegetation_sur_cesbio v
                    WHERE ss.geom_2154 && v.geom
                      AND ST_Intersects(ss.geom_2154, v.geom)
                ) AS veg_libelles,
                (
                    SELECT ROUND(ST_Distance(ss.geom_2154, f.geometry))::int
                    FROM ecocompensation.fauna f
                    WHERE f.nom_vernaculaire = :fauna_species
                    ORDER BY ss.geom_2154 <-> f.geometry
                    LIMIT 1
                ) AS dist_fauna_m
            FROM ecocompensation_results.sous_ensembles ss
            WHERE {final_where}
            ORDER BY ss.surface_ha DESC
            LIMIT 20
        """), params).mappings().all()

    print(f"\n  🎯 Pool UF final : {len(rows)} sous-ensembles")
    return [dict(r) for r in rows]

# ── Affichage ─────────────────────────────────────────────────────────────────

def print_results(rows: list[dict]) -> None:
    if not rows:
        return
    banner(f"Pool UF (top {len(rows)})")
    print(f"  {'Subset ID':<28} {'k':>3} {'S.ha':>7} {'Miller':>7} {'Dist.km':>8} {'Fauna(m)':>9}  Dénomination / Veg")
    print(f"  {'─'*95}")
    for r in rows:
        denom = (r.get("denomination") or "—")[:25]
        classes = ", ".join((r.get("veg_libelles") or []))[:30]
        dist_f = r["dist_fauna_m"] if r["dist_fauna_m"] is not None else "?"
        print(
            f"  {r['subset_id']:<28} {r['k']:>3} {r['surface_ha']:>7.2f}"
            f" {r['miller']:>7.3f} {r['dist_km']:>8} {str(dist_f):>9}"
            f"  {denom} | {classes}"
        )

# ── Nettoyage ─────────────────────────────────────────────────────────────────

def cleanup(engine, project_id: str, aoi_id: str) -> None:
    banner("Nettoyage")
    with engine.begin() as conn:
        for tbl in ("sous_ensembles", "unites_foncieres"):
            n = conn.execute(text(
                f"DELETE FROM ecocompensation_results.{tbl} WHERE project_id=:pid"
            ), {"pid": project_id}).rowcount
            print(f"  {tbl}: {n} lignes supprimées")
        conn.execute(text("DELETE FROM ecocompensation.projects WHERE id=:pid"),
                     {"pid": project_id})
        conn.execute(text("DELETE FROM ecocompensation.aoi WHERE id=:aid"),
                     {"aid": aoi_id})

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-id",  default=None)
    ap.add_argument("--no-cleanup",  action="store_true")
    ap.add_argument("--skip-fetch",  action="store_true")
    args = ap.parse_args()

    engine = get_engine()
    project_id, aoi_id = args.project_id, None
    t0 = time.perf_counter()

    if args.skip_fetch and project_id:
        with engine.begin() as conn:
            row = conn.execute(text(
                "SELECT aoi_id FROM ecocompensation.projects WHERE id=:pid"
            ), {"pid": project_id}).mappings().one_or_none()
            if row:
                aoi_id = str(row["aoi_id"])
        print(f"  → Réutilisation project_id={project_id}")
    else:
        project_id, aoi_id = setup_project(engine, BUFFER_M)
        if not args.skip_fetch:
            fetch_uf(engine, project_id, aoi_id)

    rows = filter_sous_ensembles(engine, project_id)
    print_results(rows)

    print(f"\n  ⏱  Durée totale : {round(time.perf_counter()-t0,1)}s\n")

    if args.no_cleanup:
        print(f"  💾 project_id={project_id}")
        print(f"  Relancer filtre : python3 tests/test_uf_pipeline.py --skip-fetch --project-id {project_id} --no-cleanup")
    elif not args.skip_fetch:
        cleanup(engine, project_id, aoi_id)


if __name__ == "__main__":
    main()