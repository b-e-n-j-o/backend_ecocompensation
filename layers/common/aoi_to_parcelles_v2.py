#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aoi_to_parcelles_v2.py
===================

Construit ecocompensation_results.parcelles comme intersection de l'AOI
avec les parcelles de ecocompensation.parcelles.

PROBLÈME RÉSOLU : Sur une AOI de 20km de rayon (~250k parcelles à insérer),
le ST_Intersects en une seule transaction dépasse le statement_timeout de Supabase.

SOLUTION : Tiling spatial adaptatif en Python.
  1. On récupère le bbox de l'AOI.
  2. Taille de tuile adaptée au contexte :
       - sans pré-filtre surface : tuiles 5 km (évite les timeouts sur ~250k parcelles)
       - avec pré-filtre surface  : tuiles adaptatives 5–10 km (plafond 10×10 km)
  3. Pour chaque tuile, on INSERT les parcelles qui intersectent (tuile ∩ AOI).
  4. Déduplication inter-tuiles par NOT EXISTS sur (project_id, idu).
"""

from __future__ import annotations

import logging
import math
import time
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, text
from dotenv import load_dotenv
from urllib.parse import quote_plus
import os

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

MIN_TILE_SIZE_M = 5_000     # 5 km — tuile minimale (AOI denses sans pré-filtre)
MAX_TILE_SIZE_M = 10_000    # 10 km — tuile maximale (filter pipeline : pas plus grand)
MAX_TILES_FILTERED = 8      # cible d'allers-retours avec pré-filtre surface (si AOI compacte)
STATEMENT_TIMEOUT = "90s"   # Timeout par requête SQL (doit rester < timeout Supabase)


# ── DDL ───────────────────────────────────────────────────────────────────────

def create_results_table_if_not_exists(conn):
    conn.execute(text("CREATE SCHEMA IF NOT EXISTS ecocompensation_results;"))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS ecocompensation_results.parcelles (
            id           text,
            gid          integer,
            numero       text,
            feuille      integer,
            section      text,
            code_dep     text,
            nom_com      text,
            code_com     text,
            com_abs      text,
            code_arr     text,
            idu          text,
            contenance   double precision,
            code_insee   text,
            geom_2154    geometry(Geometry, 2154),
            project_id   uuid
        );
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_parcelles_results_geom_2154
            ON ecocompensation_results.parcelles USING GIST (geom_2154);
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_parcelles_results_project_id
            ON ecocompensation_results.parcelles (project_id);
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_parcelles_results_idu
            ON ecocompensation_results.parcelles (idu);
    """))


# ── Tiling ────────────────────────────────────────────────────────────────────

def get_aoi_bbox(conn, aoi_id: str) -> tuple[float, float, float, float] | None:
    """Retourne (xmin, ymin, xmax, ymax) en SRID 2154, ou None si AOI introuvable."""
    row = conn.execute(
        text("""
            SELECT
                ST_XMin(geom_2154) AS xmin,
                ST_YMin(geom_2154) AS ymin,
                ST_XMax(geom_2154) AS xmax,
                ST_YMax(geom_2154) AS ymax,
                ST_Area(geom_2154) AS area_m2
            FROM ecocompensation.aoi
            WHERE id = :aid
        """),
        {"aid": aoi_id},
    ).mappings().one_or_none()
    if row is None:
        return None
    return row["xmin"], row["ymin"], row["xmax"], row["ymax"], row["area_m2"]


def iter_tiles(
    xmin: float, ymin: float, xmax: float, ymax: float, tile_size: float
) -> Iterator[tuple[float, float, float, float]]:
    """
    Génère les tuiles (tx0, ty0, tx1, ty1) qui couvrent le bbox donné.
    Chaque tuile est un rectangle de tile_size × tile_size mètres.
    """
    x = xmin
    while x < xmax:
        y = ymin
        while y < ymax:
            yield (x, y, min(x + tile_size, xmax), min(y + tile_size, ymax))
            y += tile_size
        x += tile_size


def count_tiles(xmin, ymin, xmax, ymax, tile_size) -> int:
    nx = math.ceil((xmax - xmin) / tile_size)
    ny = math.ceil((ymax - ymin) / tile_size)
    return nx * ny


