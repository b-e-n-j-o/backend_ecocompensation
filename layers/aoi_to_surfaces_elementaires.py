#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
from pathlib import Path
from urllib.parse import quote_plus

import geopandas as gpd
import requests
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

# =============================
# CONFIG
# =============================

# WFS Sandre
WFS_URL = "https://services.sandre.eaufrance.fr/geo/sandre"
TYPE_NAME = "sa:SurfaceElementaire_Topage2025"
SRS_WFS = "EPSG:3857"   # indiqué dans la description
SRS_TARGET = "EPSG:2154"


def run(engine, aoi_id: str, cb=None) -> int:
    """
    Récupère les surfaces élémentaires Sandre intersectant l'AOI donnée
    et les insère dans ecocompensation_results.surfaces_elementaires.

    :param engine: Engine SQLAlchemy déjà connecté.
    :param aoi_id: Identifiant de l'AOI à traiter.
    :param cb: Callback de log optionnel (cb(str)).
    :return: Nombre d'entités insérées.
    """
    log = cb or (lambda msg: None)

    # 1) AOI depuis la base (geom_2154) pour CE aoi_id
    log("📥 Chargement AOI depuis ecocompensation.aoi ...")
    aoi = gpd.read_postgis(
        """
        SELECT id, geom_2154
        FROM ecocompensation.aoi
        WHERE id = %(aid)s;
        """,
        engine,
        geom_col="geom_2154",
        params={"aid": aoi_id},
    )

    if aoi.empty:
        log(f"⚠️ AOI id={aoi_id} introuvable, insertion annulée.")
        return 0

    if aoi.crs is None or aoi.crs.to_string() != "EPSG:2154":
        aoi = aoi.set_crs("EPSG:2154", allow_override=True)

    aoi_union = aoi.union_all()

    # AOI en 3857 pour la BBOX WFS
    aoi_3857 = aoi.to_crs(SRS_WFS)
    minx, miny, maxx, maxy = aoi_3857.total_bounds
    log(f"🔗 AOI utilisée pour le lien : id={aoi_id}")
    log(f"🧭 BBOX AOI en {SRS_WFS} : {minx}, {miny}, {maxx}, {maxy}")

    # 3) Requête WFS avec bbox
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": TYPE_NAME,
        "outputFormat": "application/json; subtype=geojson",
        "srsName": SRS_WFS,
        "bbox": f"{minx},{miny},{maxx},{maxy},{SRS_WFS}",
    }

    log("📡 Requête WFS SurfaceElementaire_Topage2025 sur BBOX AOI ...")
    resp = requests.get(WFS_URL, params=params, timeout=300)
    log(f"➡️ URL: {resp.url}")

    if resp.status_code != 200:
        log(f"❌ Erreur HTTP {resp.status_code}")
        log(resp.text[:1000])
        return 0

    log("📥 Lecture GeoJSON en GeoDataFrame ...")
    gdf = gpd.read_file(resp.text)
    if gdf.empty:
        log("⚠️ Aucune entité renvoyée par le WFS.")
        return 0

    if gdf.crs is None or gdf.crs.to_string() != SRS_WFS:
        gdf = gdf.set_crs(SRS_WFS, allow_override=True)

    log(f"🧮 Entités brutes dans la BBOX AOI : {len(gdf)}")

    # 4) Reprojection en 2154, intersection exacte avec AOI
    gdf_2154 = gdf.to_crs(SRS_TARGET)
    gdf_2154 = gdf_2154.rename_geometry("geom_2154")

    # Intersection stricte
    gdf_2154 = gdf_2154[gdf_2154.geom_2154.intersects(aoi_union)]
    log(f"🎯 Entités après intersection AOI : {len(gdf_2154)}")

    if gdf_2154.empty:
        return 0

    # Liaison aoi_id
    gdf_2154["aoi_id"] = aoi_id

    # 5) Insertion dans ecocompensation_results.surfaces_elementaires
    with engine.begin() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS ecocompensation_results;"))

    log("🏗️ Insertion dans ecocompensation_results.surfaces_elementaires ...")
    t0 = time.perf_counter()
    gdf_2154.to_postgis(
        name="surfaces_elementaires",
        con=engine,
        schema="ecocompensation_results",
        if_exists="append",
        index=False,
        chunksize=5000,
    )
    t1 = time.perf_counter()

    n = len(gdf_2154)
    log(
        f"✅ {n} entités insérées dans "
        f"ecocompensation_results.surfaces_elementaires (en {t1 - t0:.2f} s)."
    )
    return n


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
        raise RuntimeError("Variables de connexion à la base manquantes dans le .env (HYDRO).")

    password_quoted = quote_plus(SUPABASE_PASSWORD)
    db_url = (
        f"postgresql+psycopg://{SUPABASE_USER}:{password_quoted}"
        f"@{SUPABASE_HOST}:{SUPABASE_PORT}/{SUPABASE_DB}"
    )
    engine = create_engine(db_url)

    # Dernière AOI
    with engine.begin() as conn:
        aoi_id = conn.execute(
            text(
                "SELECT id FROM ecocompensation.aoi "
                "ORDER BY created_at DESC LIMIT 1;"
            )
        ).scalar_one()

    n = run(engine, str(aoi_id), cb=print)
    print(f"Total inséré : {n}")


if __name__ == "__main__":
    main()
