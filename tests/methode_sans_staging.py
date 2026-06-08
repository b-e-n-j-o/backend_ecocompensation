#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
methode_sans_staging.py
=======================

Pipeline de test complet :
  1. Lit la géométrie projet depuis geometrie_test.zip
  2. Crée un AOI (buffer configurable) + un projet de test en base
  3. Fetch parcelles candidates (tiling adaptatif + filtre surface, aoi_to_parcelles_v2)
  4. Filtre en chaîne (spatial) : Miller → CESBIO EXISTS → Faune ST_DWithin
  5. Enrichit uniquement les survivantes (batch 200) — profiling léger :
       veg_libelles    text[]  — classes touchées (filtre rapide)
       fauna_distances jsonb   — distance KNN par espèce (filtre rapide)
  6. Profiling riche CESBIO sur le pool final (ST_Intersection + surfaces/pct)
  7. Affiche le pool final tagué
  8. Nettoyage optionnel (--no-cleanup pour garder les données)

Correction v2 :
  _SQL_ENRICH_VEG_BATCH utilise maintenant ST_Intersects (prédicat booléen)
  au lieu de ST_Intersection + ST_Area — 5-10× plus rapide, plus de timeout.
  veg_details (surface par classe) supprimé de l'enrichissement ; appartient
  au profiler (VegetationHybrideProfiler) sur le pool final.

Usage :
    python3 tests/methode_sans_staging.py
    python3 tests/methode_sans_staging.py --no-cleanup
    python3 tests/methode_sans_staging.py --skip-fetch --skip-enrich --project-id <uuid> --no-cleanup
