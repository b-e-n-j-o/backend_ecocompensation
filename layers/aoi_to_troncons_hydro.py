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
from shapely import force_2d  # <-- AJOUT IMPORTANT

# =============================
# CONFIG
# =============================

# WFS IGN – BDTOPO Tronçons hydrographiques
WFS_URL = "https://data.geopf.fr/wfs/ows"
TYPE_NAME = "BDTOPO_V3:troncon_hydrographique"
SRS_WFS = "EPSG:3857"   # comme indiqué
SRS_TARGET = "EPSG:2154"

# Pagination WFS
PAGE_LIMIT = 5000
SLEEP_BETWEEN_PAGES = 2.0


def run(engine, project_id: str, aoi_id: str, cb=None) -> int:
    """
    Récupère les tronçons hydro BDTOPO intersectant l'AOI donnée
    et les insère dans ecocompensation_results.troncons_hydro.

    :param engine: Engine SQLAlchemy déjà connecté.
    :param project_id: Identifiant du projet (écrit dans les lignes de résultat).
    :param aoi_id: Identifiant de l'AOI pour l'intersection géométrique.
    :param cb: Callback de log optionnel (cb(str)).
    :return: Nombre total de tronçons insérés.
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

    # 3) Pagination WFS IGN avec bbox + startIndex/count
    total_inserted = 0
    page = 0
    start_index = 0

    with engine.begin() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS ecocompensation_results;"))
        try:
            conn.execute(text("DELETE FROM ecocompensation_results.troncons_hydro WHERE project_id = :pid"), {"pid": project_id})
        except Exception:
            pass

    log(
        f"📡 Requêtes WFS BDTOPO troncon_hydrographique sur BBOX AOI "
        f"(pagination, PAGE_LIMIT={PAGE_LIMIT}) ..."
    )

    while True:
        page += 1
        params = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": TYPE_NAME,
            "srsName": SRS_WFS,
            "bbox": f"{minx},{miny},{maxx},{maxy},{SRS_WFS}",
            "count": PAGE_LIMIT,
            "startIndex": start_index,
        }

        log(f"\n========== PAGE {page} (startIndex={start_index}) ==========")
        resp = requests.get(WFS_URL, params=params, timeout=300)
        log(f"➡️ URL: {resp.url}")

        if resp.status_code != 200:
            log(f"❌ Erreur HTTP {resp.status_code}")
            log(resp.text[:1000])
            break

        log("📥 Lecture des données WFS (GML) en GeoDataFrame ...")
        gdf = gpd.read_file(resp.url)
        if gdf.empty:
            log("⚠️ Page vide, fin de pagination.")
            break

        if gdf.crs is None or gdf.crs.to_string() != SRS_WFS:
            gdf = gdf.set_crs(SRS_WFS, allow_override=True)

        log(f"🧮 Tronçons bruts dans la BBOX AOI (page {page}) : {len(gdf)}")

        # 4) Reprojection en 2154, intersection exacte avec AOI
        gdf_2154 = gdf.to_crs(SRS_TARGET)
        gdf_2154 = gdf_2154.rename_geometry("geom_2154")

        # =========================================================
        # PATCH : conversion 3D -> 2D (supprime le Z)
        # =========================================================
        gdf_2154["geom_2154"] = gdf_2154["geom_2154"].apply(force_2d)

        # Intersection
        gdf_2154 = gdf_2154[gdf_2154.geom_2154.intersects(aoi_union)]
        log(f"🎯 Tronçons après intersection AOI (page {page}) : {len(gdf_2154)}")

        if gdf_2154.empty:
            if len(gdf) < PAGE_LIMIT:
                log("   ✅ Dernière page vide → fin de pagination.")
                break
            start_index += PAGE_LIMIT
            time.sleep(SLEEP_BETWEEN_PAGES)
            continue

        # Liaison project_id
        gdf_2154["project_id"] = project_id

        # Colonnes booléennes → entier (0/1) pour éviter "invalid input syntax for type double precision: False"
        for col in gdf_2154.select_dtypes(include=["bool"]).columns:
            gdf_2154[col] = gdf_2154[col].astype("int8")

        # 5) Insertion dans ecocompensation_results.troncons_hydro
        log("🏗️ Insertion dans ecocompensation_results.troncons_hydro ...")
        t0 = time.perf_counter()
        gdf_2154.to_postgis(
            name="troncons_hydro",
            con=engine,
            schema="ecocompensation_results",
            if_exists="append",
            index=False,
            chunksize=5000,
        )
        t1 = time.perf_counter()

        log(
            f"✅ {len(gdf_2154)} tronçons insérés (page {page}) "
            f"dans ecocompensation_results.troncons_hydro (en {t1 - t0:.2f} s)."
        )
        total_inserted += len(gdf_2154)

        if len(gdf) < PAGE_LIMIT:
            log("   ✅ Dernière page incomplète → fin de pagination.")
            break

        start_index += PAGE_LIMIT
        time.sleep(SLEEP_BETWEEN_PAGES)

    log(f"\n🎯 Ingestion terminée. Total tronçons insérés : {total_inserted}")
    return total_inserted


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
    print(f"Total inséré : {n}")


if __name__ == "__main__":
    main()

