#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aoi_to_znieff.py
================

À partir d'une AOI, interroge uniquement les couches ZNIEFF (type I et II)
via WFS avec carroyage adaptatif, puis insère dans :

    ecocompensation_results.znieff

uniquement les entités qui intersectent l'AOI.
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus
import hashlib

import geopandas as gpd
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from carroyage_utils import harvest_adaptive, dedup_on_id_or_geom

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# -----------------------------
# CONFIG
# -----------------------------

CAP = 5000
TABLE_FINAL = "ecocompensation_results.znieff"

COUCHES = [
    {
        "name": "ZNIEFF type I",
        "layer": "patrinat_znieff1:znieff1",
        "url": "https://data.geopf.fr/wfs/ows",
        "nom_col": "nom_site",
    },
    {
        "name": "ZNIEFF type II",
        "layer": "patrinat_znieff2:znieff2",
        "url": "https://data.geopf.fr/wfs/ows",
        "nom_col": "nom_site",
    },
]

BASE_DIR = Path(__file__).resolve().parent


# -----------------------------
# Helpers
# -----------------------------


def make_uid(layer: str, row) -> str:
    """UID stable par hash layer+id (reprend la logique du pipeline original)."""
    id_col = None
    for c in ("id", "id_mnhn", "id_local", "sitecode"):
        if c in row and pd.notna(row[c]):
            id_col = str(row[c])
            break
    if not id_col:
        id_col = str(hashlib.sha1(str(row.values).encode()).hexdigest()[:12])
    return hashlib.sha1(f"{layer}_{id_col}".encode()).hexdigest()