"""
from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

import geopandas as gpd
from shapely.ops import unary_union
from sqlalchemy import text

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from layers.db import get_engine
from layers.aoi_to_parcelles_v2 import run as run_parcelles

GEOM_ZIP = BACKEND_DIR / "geometrie_test.zip"

BUFFER_M        = 10_000
MIN_AREA_HA     = 7.0
MILLER_THRESH   = 0.39
CESBIO_LIBELLES = ["Forêts de conifères", "Forêts de feuillus"]
FAUNA_SPECIES   = "Tarier pâtre"
FAUNA_DIST_M    = 1000.0

ENRICH_BATCH_SIZE    = 200
PROFILE_BATCH_SIZE   = 50    # profiling riche (ST_Intersection) sur pool final uniquement
STMT_TIMEOUT         = "90s"

TARGET_EPSG = 2154


def banner(msg: str) -> None:
    print(f"\n{'─'*60}\n  {msg}\n{'─'*60}")


def timed_count(conn, clauses: list[str], params: dict) -> tuple[int, float]:
    where = " AND ".join(f"({c})" for c in clauses)
    t0 = time.perf_counter()
    n = conn.execute(
        text(f"SELECT COUNT(*) FROM ecocompensation_results.parcelles p WHERE {where}"),
        params,
    ).scalar_one()
    return int(n), round(time.perf_counter() - t0, 2)


# ── 1. Géométrie ──────────────────────────────────────────────────────────────

def _extract_code_insee(gdf: gpd.GeoDataFrame) -> str:
    for col in ("code_insee", "InseeCom", "insee", "INSEE_COM"):
        if col in gdf.columns:
            val = gdf[col].dropna().iloc[0]
            if val is not None:
                return str(val).strip()
    return "TEST"


def load_geometry_wkt(zip_path: Path) -> tuple[str, str]:
    banner("Lecture géométrie projet")
    if not zip_path.exists():
        raise FileNotFoundError(f"Fichier introuvable : {zip_path}")
    try:
        gdf = gpd.read_file(f"zip://{zip_path}")
    except Exception:
        gdf = gpd.read_file(zip_path)
    code_insee = _extract_code_insee(gdf)
    print(f"  CRS source    : {gdf.crs}")
    print(f"  Nb entités    : {len(gdf)}")
    print(f"  code_insee    : {code_insee}")
    if gdf.crs is None or gdf.crs.to_epsg() != TARGET_EPSG:
        gdf = gdf.to_crs(epsg=TARGET_EPSG)
        print(f"  → Reprojection EPSG:{TARGET_EPSG}")
    geom = unary_union(gdf.geometry.values)
    print(f"  Surface       : {geom.area / 10_000:,.1f} ha")
    return geom.wkt, code_insee


# ── 2. Projet + AOI ───────────────────────────────────────────────────────────

def setup_test_project(engine, geom_wkt: str, code_insee: str, buffer_m: int) -> tuple[str, str]:
    banner(f"Création AOI (buffer {buffer_m/1000:.0f} km) + projet de test")
    project_id = str(uuid.uuid4())
    with engine.begin() as conn:
        aoi_id = str(conn.execute(text("""
            INSERT INTO ecocompensation.aoi (id, code_insee, buffer_m, geom_2154, project_id)
            VALUES (
                :project_id, :code_insee, :buffer_m,
                ST_Buffer(ST_GeomFromText(:geom_wkt, 2154), :buffer_m),
                :project_id
            )
            RETURNING id
        """), {"project_id": project_id, "code_insee": code_insee,
               "buffer_m": buffer_m, "geom_wkt": geom_wkt}).scalar_one())
        conn.execute(text("""
            INSERT INTO ecocompensation.projects (id, name, aoi_id, status)
            VALUES (:project_id, 'TEST_METHODE_SANS_STAGING', :aoi_id, 'created')
        """), {"project_id": project_id, "aoi_id": aoi_id})
    with engine.connect() as conn:
        area = conn.execute(text(
            "SELECT ST_Area(geom_2154)/10000 FROM ecocompensation.aoi WHERE id=:id"
        ), {"id": aoi_id}).scalar_one()
    print(f"  aoi_id={aoi_id}\n  project_id={project_id}")
    print(f"  code_insee={code_insee} | Surface AOI : {area:,.0f} ha")
    return project_id, aoi_id


# ── 3. Fetch ──────────────────────────────────────────────────────────────────

def fetch_parcelles(engine, project_id: str, aoi_id: str,
                    *, min_area_ha: float = MIN_AREA_HA) -> int:
    banner(f"Fetch parcelles candidates (tiling adaptatif, surface ≥ {min_area_ha} ha)")
    def log(m: str) -> None:
        if any(m.startswith(p) for p in ("TILE_PROGRESS", "✅", "🗺", "📏")):
            print(f"  {m}")
    t0 = time.perf_counter()
    n = run_parcelles(engine, project_id, aoi_id, cb=log, min_area_ha=min_area_ha)
    print(f"\n  → {n:,} parcelles insérées en {round(time.perf_counter()-t0,1)}s")
    return n


# ── 4. Filtrage spatial ───────────────────────────────────────────────────────

def run_filter(engine, project_id: str) -> list[str]:
    banner("Filtrage (chaîne cumulative — spatial)")
    params = {
        "project_id":      project_id,
        "miller_th":       MILLER_THRESH,
        "cesbio_libelles": CESBIO_LIBELLES,
        "fauna_species":   FAUNA_SPECIES,
        "fauna_dist_m":    FAUNA_DIST_M,
    }
    print(f"  CESBIO libellés : {CESBIO_LIBELLES}")
    print(f"  Faune           : '{FAUNA_SPECIES}' ≤ {FAUNA_DIST_M:.0f} m\n")

    all_clauses = [
        "p.project_id = :project_id",
        """(4.0*PI()*ST_Area(p.geom_2154))
           /NULLIF(ST_Perimeter(p.geom_2154)^2,0)::double precision >= :miller_th""",
        """EXISTS (
               SELECT 1 FROM ecocompensation.vegetation_sur_cesbio v
               WHERE v.libelle_prio = ANY(:cesbio_libelles)
                 AND p.geom_2154 && v.geom
                 AND ST_Intersects(p.geom_2154, v.geom)
           )""",
        """EXISTS (
               SELECT 1 FROM ecocompensation.fauna f
               WHERE f.nom_vernaculaire = :fauna_species
                 AND ST_DWithin(p.geom_2154, f.geometry, :fauna_dist_m)
           )""",
    ]
    labels = [
        f"Candidats (post-tiling ≥ {MIN_AREA_HA} ha)",
        f"Miller ≥ {MILLER_THRESH}",
        "CESBIO forêts (EXISTS/GiST)",
        f"Faune ≤ {FAUNA_DIST_M:.0f} m (ST_DWithin)",
    ]

    with engine.begin() as conn:
        cumulative = []
        for label, clause in zip(labels, all_clauses):
            cumulative.append(clause)
            n, dt = timed_count(conn, cumulative, params)
            print(f"  {label:<40} → {n:>7,}  [{dt}s]")
            if n == 0 and len(cumulative) > 1:
                print("  ⚠️  Pool vide, arrêt.")
                return []

        final_where = " AND ".join(f"({c})" for c in cumulative)
        idus = conn.execute(text(f"""
            SELECT p.idu
            FROM ecocompensation_results.parcelles p
            WHERE {final_where}
            ORDER BY ST_Area(p.geom_2154) DESC
        """), params).scalars().all()

    print(f"\n  🎯 Pool final : {len(idus)} parcelles (à enrichir)")
    return [str(i) for i in idus]


# ── 5. Enrichissement ─────────────────────────────────────────────────────────

_ENRICH_DDL = """
ALTER TABLE ecocompensation_results.parcelles
    ADD COLUMN IF NOT EXISTS veg_libelles    text[]  NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS fauna_distances jsonb   NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_parcelles_results_veg
    ON ecocompensation_results.parcelles USING GIN (veg_libelles);
