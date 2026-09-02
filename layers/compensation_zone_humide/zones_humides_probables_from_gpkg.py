#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zones_humides_probables_from_gpkg.py
====================================

Script de test : part d'une géométrie contenue dans un GeoPackage,
fait un buffer de 1 km autour, intersecte cette zone avec
geo.zones_humides_probables (raster de probabilité de zones humides)
et insère les POLYGONES vectorisés dans ecocompensation_results.zones_humides_probables.

Utile pour vérifier que le pipeline zones humides probables fonctionne bien
sans passer par ecocompensation.aoi.
"""

import os
from pathlib import Path
from urllib.parse import quote_plus

import geopandas as gpd
from sqlalchemy import create_engine, text
from dotenv import load_dotenv


# GeoPackage source (même que pour la fragmentation)
GPKG_PATH = Path(
    "/Volumes/T7/Travaux_Freelance/KERELIA/CUAs/COMPENSATION_PARCELLE/"
    "COMPENSATION_ECO/AOI/ZIP_MOR33.gpkg"
)

# ID logique pour distinguer cette zone dans la table de résultats
AOI_ID_LABEL = "ZIP_MOR33_ZH_buffer_1km"


def main():
    BASE_DIR = Path(__file__).resolve().parents[2]
    load_dotenv(BASE_DIR / ".env")

    SUPABASE_HOST = os.getenv("SUPABASE_HOST")
    SUPABASE_PORT = os.getenv("SUPABASE_PORT", "6543")
    SUPABASE_DB = os.getenv("SUPABASE_DB", "postgres")
    SUPABASE_USER = os.getenv("SUPABASE_USER")
    SUPABASE_PASSWORD = os.getenv("SUPABASE_PASSWORD")

    if not all([SUPABASE_HOST, SUPABASE_DB, SUPABASE_USER, SUPABASE_PASSWORD]):
        raise RuntimeError(
            "Variables de connexion à la base manquantes dans le .env (ZH_PROBA_FROM_GPKG)."
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
        # Création de la table de résultats si besoin (même structure que aoi_ro_zones_humides_probables)
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS ecocompensation_results;"))
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS ecocompensation_results.zones_humides_probables (
                    id uuid NOT NULL DEFAULT gen_random_uuid(),
                    aoi_id text NOT NULL,
                    rid integer NOT NULL,
                    value integer NULL,
                    geom geometry NOT NULL,
                    created_at timestamptz NULL DEFAULT now(),
                    CONSTRAINT zones_humides_probables_pkey PRIMARY KEY (id)
                );
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS zones_humides_probables_geom_gix
                    ON ecocompensation_results.zones_humides_probables
                    USING GIST (geom);
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS zones_humides_probables_aoi_idx
                    ON ecocompensation_results.zones_humides_probables (aoi_id);
                """
            )
        )
        # Nettoyage des éventuelles anciennes données pour cet identifiant
        conn.execute(
            text(
                "DELETE FROM ecocompensation_results.zones_humides_probables WHERE aoi_id = :aid;"
            ),
            {"aid": AOI_ID_LABEL},
        )

        # Comptage des tuiles intersectantes
        count_tiles = conn.execute(
            text(
                """
                SELECT count(*)
                FROM geo.zones_humides_probables r
                WHERE ST_Intersects(
                    r.rast,
                    ST_GeomFromText(:wkt, 2154)
                );
                """
            ),
            {"wkt": wkt_buffer},
        ).scalar_one()

        print(
            f"🧮 {count_tiles} tuiles raster de zones humides probables intersectent le buffer 1 km."
        )

        if count_tiles == 0:
            print("⚠️ Rien à vectoriser.")
            return

        # Insertion des polygones vectorisés
        res = conn.execute(
            text(
                """
                INSERT INTO ecocompensation_results.zones_humides_probables (aoi_id, rid, value, geom)
                SELECT
                    :aoi_id AS aoi_id,
                    r.rid,
                    (p).val::integer AS value,
                    (p).geom::geometry(Polygon, 2154) AS geom
                FROM geo.zones_humides_probables r
                CROSS JOIN LATERAL ST_DumpAsPolygons(
                    ST_Clip(
                        r.rast,
                        1,
                        ST_GeomFromText(:wkt, 2154),
                        true
                    )
                ) AS p
                WHERE ST_Intersects(
                    r.rast,
                    ST_GeomFromText(:wkt, 2154)
                )
                  AND (p).val IS NOT NULL
                  AND (p).val <> ST_BandNoDataValue(r.rast, 1);
                """
            ),
            {"aoi_id": AOI_ID_LABEL, "wkt": wkt_buffer},
        )

    rows = res.rowcount if res.rowcount is not None else count_tiles
    print(
        f"✅ {rows} polygones de zones humides probables insérés dans "
        f"ecocompensation_results.zones_humides_probables pour aoi_id='{AOI_ID_LABEL}'."
    )


if __name__ == "__main__":
    main()