def compute_tile_size(
    xmin: float,
    ymin: float,
    xmax: float,
    ymax: float,
    *,
    min_area_ha: float,
) -> float:
    """
    Choisit la taille de tuile selon le contexte.

    Sans pré-filtre surface : tuiles 5 km (comportement historique, évite timeout).
    Avec pré-filtre surface  : tuiles adaptatives entre 5 et 10 km (jamais plus de 10×10 km).
    """
    if min_area_ha <= 0:
        return MIN_TILE_SIZE_M

    span = max(xmax - xmin, ymax - ymin)
    target_dim = math.ceil(math.sqrt(MAX_TILES_FILTERED))
    raw_size = math.ceil(span / target_dim)
    tile_size = max(
        MIN_TILE_SIZE_M,
        min(
            MAX_TILE_SIZE_M,
            math.ceil(raw_size / MIN_TILE_SIZE_M) * MIN_TILE_SIZE_M,
        ),
    )

    while (
        count_tiles(xmin, ymin, xmax, ymax, tile_size) > MAX_TILES_FILTERED
        and tile_size < MAX_TILE_SIZE_M
    ):
        tile_size = min(tile_size + MIN_TILE_SIZE_M, MAX_TILE_SIZE_M)

    return tile_size


# ── Core ──────────────────────────────────────────────────────────────────────