CREATE INDEX IF NOT EXISTS idx_parcelles_results_fauna
    ON ecocompensation_results.parcelles USING GIN (fauna_distances);
"""

# ── VEG : ST_Intersects (prédicat booléen) + array_agg ───────────────────────
# PAS de ST_Intersection ni ST_Area → pas de création de géométrie → pas de timeout.
# veg_details (surface par classe) appartient au VegetationHybrideProfiler,
# qui tourne sur le pool final sélectionné (~50 parcelles), pas ici.
_SQL_ENRICH_VEG_BATCH = """
WITH veg_agg AS (
    SELECT
        p.idu,
        array_agg(DISTINCT v.libelle_prio ORDER BY v.libelle_prio)
            FILTER (WHERE v.libelle_prio IS NOT NULL) AS veg_libelles
    FROM ecocompensation_results.parcelles p
    JOIN ecocompensation.vegetation_sur_cesbio v
        ON p.geom_2154 && v.geom
       AND ST_Intersects(p.geom_2154, v.geom)
    WHERE p.project_id = :pid
      AND p.idu        = ANY(:idus)
    GROUP BY p.idu
)
UPDATE ecocompensation_results.parcelles p
SET    veg_libelles = COALESCE(va.veg_libelles, '{}')
FROM   veg_agg va
WHERE  p.project_id = :pid
  AND  p.idu        = va.idu
"""

# ── FAUNA : KNN via opérateur <-> (GiST) ─────────────────────────────────────
# fauna.geometry est en EPSG:2154 → ST_Distance retourne des mètres.
# -1 = aucune observation connue pour cette espèce.
_SQL_ENRICH_FAUNA_BATCH = """
WITH fauna_agg AS (
    SELECT
        p.idu,
        COALESCE(
            (
                SELECT ROUND(ST_Distance(p.geom_2154, f.geometry))::int
                FROM   ecocompensation.fauna f
                WHERE  f.nom_vernaculaire = :species
                ORDER  BY p.geom_2154 <-> f.geometry
                LIMIT  1
            ),
            -1
        ) AS dist_m
    FROM ecocompensation_results.parcelles p
    WHERE p.project_id = :pid
      AND p.idu        = ANY(:idus)
)
UPDATE ecocompensation_results.parcelles p
SET    fauna_distances = fauna_distances || jsonb_build_object(:species, fa.dist_m)
FROM   fauna_agg fa
WHERE  p.project_id = :pid
  AND  p.idu        = fa.idu
