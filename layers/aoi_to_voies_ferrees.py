#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Récupère les tronçons de voie ferrée BDTOPO (WFS data.geopf.fr) intersectant l'AOI
et les insère dans ecocompensation_results.voies_ferrees.
Attributs conservés : cleabs, nature, nombre_de_voies.
"""

import os
import time
from pathlib import Path
from urllib.parse import quote_plus

import geopandas as gpd
import requests
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
from shapely import force_2d

# =============================
# CONFIG
# =============================

WFS_URL = "https://data.geopf.fr/wfs/ows"
TYPE_NAME = "BDTOPO_V3:troncon_de_voie_ferree"
SRS_WFS = "EPSG:3857"
SRS_TARGET = "EPSG:2154"

VOIES_FERREES_COLUMNS = ["cleabs", "nature", "nombre_de_voies"]

PAGE_LIMIT = 5000
SLEEP_BETWEEN_PAGES = 2.0


def run(engine, project_id: str, aoi_id: str, cb=None) -> int:
    """
    Récupère les tronçons de voie ferrée BDTOPO intersectant l'AOI
    et les insère dans ecocompensation_results.voies_ferrees.

    :param engine: Engine SQLAlchemy déjà connecté.
    :param project_id: Identifiant du projet (écrit dans les lignes de résultat).
    :param aoi_id: Identifiant de l'AOI pour l'intersection géométrique.
    :param cb: Callback de log optionnel (cb(str)).
    :return: Nombre total de tronçons insérés.
    """
    log = cb or (lambda msg: None)

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
    aoi_3857 = aoi.to_crs(SRS_WFS)
    minx, miny, maxx, maxy = aoi_3857.total_bounds
    log(f"🔗 AOI utilisée : id={aoi_id}")
    log(f"🧭 BBOX AOI en {SRS_WFS} : {minx}, {miny}, {maxx}, {maxy}")

    total_inserted = 0
    page = 0
    start_index = 0

    with engine.begin() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS ecocompensation_results;"))
        try:
            conn.execute(text("DELETE FROM ecocompensation_results.voies_ferrees WHERE project_id = :pid"), {"pid": project_id})
        except Exception:
            pass

    log(
        f"📡 Requêtes WFS BDTOPO troncon_de_voie_ferree sur BBOX AOI "
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

        log(f"🧮 Tronçons de voie ferrée bruts dans la BBOX (page {page}) : {len(gdf)}")

        gdf_2154 = gdf.to_crs(SRS_TARGET)
        gdf_2154 = gdf_2154.rename_geometry("geom_2154")
        gdf_2154["geom_2154"] = gdf_2154["geom_2154"].apply(force_2d)

        gdf_2154 = gdf_2154[gdf_2154.geom_2154.intersects(aoi_union)]
        log(f"🎯 Tronçons après intersection AOI (page {page}) : {len(gdf_2154)}")

        if gdf_2154.empty:
            if len(gdf) < PAGE_LIMIT:
                log("   ✅ Dernière page vide → fin de pagination.")
                break
            start_index += PAGE_LIMIT
            time.sleep(SLEEP_BETWEEN_PAGES)
            continue

        gdf_2154["project_id"] = project_id

        for col in VOIES_FERREES_COLUMNS:
            if col not in gdf_2154.columns:
                gdf_2154[col] = None
        cols_final = ["project_id"] + VOIES_FERREES_COLUMNS + ["geom_2154"]
        gdf_2154 = gdf_2154[[c for c in cols_final if c in gdf_2154.columns]]

        log("🏗️ Insertion dans ecocompensation_results.voies_ferrees ...")
        t0 = time.perf_counter()
        gdf_2154.to_postgis(
            name="voies_ferrees",
            con=engine,
            schema="ecocompensation_results",
            if_exists="append",
            index=False,
            chunksize=5000,
        )
        t1 = time.perf_counter()

        log(
            f"✅ {len(gdf_2154)} tronçons insérés (page {page}) "
            f"dans ecocompensation_results.voies_ferrees (en {t1 - t0:.2f} s)."
        )
        total_inserted += len(gdf_2154)

        if len(gdf) < PAGE_LIMIT:
            log("   ✅ Dernière page incomplète → fin de pagination.")
            break

        start_index += PAGE_LIMIT
        time.sleep(SLEEP_BETWEEN_PAGES)

    log(f"\n🎯 Ingestion terminée. Total tronçons de voie ferrée insérés : {total_inserted}")
    return total_inserted


def main():
    BASE_DIR = Path(__file__).resolve().parent
    load_dotenv(BASE_DIR / ".env")

    SUPABASE_HOST = os.getenv("SUPABASE_HOST")
    SUPABASE_PORT = os.getenv("SUPABASE_PORT", "6543")
    SUPABASE_DB = os.getenv("SUPABASE_DB", "postgres")
    SUPABASE_USER = os.getenv("SUPABASE_USER")
    SUPABASE_PASSWORD = os.getenv("SUPABASE_PASSWORD")

    if not all([SUPABASE_HOST, SUPABASE_DB, SUPABASE_USER, SUPABASE_PASSWORD]):
        raise RuntimeError("Variables de connexion à la base manquantes dans le .env.")

    password_quoted = quote_plus(SUPABASE_PASSWORD)
    db_url = (
        f"postgresql+psycopg://{SUPABASE_USER}:{password_quoted}"
        f"@{SUPABASE_HOST}:{SUPABASE_PORT}/{SUPABASE_DB}"
    )
    engine = create_engine(db_url)

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