def normalize_geom(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Force EPSG:2154 et geometry multi (MultiPolygon ou MultiGeometry)."""
    if gdf.empty:
        return gdf
    gdf = gdf.to_crs(2154)
    return gdf


def fetch_layer(cfg: dict, bbox_wgs84, log=None) -> gpd.GeoDataFrame:
    """Carroyage + dédup + sélection colonnes, limité à la BBOX de l'AOI."""
    _log = log or logging.info
    _log(f"=== 🔎 {cfg['layer']} ===")
    gdf, _ = harvest_adaptive(
        cfg["url"], cfg["layer"], bbox_wgs84, srs="EPSG:4326", cap=CAP
    )
    if gdf.empty:
        _log(f"⚠️ Pas de données pour {cfg['layer']} dans la BBOX AOI")
        return gpd.GeoDataFrame()

    gdf = dedup_on_id_or_geom(gdf)

    # UID basé sur l'identifiant technique de la couche
    gdf["uid"] = gdf.apply(lambda r: make_uid(cfg["layer"], r), axis=1)
    # Colonne type_patrimoine: nom descriptif de la couche depuis la config
    gdf["type_patrimoine"] = cfg.get("name") or cfg["layer"]
    if cfg.get("nom_col") and cfg["nom_col"] in gdf.columns:
        gdf["nom"] = gdf[cfg["nom_col"]]
    else:
        gdf["nom"] = None
    gdf["updated_at"] = datetime.utcnow()

    gdf = normalize_geom(gdf[["uid", "type_patrimoine", "nom", "updated_at", "geometry"]])
    return gdf


def run(engine, project_id: str, aoi_id: str, cb=None) -> int:
    """
    Récupère les ZNIEFF intersectant l'AOI donnée
    et les insère dans ecocompensation_results.znieff.

    :param engine: Engine SQLAlchemy déjà connecté.
    :param project_id: Identifiant du projet (écrit dans les lignes de résultat).
    :param aoi_id: Identifiant de l'AOI pour l'intersection géométrique.
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
        log(
            f"⚠️ Aucune AOI trouvée dans ecocompensation.aoi pour id={aoi_id}, annulation."
        )
        return 0

    if aoi.crs is None or aoi.crs.to_string() != "EPSG:2154":
        aoi = aoi.set_crs("EPSG:2154", allow_override=True)

    aoi_union = aoi.union_all()

    aoi_wgs84 = aoi.to_crs("EPSG:4326")
    minx, miny, maxx, maxy = aoi_wgs84.total_bounds
    bbox_wgs84 = (minx, miny, maxx, maxy)
    log(f"🔗 AOI utilisée : id={aoi_id}")
    log(f"🧭 BBOX AOI en WGS84 : {bbox_wgs84}")

    # 2) Collecte des couches ZNIEFF sur la BBOX AOI
    all_parts: list[gpd.GeoDataFrame] = []
    for i, cfg in enumerate(COUCHES, 1):
        log(f"[{i}/{len(COUCHES)}] {cfg['name']}...")
        gdf = fetch_layer(cfg, bbox_wgs84, log=log)
        if not gdf.empty:
            all_parts.append(gdf)

    if not all_parts:
        log("⚠️ Aucune géométrie ZNIEFF collectée dans la BBOX AOI.")
        return 0

    gdf_final = pd.concat(all_parts, ignore_index=True)

    # 3) Passage en 2154 + intersection stricte avec AOI
    gdf_2154 = gdf_final.to_crs(2154)
    gdf_2154 = gdf_2154.rename_geometry("geom_2154")
    before = len(gdf_2154)
    gdf_2154 = gdf_2154[gdf_2154.geom_2154.intersects(aoi_union)]
    after = len(gdf_2154)
    log(f"🎯 ZNIEFF intersectant l'AOI : {after}/{before} entités")

    if gdf_2154.empty:
        log("⚠️ Aucune entité ZNIEFF n'intersecte l'AOI.")
        return 0

    # 4) Ajout de project_id et insertion dans ecocompensation_results.znieff
    gdf_2154["project_id"] = project_id

    with engine.begin() as conn:
        conn.execute(
            text(
                f"""
                CREATE SCHEMA IF NOT EXISTS ecocompensation_results;
                CREATE TABLE IF NOT EXISTS {TABLE_FINAL} (
                    id uuid NOT NULL DEFAULT gen_random_uuid(),
                    project_id uuid NULL,
                    uid text NOT NULL,
                    type_patrimoine text NULL,
                    nom text NULL,
                    updated_at timestamp NULL,
                    geom_2154 geometry(Geometry,2154) NOT NULL,
                    created_at timestamptz NULL DEFAULT now(),
                    PRIMARY KEY (id)
                );

                CREATE INDEX IF NOT EXISTS znieff_geom_gix
                    ON {TABLE_FINAL}
                    USING GIST (geom_2154);

                CREATE INDEX IF NOT EXISTS znieff_project_idx
                    ON {TABLE_FINAL} (project_id);
                """
            )
        )
        conn.execute(text(f"DELETE FROM {TABLE_FINAL} WHERE project_id = :pid"), {"pid": project_id})

    log("🏗️ Insertion dans ecocompensation_results.znieff ...")
    t0 = datetime.now().timestamp()
    gdf_2154[
        ["project_id", "uid", "type_patrimoine", "nom", "updated_at", "geom_2154"]
    ].to_postgis(
        name="znieff",
        con=engine,
        schema="ecocompensation_results",
        if_exists="append",
        index=False,
        chunksize=5000,
    )
    t1 = datetime.now().timestamp()
    n = len(gdf_2154)
    log(
        f"✅ {n} entités insérées dans ecocompensation_results.znieff "
        f"pour project_id={project_id} (en {t1 - t0:.2f} s)."
    )
    return n


def main():
    """
    Entrée CLI : construit son propre engine et utilise la dernière AOI.
    """
    load_dotenv(BASE_DIR / ".env")

    SUPABASE_HOST = os.getenv("SUPABASE_HOST")
    SUPABASE_PORT = os.getenv("SUPABASE_PORT", "6543")
    SUPABASE_DB = os.getenv("SUPABASE_DB", "postgres")
    SUPABASE_USER = os.getenv("SUPABASE_USER")
    SUPABASE_PASSWORD = os.getenv("SUPABASE_PASSWORD")

    if not all([SUPABASE_HOST, SUPABASE_DB, SUPABASE_USER, SUPABASE_PASSWORD]):
        raise RuntimeError(
            "Variables de connexion à la base manquantes dans le .env (ZNIEFF)."
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

    n = run(engine, project_id, aoi_id, cb=logging.info)
    print(f"Total ZNIEFF insérées : {n}")


if __name__ == "__main__":
    main()