"""


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def enrich(engine, project_id: str, idus: list[str], species_list: list[str]) -> None:
    if not idus:
        print("  (aucune parcelle à enrichir)")
        return
    banner(f"Enrichissement veg + fauna ({len(idus)} parcelles, batch {ENRICH_BATCH_SIZE})")

    with engine.begin() as conn:
        conn.execute(text(_ENRICH_DDL))

    batches = _chunks(idus, ENRICH_BATCH_SIZE)
    t_veg = t_fauna = 0.0

    for i, batch in enumerate(batches, 1):
        # Végétation
        t0 = time.perf_counter()
        with engine.begin() as conn:
            conn.execute(text(f"SET LOCAL statement_timeout = '{STMT_TIMEOUT}'"))
            conn.execute(text(_SQL_ENRICH_VEG_BATCH), {"pid": project_id, "idus": batch})
        dt = round(time.perf_counter() - t0, 2)
        t_veg += dt
        print(f"  batch {i}/{len(batches)} veg   ({len(batch):>3} parcelles) → {dt}s")

        # Faune par espèce
        for species in species_list:
            t0 = time.perf_counter()
            with engine.begin() as conn:
                conn.execute(text(f"SET LOCAL statement_timeout = '{STMT_TIMEOUT}'"))
                conn.execute(text(_SQL_ENRICH_FAUNA_BATCH),
                             {"pid": project_id, "idus": batch, "species": species})
            dt = round(time.perf_counter() - t0, 2)
            t_fauna += dt
            print(f"  batch {i}/{len(batches)} fauna '{species}' ({len(batch):>3}) → {dt}s")

    print(f"  → veg {t_veg:.1f}s | fauna {t_fauna:.1f}s")


# ── 6. Profiling riche CESBIO (pool final — surfaces + pct par classe) ───────

_SQL_PROFILE_VEG_BATCH = """
WITH hits AS (
    SELECT
        p.idu,
        v.libelle_prio,
        ST_Area(ST_Intersection(p.geom_2154, v.geom)) AS inter_area_m2
    FROM ecocompensation_results.parcelles p
    JOIN ecocompensation.vegetation_sur_cesbio v
        ON p.geom_2154 && v.geom
       AND ST_Intersects(p.geom_2154, v.geom)
    WHERE p.project_id = :pid
      AND p.idu = ANY(:idus)
      AND v.libelle_prio IS NOT NULL
),
class_areas AS (
    SELECT idu, libelle_prio, SUM(inter_area_m2) AS area_m2
    FROM hits
    WHERE inter_area_m2 > 0
    GROUP BY idu, libelle_prio
),
parcel_areas AS (
    SELECT idu, ST_Area(geom_2154) AS parcel_area_m2
    FROM ecocompensation_results.parcelles
    WHERE project_id = :pid
      AND idu = ANY(:idus)
)
SELECT
    ca.idu,
    ca.libelle_prio,
    ROUND(ca.area_m2::numeric, 1) AS area_m2,
    ROUND((ca.area_m2 / NULLIF(pa.parcel_area_m2, 0) * 100)::numeric, 2) AS pct
FROM class_areas ca
JOIN parcel_areas pa ON pa.idu = ca.idu
ORDER BY ca.idu, ca.area_m2 DESC
"""


def profile_veg_surfaces(
    engine, project_id: str, idus: list[str]
) -> dict[str, dict[str, dict]]:
    """
    Profiling riche type VegetationHybrideProfiler, limité au pool final.
    Retourne {idu: {libelle_prio: {area_m2, pct}}}.
    """
    if not idus:
        return {}

    banner(
        f"Profiling CESBIO riche ({len(idus)} parcelles, batch {PROFILE_BATCH_SIZE})"
    )
    result: dict[str, dict[str, dict]] = {}
    batches = _chunks(idus, PROFILE_BATCH_SIZE)
    t_total = 0.0

    for i, batch in enumerate(batches, 1):
        t0 = time.perf_counter()
        with engine.connect() as conn:
            conn.execute(text(f"SET statement_timeout = '{STMT_TIMEOUT}'"))
            rows = conn.execute(
                text(_SQL_PROFILE_VEG_BATCH),
                {"pid": project_id, "idus": batch},
            ).mappings().all()
        dt = round(time.perf_counter() - t0, 2)
        t_total += dt
        print(f"  batch {i}/{len(batches)} surfaces ({len(batch):>3} parcelles) → {dt}s")

        for r in rows:
            idu = str(r["idu"])
            lib = str(r["libelle_prio"])
            result.setdefault(idu, {})[lib] = {
                "area_m2": float(r["area_m2"]),
                "pct": float(r["pct"]),
            }

    print(f"  → profiling total {t_total:.1f}s | {len(result)} parcelles détaillées")
    return result


# ── 7. Résultats ──────────────────────────────────────────────────────────────

def fetch_results(engine, project_id: str, idus: list[str]) -> list[dict]:
    if not idus:
        return []
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT
                p.idu,
                p.code_insee,
                p.section,
                p.numero,
                ROUND((ST_Area(p.geom_2154)/10000)::numeric, 2)     AS surface_ha,
                ROUND(((4*PI()*ST_Area(p.geom_2154))
                       /NULLIF(ST_Perimeter(p.geom_2154)^2,0))::numeric, 3) AS miller,
                p.veg_libelles,
                (p.fauna_distances->>:fauna_species)::int            AS dist_fauna_m
            FROM ecocompensation_results.parcelles p
            WHERE p.project_id = :project_id
              AND p.idu = ANY(:idus)
            ORDER BY surface_ha DESC
            LIMIT 20
        """), {"project_id": project_id, "idus": idus,
               "fauna_species": FAUNA_SPECIES}).mappings().all()
    return [dict(r) for r in rows]