def run(
    engine,
    project_id: str,
    aoi_id: str,
    cb=None,
    *,
    min_area_ha: float = 7.0,
) -> int:
    """
    Construit ecocompensation_results.parcelles pour le projet donné via tiling.

    :param engine:      Engine SQLAlchemy.
    :param project_id:  UUID du projet.
    :param aoi_id:      UUID de l'AOI.
    :param cb:          Callback de log optionnel cb(str).
    :param min_area_ha: Surface minimale (ha) appliquée au tiling — évite d'écrire
                        des micro-parcelles immédiatement éliminées en post-filtre.
    :return:            Nombre de parcelles insérées (après déduplication).
    """
    log = cb or (lambda msg: None)
    min_area_m2 = min_area_ha * 10_000

    # ── 1. Récupérer le bbox de l'AOI ────────────────────────────────────────
    with engine.connect() as conn:
        result = get_aoi_bbox(conn, aoi_id)

    if result is None:
        log(f"⚠️ AOI id={aoi_id} introuvable.")
        return 0

    xmin, ymin, xmax, ymax, area_m2 = result
    area_ha = area_m2 / 10_000
    width_km = (xmax - xmin) / 1000
    height_km = (ymax - ymin) / 1000
    tile_size_m = compute_tile_size(xmin, ymin, xmax, ymax, min_area_ha=min_area_ha)
    n_tiles = count_tiles(xmin, ymin, xmax, ymax, tile_size_m)
    tile_mode = "adaptatif" if min_area_ha > 0 else "fixe 5km"

    log(f"🗺️  AOI id={aoi_id} — surface ~{area_ha:,.0f} ha")
    log(
        f"📐 Bbox : {width_km:.1f} km × {height_km:.1f} km "
        f"→ {n_tiles} tuiles de {tile_size_m // 1000}km ({tile_mode})"
    )
    if min_area_ha > 0:
        log(f"📏 Filtre surface au tiling : ≥ {min_area_ha} ha")

    # ── 2. DDL ────────────────────────────────────────────────────────────────
    with engine.begin() as conn:
        create_results_table_if_not_exists(conn)

    # ── 3. Purge des anciennes données pour ce projet ─────────────────────────
    with engine.begin() as conn:
        deleted = conn.execute(
            text("DELETE FROM ecocompensation_results.parcelles WHERE project_id = :pid"),
            {"pid": project_id},
        ).rowcount
    if deleted:
        log(f"🧹 {deleted:,} anciennes parcelles supprimées pour project_id={project_id}")

    # ── 4. Insertion par tuiles ───────────────────────────────────────────────
    # On utilise une table temporaire pour collecter les IDUs déjà insérés
    # et éviter les doublons sur les parcelles à cheval entre tuiles.
    # Stratégie : INSERT ... WHERE idu NOT IN (SELECT idu FROM results WHERE project_id=...)
    # → trop lent à grande échelle. Alternative plus efficace :
    #   ON CONFLICT DO NOTHING sur (idu, project_id) après avoir ajouté une contrainte UNIQUE.
    #
    # Pour rester non-intrusif (pas de ALTER TABLE en prod), on utilise
    # une approche "seen_idus" côté SQL : on filtre par idu non encore présent.
    # Grâce à l'index sur idu, le NOT EXISTS est rapide.

    total_inserted = 0
    tile_errors = 0
    t_global = time.perf_counter()

    tiles = list(iter_tiles(xmin, ymin, xmax, ymax, tile_size_m))

    for i, (tx0, ty0, tx1, ty1) in enumerate(tiles, 1):
        t_tile = time.perf_counter()
        try:
            with engine.begin() as conn:
                # Timeout par requête pour éviter de bloquer Supabase
                conn.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))

                result = conn.execute(
                    text("""
                        WITH aoi AS (
                            SELECT geom_2154
                            FROM ecocompensation.aoi
                            WHERE id = :aid
                        ),
                        tile AS (
                            SELECT ST_MakeEnvelope(:tx0, :ty0, :tx1, :ty1, 2154) AS geom
                        ),
                        zone AS (
                            -- Intersection tuile × AOI : réduit encore la zone de recherche
                            SELECT ST_Intersection(aoi.geom_2154, tile.geom) AS geom
                            FROM aoi, tile
                            WHERE ST_Intersects(aoi.geom_2154, tile.geom)
                        )
                        INSERT INTO ecocompensation_results.parcelles (
                            id, gid, numero, feuille, section, code_dep, nom_com,
                            code_com, com_abs, code_arr, idu, contenance, code_insee,
                            geom_2154, project_id
                        )
                        SELECT DISTINCT ON (p.idu)
                            p.id, p.gid, p.numero, p.feuille, p.section, p.code_dep,
                            p.nom_com, p.code_com, p.com_abs, p.code_arr, p.idu,
                            p.contenance, p.code_insee,
                            ST_Multi(p.geom_2154) AS geom_2154,
                            :pid AS project_id
                        FROM ecocompensation.parcelles p
                        JOIN zone ON p.geom_2154 && zone.geom
                                  AND ST_Intersects(p.geom_2154, zone.geom)
                        -- Déduplication inter-tuiles : on n'insère pas ce qui est déjà là
                        WHERE NOT EXISTS (
                            SELECT 1
                            FROM ecocompensation_results.parcelles r
                            WHERE r.project_id = :pid
                              AND r.idu = p.idu
                        )
                          AND ST_Area(p.geom_2154) >= :min_area_m2
                    """),
                    {
                        "aid": aoi_id,
                        "pid": project_id,
                        "tx0": tx0, "ty0": ty0,
                        "tx1": tx1, "ty1": ty1,
                        "min_area_m2": min_area_m2,
                    },
                )
                n = result.rowcount or 0

            total_inserted += n
            elapsed_tile = time.perf_counter() - t_tile
            # Préfixe structuré "TILE_PROGRESS:i/n_tiles:total" pour le frontend,
            # suivi du texte lisible en CLI. Le frontend parse ce préfixe pour
            # afficher le compteur croissant en temps réel.
            log(
                f"TILE_PROGRESS:{i}/{len(tiles)}:{total_inserted} "
                f"🔲 Tuile {i}/{len(tiles)} "
                f"({(tx1-tx0)/1000:.0f}×{(ty1-ty0)/1000:.0f}km) "
                f"→ {n:,} parcelles ({elapsed_tile:.1f}s) | total: {total_inserted:,}"
            )

        except Exception as e:
            tile_errors += 1
            log(f"  ❌ Tuile {i}/{len(tiles)} — erreur : {e}")
            logger.exception("Erreur tuile %d/%d", i, len(tiles))
            # On continue sur les autres tuiles

    elapsed_total = time.perf_counter() - t_global
    log(
        f"\n✅ {total_inserted:,} parcelles insérées pour project_id={project_id} "
        f"en {elapsed_total:.1f}s ({tile_errors} tuile(s) en erreur)"
    )

    # ── 5. Taille table (best-effort) ─────────────────────────────────────────
    try:
        with engine.connect() as conn:
            size_bytes = conn.execute(
                text("SELECT pg_total_relation_size('ecocompensation_results.parcelles')")
            ).scalar_one()
        log(f"🗄️  Taille table résultats : ~{size_bytes / 1_000_000:.1f} Mo")
    except Exception:
        pass

    return total_inserted


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    BASE_DIR = Path(__file__).resolve().parents[2]
    load_dotenv(BASE_DIR / ".env")

    host = os.getenv("SUPABASE_HOST")
    port = os.getenv("SUPABASE_PORT", "6543")
    db   = os.getenv("SUPABASE_DB", "postgres")
    user = os.getenv("SUPABASE_USER")
    pwd  = os.getenv("SUPABASE_PASSWORD")

    if not all([host, db, user, pwd]):
        raise RuntimeError("Variables de connexion manquantes dans .env")

    db_url = f"postgresql+psycopg://{user}:{quote_plus(pwd)}@{host}:{port}/{db}"
    engine = create_engine(db_url, pool_pre_ping=True)

    # ── IDs de test hardcodés (projet ZIP_MOR33) ──────────────────────────────
    project_id = "f5053040-cca6-4bbf-8a72-a36f519356a6"
    aoi_id     = "c0d47159-f2d7-4e76-bc61-4eef659f231f"
    print(f"Projet : {project_id} | AOI : {aoi_id}")

    n = run(engine, project_id, aoi_id, cb=print)
    print(f"\nTotal final : {n:,} parcelles")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    main()