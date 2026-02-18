#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aoi_to_patrimoine_naturel.py
============================

À partir d'une AOI, interroge les couches
de patrimoine naturel (PNR, PN, RNN, Natura 2000, ZNIEFF, etc.) via WFS
avec carroyage adaptatif, puis insère dans :

    ecocompensation_results.patrimoine_naturel

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
TABLE_FINAL = "ecocompensation_results.patrimoine_naturel"

COUCHES = [
    {"name": "Parcs naturels régionaux",
     "layer": "PROTECTEDAREAS.PNR:pnr",
     "url": "https://data.geopf.fr/wfs/ows",
     "nom_col": "nom"},
    {"name": "Parcs nationaux",
     "layer": "PROTECTEDAREAS.PN:pn",
     "url": "https://data.geopf.fr/wfs/ows",
     "nom_col": "nom"},
    {"name": "Réserves naturelles nationales",
     "layer": "PROTECTEDAREAS.RNN:rnn",
     "url": "https://data.geopf.fr/wfs/ows",
     "nom_col": "nom"},
    {"name": "Sites Natura 2000 (Habitats)",
     "layer": "PROTECTEDAREAS.SIC:sic",
     "url": "https://data.geopf.fr/wfs/ows",
     "nom_col": "sitename"},
    {"name": "Sites Natura 2000 (Oiseaux)",
     "layer": "PROTECTEDAREAS.ZPS:zps",
     "url": "https://data.geopf.fr/wfs/ows",
     "nom_col": "sitename"},
    {"name": "Réserves naturelles régionales",
     "layer": "PROTECTEDSITES.MNHN.RESERVES-REGIONALES:rnr",
     "url": "https://data.geopf.fr/wfs/ows",
     "nom_col": "nom"},
    {"name": "Périmètres de protection de réserves naturelles (PPRNN)",
     "layer": "PROTECTEDAREAS.MNHN.RN.PERIMETER:pprnn",
     "url": "https://data.geopf.fr/wfs/ows",
     "nom_col": "nom"},
    {"name": "Inventaire National du Patrimoine Géologique (INPG)",
     "layer": "PROTECTEDAREAS.INPG:inpg",
     "url": "https://data.geopf.fr/wfs/ows",
     "nom_col": "nom_site"},
    {"name": "ZNIEFF type I",
     "layer": "patrinat_znieff1:znieff1",
     "url": "https://data.geopf.fr/wfs/ows",
     "nom_col": "nom_site"},
    {"name": "ZNIEFF type II",
     "layer": "patrinat_znieff2:znieff2",
     "url": "https://data.geopf.fr/wfs/ows",
     "nom_col": "nom_site"},
    {"name": "Prairies et pâturages sensibles",
     "layer": "PRAIRIES.SENSIBLES.BCAE:prairies_sensibles",
     "url": "https://data.geopf.fr/wfs/ows",
     "nom_col": None, "id_col": "id"},
    {"name": "Terrains des Conservatoires d’espaces naturels",
     "layer": "PROTECTEDAREAS.MNHN.CONSERVATOIRES:cen",
     "url": "https://data.geopf.fr/wfs/ows",
     "nom_col": "nom", "id_col": "id_mnhn"},
    {"name": "Conservatoire du littoral - sites sous responsabilité du conservatoire",
     "layer": "PROTECTEDAREAS.MNHN.CDL.PARCELS:cdl",
     "url": "https://data.geopf.fr/wfs/ows",
     "nom_col": "nom", "id_col": "id_mnhn"},
    {"name": "Zones humides et tourbières BCAE",
     "layer": "TOURBIERES_ZONES-HUMIDES.BCAE:bcae",
     "url": "https://data.geopf.fr/wfs/ows",
     "nom_col": "type_zone"},
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
    gdf, _ = harvest_adaptive(cfg["url"], cfg["layer"], bbox_wgs84, srs="EPSG:4326", cap=CAP)
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


def run(engine, aoi_id: str, cb=None) -> int:
    """
    Récupère le patrimoine naturel intersectant l'AOI donnée
    et l'insère dans ecocompensation_results.patrimoine_naturel.

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
        log(f"⚠️ Aucune AOI trouvée dans ecocompensation.aoi pour id={aoi_id}, annulation.")
        return 0

    if aoi.crs is None or aoi.crs.to_string() != "EPSG:2154":
        aoi = aoi.set_crs("EPSG:2154", allow_override=True)

    aoi_union = aoi.union_all()

    aoi_wgs84 = aoi.to_crs("EPSG:4326")
    minx, miny, maxx, maxy = aoi_wgs84.total_bounds
    bbox_wgs84 = (minx, miny, maxx, maxy)
    log(f"🔗 AOI utilisée : id={aoi_id}")
    log(f"🧭 BBOX AOI en WGS84 : {bbox_wgs84}")

    # 2) Collecte des couches PN / Natura / ZNIEFF... sur la BBOX AOI
    all_parts: list[gpd.GeoDataFrame] = []
    for i, cfg in enumerate(COUCHES, 1):
        log(f"[{i}/{len(COUCHES)}] {cfg['name']}...")
        gdf = fetch_layer(cfg, bbox_wgs84, log=log)
        if not gdf.empty:
            all_parts.append(gdf)

    if not all_parts:
        log("⚠️ Aucune géométrie de patrimoine naturel collectée dans la BBOX AOI.")
        return 0

    gdf_final = pd.concat(all_parts, ignore_index=True)

    # 3) Passage en 2154 + intersection stricte avec AOI
    gdf_2154 = gdf_final.to_crs(2154)
    gdf_2154 = gdf_2154.rename_geometry("geom_2154")
    before = len(gdf_2154)
    gdf_2154 = gdf_2154[gdf_2154.geom_2154.intersects(aoi_union)]
    after = len(gdf_2154)
    log(f"🎯 Patrimoine naturel intersectant l'AOI : {after}/{before} entités")

    if gdf_2154.empty:
        log("⚠️ Aucune entité de patrimoine naturel n'intersecte l'AOI.")
        return 0

    # 4) Ajout de aoi_id et insertion dans ecocompensation_results.patrimoine_naturel
    gdf_2154["aoi_id"] = aoi_id

    with engine.begin() as conn:
        conn.execute(
            text(
                f"""
                CREATE SCHEMA IF NOT EXISTS ecocompensation_results;
                CREATE TABLE IF NOT EXISTS {TABLE_FINAL} (
                    id uuid NOT NULL DEFAULT gen_random_uuid(),
                    aoi_id text NOT NULL,
                    uid text NOT NULL,
                    type_patrimoine text NULL,
                    nom text NULL,
                    updated_at timestamp NULL,
                    geom_2154 geometry(Geometry,2154) NOT NULL,
                    created_at timestamptz NULL DEFAULT now(),
                    PRIMARY KEY (id)
                );

                CREATE INDEX IF NOT EXISTS patrimoine_naturel_geom_gix
                    ON {TABLE_FINAL}
                    USING GIST (geom_2154);

                CREATE INDEX IF NOT EXISTS patrimoine_naturel_aoi_idx
                    ON {TABLE_FINAL} (aoi_id);
                """
            )
        )

    log("🏗️ Insertion dans ecocompensation_results.patrimoine_naturel ...")
    t0 = datetime.now().timestamp()
    gdf_2154[["aoi_id", "uid", "type_patrimoine", "nom", "updated_at", "geom_2154"]].to_postgis(
        name="patrimoine_naturel",
        con=engine,
        schema="ecocompensation_results",
        if_exists="append",
        index=False,
        chunksize=5000,
    )
    t1 = datetime.now().timestamp()
    n = len(gdf_2154)
    log(
        f"✅ {n} entités insérées dans ecocompensation_results.patrimoine_naturel "
        f"pour aoi_id={aoi_id} (en {t1 - t0:.2f} s)."
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
        raise RuntimeError("Variables de connexion à la base manquantes dans le .env (PATRIMOINE_NATUREL).")

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

    n = run(engine, str(aoi_id), cb=logging.info)
    print(f"Total patrimoine naturel inséré : {n}")


if __name__ == "__main__":
    main()