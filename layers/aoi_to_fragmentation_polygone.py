#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aoi_to_fragmentation_polygone.py
================================

Intersection AOI × ecocompensation.fragmentation_raster
→ ecocompensation_results.fragmentation_polygons (vecteur).

- Lit l’AOI depuis ecocompensation.aoi (geom_2154, EPSG:2154).
- Sélectionne les tuiles raster de ecocompensation.fragmentation_raster
  qui intersectent l’AOI.
- CLIP les tuiles à l’AOI (ST_Clip) puis vectorise en polygones
  via ST_DumpAsPolygons.
- Stocke le résultat dans ecocompensation_results.fragmentation_polygons
  (une entité par zone contiguë de même valeur de pixel).
"""

import os
import time
from pathlib import Path
from urllib.parse import quote_plus

from sqlalchemy import create_engine, text
from dotenv import load_dotenv


def run(engine, project_id: str, aoi_id: str, cb=None) -> int:
    """
    Construit ecocompensation_results.fragmentation_polygons pour l'AOI donnée.

    :param engine: Engine SQLAlchemy déjà connecté.
    :param project_id: Identifiant du projet (écrit dans les lignes de résultat).
    :param aoi_id: Identifiant de l'AOI pour la géométrie et les jointures.
    :param cb: Callback de log optionnel (cb(str)).
    :return: Nombre de polygones insérés.
    """
    log = cb or (lambda msg: None)

    # 1) AOI depuis la base (pour CE aoi_id)
    with engine.begin() as conn:
        row_aoi = conn.execute(
            text(
                """
                SELECT id,
                       ST_AsText(geom_2154) AS wkt_aoi,
                       ST_Area(geom_2154)  AS area_m2
                FROM ecocompensation.aoi
                WHERE id = :aid;
                """
            ),
            {"aid": aoi_id},
        ).mappings().one_or_none()

    if row_aoi is None:
        log(
            f"[FRAGMENTATION_POLYGONE] Aucune AOI trouvée dans ecocompensation.aoi pour id={aoi_id}, exécution annulée."
        )
        return 0

    aoi_area = row_aoi["area_m2"]
    log(
        f"[FRAGMENTATION_POLYGONE] AOI id={aoi_id}, surface ~ {aoi_area / 10_000:.2f} ha"
    )

    # 2) Schéma + table results (vecteur)
    with engine.begin() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS ecocompensation_results;"))
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS ecocompensation_results.fragmentation_polygons (
                    id uuid NOT NULL DEFAULT gen_random_uuid(),
                    project_id uuid NULL,
                    rid integer NOT NULL,
                    value integer NULL,
                    geom geometry NOT NULL,
                    created_at timestamptz NULL DEFAULT now(),
                    CONSTRAINT fragmentation_polygons_pkey PRIMARY KEY (id)
                );
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS fragmentation_polygons_geom_gix
                    ON ecocompensation_results.fragmentation_polygons
                    USING GIST (geom);
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS fragmentation_polygons_project_idx
                    ON ecocompensation_results.fragmentation_polygons (project_id);
                """
            )
        )
        # Nettoyage des anciennes géométries pour ce projet
        conn.execute(
            text(
                """
                DELETE FROM ecocompensation_results.fragmentation_polygons
                WHERE project_id = :pid;
                """
            ),
            {"pid": project_id},
        )

    # 3) Comptage des tuiles raster intersectantes
    with engine.begin() as conn:
        count_tiles = conn.execute(
            text(
                """
                SELECT count(*)
                FROM ecocompensation.fragmentation_raster r
                JOIN ecocompensation.aoi a
                  ON a.id = :aid
                WHERE ST_Intersects(
                    r.rast,
                    a.geom_2154
                );
                """
            ),
            {"aid": aoi_id},
        ).scalar_one()

    log(
        f"[FRAGMENTATION_POLYGONE] {count_tiles} tuiles raster intersectent l'AOI (avant vectorisation)."
    )

    if count_tiles == 0:
        log("[FRAGMENTATION_POLYGONE] Rien à vectoriser.")
        return 0

    # 4) Vectorisation : ST_Clip + ST_DumpAsPolygons
    t0 = time.perf_counter()
    with engine.begin() as conn:
        res = conn.execute(
            text(
                """
                INSERT INTO ecocompensation_results.fragmentation_polygons (project_id, rid, value, geom)
                SELECT
                    :project_id AS project_id,
                    r.rid,
                    (p).val::integer AS value,
                    (p).geom::geometry(Polygon, 2154) AS geom
                FROM ecocompensation.fragmentation_raster r
                JOIN ecocompensation.aoi a
                  ON a.id = :aoi_id
                CROSS JOIN LATERAL ST_DumpAsPolygons(
                    ST_Clip(
                        r.rast,
                        1,
                        a.geom_2154,
                        true
                    )
                ) AS p
                WHERE ST_Intersects(
                    r.rast,
                    a.geom_2154
                )
                  AND (p).val IS NOT NULL
                  AND (p).val <> ST_BandNoDataValue(r.rast, 1);
                """
            ),
            {"project_id": project_id, "aoi_id": aoi_id},
        )
    t1 = time.perf_counter()

    rows = res.rowcount or 0
    log(
        f"[RESULTS FRAGMENTATION_POLYGONE] {rows} polygones insérés dans ecocompensation_results.fragmentation_polygons "
        f"(en {t1 - t0:.2f} s)."
    )
    return rows


def main():
    """
    Entrée CLI : construit son propre engine et utilise la dernière AOI.
    """
    BASE_DIR = Path(__file__).resolve().parent
    load_dotenv(BASE_DIR / ".env")

    SUPABASE_HOST = os.getenv("SUPABASE_HOST")
    SUPABASE_PORT = os.getenv("SUPABASE_PORT", "6543")
    SUPABASE_DB = os.getenv("SUPABASE_DB", "postgres")
    SUPABASE_USER = os.getenv("SUPABASE_USER")
    SUPABASE_PASSWORD = os.getenv("SUPABASE_PASSWORD")

    if not all([SUPABASE_HOST, SUPABASE_DB, SUPABASE_USER, SUPABASE_PASSWORD]):
        raise RuntimeError(
            "Variables de connexion à la base manquantes dans le .env (FRAGMENTATION_POLYGONE)."
        )

    password_quoted = quote_plus(SUPABASE_PASSWORD)
    db_url = (
        f"postgresql+psycopg://{SUPABASE_USER}:{password_quoted}"
        f"@{SUPABASE_HOST}:{SUPABASE_PORT}/{SUPABASE_DB}"
    )
    engine = create_engine(db_url)

    # Dernier projet et son AOI
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT id, aoi_id FROM ecocompensation.projects "
                "WHERE aoi_id IS NOT NULL ORDER BY created_at DESC LIMIT 1;"
            )
        ).mappings().one_or_none()
    if not row:
        print("Aucun projet avec AOI trouvé.")
        return
    project_id = str(row["id"])
    aoi_id = str(row["aoi_id"])

    n = run(engine, project_id, aoi_id, cb=print)
    print(f"Total polygones de fragmentation insérés : {n}")


if __name__ == "__main__":
    main()