def _format_profiled_veg(
    veg_profile: dict[str, dict] | None,
    selected: list[str],
) -> str:
    """Affiche pct + m² depuis le profiling riche (pool final)."""
    if not veg_profile:
        return ""
    parts = []
    for lib in selected:
        d = veg_profile.get(lib)
        if not d:
            continue
        parts.append(f"{lib}: {d['pct']}% ({d['area_m2']:,.0f} m²)")
    if not parts:
        # fallback : toutes les classes profilées, triées par pct décroissant
        for lib, d in sorted(veg_profile.items(), key=lambda x: -x[1]["pct"]):
            parts.append(f"{lib}: {d['pct']}% ({d['area_m2']:,.0f} m²)")
    return " | ".join(parts)


def _format_fauna_dist(dist) -> str:
    """0 m = observation dans la parcelle (pas falsy → '?')."""
    return "?" if dist is None else str(dist)


def print_results(
    parcelles: list[dict],
    veg_profiles: dict[str, dict[str, dict]] | None = None,
) -> None:
    if not parcelles:
        return
    banner(f"Pool (top {len(parcelles)})")
    print(
        f"  {'IDU':<22} {'S.ha':>7} {'Miller':>7} {'Fauna(m)':>9}  "
        f"Végétation (pct, m²)"
    )
    print(f"  {'─'*95}")
    for p in parcelles:
        idu = p["idu"]
        veg_str = _format_profiled_veg(
            (veg_profiles or {}).get(idu),
            CESBIO_LIBELLES,
        )
        if not veg_str:
            veg_str = ", ".join(p.get("veg_libelles") or [])
        print(
            f"  {idu:<22} {p['surface_ha']:>7.2f} {p['miller']:>7.3f} "
            f"{_format_fauna_dist(p['dist_fauna_m']):>9}  {veg_str}"
        )


# ── 7. Nettoyage ──────────────────────────────────────────────────────────────

def cleanup(engine, project_id: str, aoi_id: str) -> None:
    banner("Nettoyage données de test")
    with engine.begin() as conn:
        n = conn.execute(text(
            "DELETE FROM ecocompensation_results.parcelles WHERE project_id=:pid"
        ), {"pid": project_id}).rowcount
        conn.execute(text("DELETE FROM ecocompensation.projects WHERE id=:pid"),
                     {"pid": project_id})
        conn.execute(text("DELETE FROM ecocompensation.aoi WHERE id=:aid"),
                     {"aid": aoi_id})
    print(f"  {n} parcelles supprimées + projet + AOI.")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-cleanup",  action="store_true")
    parser.add_argument("--skip-fetch",  action="store_true")
    parser.add_argument("--skip-enrich", action="store_true")
    parser.add_argument("--project-id",  default=None)
    args = parser.parse_args()

    engine = get_engine()
    project_id, aoi_id = args.project_id, None
    t0 = time.perf_counter()

    if args.skip_fetch and project_id:
        print(f"  → Réutilisation project_id={project_id}")
        with engine.begin() as conn:
            row = conn.execute(text(
                "SELECT aoi_id FROM ecocompensation.projects WHERE id=:pid"
            ), {"pid": project_id}).mappings().one_or_none()
            if row:
                aoi_id = str(row["aoi_id"])
    else:
        geom_wkt, code_insee = load_geometry_wkt(GEOM_ZIP)
        project_id, aoi_id = setup_test_project(engine, geom_wkt, code_insee, BUFFER_M)
        if not args.skip_fetch:
            fetch_parcelles(engine, project_id, aoi_id, min_area_ha=MIN_AREA_HA)

    surviving_idus = run_filter(engine, project_id)

    if not args.skip_enrich and surviving_idus:
        enrich(engine, project_id, surviving_idus, species_list=[FAUNA_SPECIES])

    veg_profiles = (
        profile_veg_surfaces(engine, project_id, surviving_idus)
        if surviving_idus else {}
    )

    parcelles = fetch_results(engine, project_id, surviving_idus)
    print_results(parcelles, veg_profiles=veg_profiles)

    print(f"\n  ⏱  Durée totale : {round(time.perf_counter()-t0, 1)}s\n")

    if not args.no_cleanup and not args.skip_fetch:
        cleanup(engine, project_id, aoi_id)
    elif args.no_cleanup:
        print(f"\n  💾 Données conservées :")
        print(f"     project_id = {project_id}")
        print(f"     aoi_id     = {aoi_id}")
        print(f"\n  Relancer filtre + enrich :")
        print(f"     python3 tests/methode_sans_staging.py --skip-fetch --project-id {project_id} --no-cleanup")
        print(f"  Relancer filtre seul :")
        print(f"     python3 tests/methode_sans_staging.py --skip-fetch --skip-enrich --project-id {project_id} --no-cleanup")


if __name__ == "__main__":
    main()