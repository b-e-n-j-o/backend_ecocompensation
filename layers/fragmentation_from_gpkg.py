#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fragmentation_from_gpkg.py
==========================

Script de test : part d'une géométrie contenue dans un GeoPackage,
fait un buffer de 1 km autour, intersecte cette zone avec
ecocompensation.fragmentation_raster et insère les tuiles raster
CLIPÉES dans ecocompensation_results.fragmentation.

Utile pour vérifier que le pipeline de fragmentation fonctionne bien,
sans passer par ecocompensation.aoi.
"""

import os
from pathlib import Path
from urllib.parse import quote_plus

import geopandas as gpd
from sqlalchemy import create_engine, text
from dotenv import load_dotenv


# GeoPackage source
GPKG_PATH = Path(
    "/Volumes/T7/Travaux_Freelance/KERELIA/CUAs/COMPENSATION_PARCELLE/"
    "COMPENSATION_ECO/AOI/ZIP_MOR33.gpkg"
)

# ID logique pour distinguer cette zone dans la table de résultats
AOI_ID_LABEL = "ZIP_MOR33_buffer_1km"


def main():
    BASE_DIR = Path(__file__).resolve().parent
    load_dotenv(BASE_DIR / ".env")

    SUPABASE_HOST = os.getenv("SUPABASE_HOST")
    SUPABASE_PORT = os.getenv("SUPABASE_PORT", "6543")
    SUPABASE_DB = os.getenv("SUPABASE_DB", "postgres")
    SUPABASE_USER = os.getenv("SUPABASE_USER")
    SUPABASE_PASSWORD = os.getenv("SUPABASE_PASSWORD")

    if not all([SUPABASE_HOST, SUPABASE_DB, SUPABASE_USER, SUPABASE_PASSWORD]):
        raise RuntimeError(
            "Variables de connexion à la base manquantes dans le .env (FRAG_FROM_GPKG)."
        )

    password_quoted = quote_plus(SUPABASE_PASSWORD)
    db_url = (
        f"postgresql+psycopg://{SUPABASE_USER}:{password_quoted}"
        f"@{SUPABASE_HOST}:{SUPABASE_PORT}/{SUPABASE_DB}"
    )
    engine = create_engine(db_url)

    if not GPKG_PATH.exists():
        raise FileNotFoundError(f"GeoPackage introuvable: {GPKG_PATH}")

    print(f"📥 Lecture du GeoPackage: {GPKG_PATH}")
    gdf = gpd.read_file(GPKG_PATH)
    if gdf.empty:
        print("⚠️ GeoPackage vide, rien à faire.")
        return

    if gdf.crs is None:
        # On suppose EPSG:2154 si rien n'est renseigné (adapter si besoin)
        gdf = gdf.set_crs("EPSG:2154", allow_override=True)
    elif gdf.crs.to_string() != "EPSG:2154":
        gdf = gdf.to_crs("EPSG:2154")

    # Union de toutes les géométries puis buffer 1 km
    # unary_union renvoie directement une géométrie (Multi/Polygon, LineString, etc.)
    geom = gdf.unary_union
    geom_buffer = geom.buffer(1000.0)  # 1 km

    wkt_buffer = geom_buffer.wkt
    print("🧭 Zone buffer 1 km calculée à partir du GPKG.")

    with engine.begin() as conn:
        # Création de la table de résultats si besoin (même structure que aoi_to_fragmentation)
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS ecocompensation_results;"))
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS ecocompensation_results.fragmentation (
                    id uuid NOT NULL DEFAULT gen_random_uuid(),
                    aoi_id text NOT NULL,
                    rid integer NOT NULL,
                    rast raster NOT NULL,
                    created_at timestamptz NULL DEFAULT now(),
                    PRIMARY KEY (id)
                );
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_ecocomp_results_fragmentation_rast
                    ON ecocompensation_results.fragmentation
                    USING GIST (ST_ConvexHull(rast));
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_ecocomp_results_fragmentation_aoi
                    ON ecocompensation_results.fragmentation (aoi_id);
                """
            )
        )
        # Nettoyage des éventuelles anciennes données pour cet identifiant
        conn.execute(
            text(
                "DELETE FROM ecocompensation_results.fragmentation WHERE aoi_id = :aid;"
            ),
            {"aid": AOI_ID_LABEL},
        )

        # Comptage des tuiles intersectantes
        count = conn.execute(
            text(
                """
                SELECT count(*)
                FROM ecocompensation.fragmentation_raster r
                WHERE ST_Intersects(
                    r.rast,
                    ST_GeomFromText(:wkt, 2154)
                );
                """
            ),
            {"wkt": wkt_buffer},
        ).scalar_one()

        print(f"🧮 {count} tuiles raster de fragmentation intersectent le buffer 1 km.")

        if count == 0:
            print("⚠️ Rien à insérer.")
            return

        # Insertion des tuiles clipées
        res = conn.execute(
            text(
                """
                INSERT INTO ecocompensation_results.fragmentation (aoi_id, rid, rast)
                SELECT
                    :aoi_id AS aoi_id,
                    r.rid,
                    ST_Clip(
                        r.rast,
                        1,
                        ST_GeomFromText(:wkt, 2154),
                        true
                    ) AS rast
                FROM ecocompensation.fragmentation_raster r
                WHERE ST_Intersects(
                    r.rast,
                    ST_GeomFromText(:wkt, 2154)
                );
                """
            ),
            {"aoi_id": AOI_ID_LABEL, "wkt": wkt_buffer},
        )

    rows = res.rowcount if res.rowcount is not None else count
    print(
        f"✅ {rows} tuiles raster de fragmentation insérées dans "
        f"ecocompensation_results.fragmentation pour aoi_id='{AOI_ID_LABEL}'."
    )


if __name__ == "__main__":
    main()

