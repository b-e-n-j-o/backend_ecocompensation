#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aoi_to_natura_2000.py
=====================

Réseau Natura 2000 Patrinat (MNHN / Géoplateforme) : deux flux WFS au même
schéma attributaire, fusionnés dans une seule table :

  - **SIC** (Sites d’importance communautaire — habitats) : ``patrinat_sic:sic``
  - **ZPS** (Zones de protection spéciale — oiseaux) : ``patrinat_zps:zps``

WFS : https://data.geopf.fr/wfs/ows (EPSG:3857)
→ ``ecocompensation_results.natura2000``

Colonne ``natura_categorie`` : ``habitats`` (SIC) ou ``oiseaux`` (ZPS).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import quote_plus

import geopandas as gpd
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

try:
    from .carroyage_utils import harvest_adaptive, dedup_on_id_or_geom
except Exception:
    from carroyage_utils import harvest_adaptive, dedup_on_id_or_geom

WFS_URL = "https://data.geopf.fr/wfs/ows"
SRS_WFS = "EPSG:3857"
SRS_TARGET = "EPSG:2154"
CAP = 5000
TABLE_FULL = "ecocompensation_results.natura2000"

COUCHES: list[dict] = [
    {
        "name": "SIC (habitats)",
        "layer": "patrinat_sic:sic",
        "natura_categorie": "habitats",
    },
    {
        "name": "ZPS (oiseaux)",
        "layer": "patrinat_zps:zps",
        "natura_categorie": "oiseaux",
    },
]

# Attributs WFS à conserver (hors géométrie).
# « precision » est renommé en precision_qual (mot réservé SQL).
ATTR_COLS = [
    "id_local",
    "nom_site",
    "date_crea",
    "modif_adm",
    "modif_geo",
    "url_fiche",
    "surf_off",
    "acte_deb",
    "acte_fin",
    "gest_site",
    "operateur",
    "precision",
    "src_geom",
    "src_annee",
    "marin",
    "p1_nature",
    "p4_geologi",
    "id_mnhn",
    "area_sig",
    "cd_sig",
    "territoire",
]


def _ensure_attr_columns(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Normalise les noms de colonnes (casse) et remplit les manquants."""
    lower_map = {c.lower(): c for c in gdf.columns}
    out = gdf.copy()
    for want in ATTR_COLS:
        key = want.lower()
        if key in lower_map:
            src = lower_map[key]
            if src != want:
                out[want] = out[src]
        elif want not in out.columns:
            out[want] = None
    if "precision" in out.columns:
        out["precision_qual"] = out["precision"]
    else:
        out["precision_qual"] = None
    return out


def _fetch_layer(
    layer: str,
    natura_categorie: str,
    bbox_3857: tuple[float, float, float, float],
    log,
) -> gpd.GeoDataFrame:
    log(f"📡 WFS {layer} — {natura_categorie} (carroyage adaptatif, cap={CAP}) ...")
    gdf, _ = harvest_adaptive(WFS_URL, layer, bbox_3857, srs=SRS_WFS, cap=CAP)
    if gdf.empty:
        log(f"⚠️ Aucune entité dans la BBOX pour {layer}")
        return gpd.GeoDataFrame()
    gdf = dedup_on_id_or_geom(gdf)
    gdf = _ensure_attr_columns(gdf)
    gdf["natura_categorie"] = natura_categorie
    return gdf


def _ensure_table(conn) -> None:
    conn.execute(
        text(
            """
            CREATE SCHEMA IF NOT EXISTS ecocompensation_results;

            CREATE TABLE IF NOT EXISTS ecocompensation_results.natura2000 (
                rid uuid NOT NULL DEFAULT gen_random_uuid(),
                project_id uuid NULL,
                aoi_id uuid NULL,
                natura_categorie text NULL,
                id_local text NULL,
                nom_site text NULL,
                date_crea date NULL,
                modif_adm date NULL,
                modif_geo date NULL,
                url_fiche text NULL,
                surf_off double precision NULL,
                acte_deb text NULL,
                acte_fin text NULL,
                gest_site text NULL,
                operateur text NULL,
                precision_qual text NULL,
                src_geom text NULL,
                src_annee text NULL,
                marin text NULL,
                p1_nature text NULL,
                p4_geologi text NULL,
                id_mnhn text NULL,
                area_sig double precision NULL,
                cd_sig text NULL,
                territoire text NULL,
                geom_2154 geometry(Geometry, 2154) NOT NULL,
                created_at timestamptz NULL DEFAULT now(),
                PRIMARY KEY (rid)
            );
            """
        )
    )
    conn.execute(
        text(
            """
            ALTER TABLE ecocompensation_results.natura2000
                ADD COLUMN IF NOT EXISTS natura_categorie text;
            ALTER TABLE ecocompensation_results.natura2000
                ADD COLUMN IF NOT EXISTS id_local text;
            ALTER TABLE ecocompensation_results.natura2000
                ADD COLUMN IF NOT EXISTS nom_site text;
            ALTER TABLE ecocompensation_results.natura2000
                ADD COLUMN IF NOT EXISTS date_crea date;
            ALTER TABLE ecocompensation_results.natura2000
                ADD COLUMN IF NOT EXISTS modif_adm date;
            ALTER TABLE ecocompensation_results.natura2000
                ADD COLUMN IF NOT EXISTS modif_geo date;
            ALTER TABLE ecocompensation_results.natura2000
                ADD COLUMN IF NOT EXISTS url_fiche text;
            ALTER TABLE ecocompensation_results.natura2000
                ADD COLUMN IF NOT EXISTS surf_off double precision;
            ALTER TABLE ecocompensation_results.natura2000
                ADD COLUMN IF NOT EXISTS acte_deb text;
            ALTER TABLE ecocompensation_results.natura2000
                ADD COLUMN IF NOT EXISTS acte_fin text;
            ALTER TABLE ecocompensation_results.natura2000
                ADD COLUMN IF NOT EXISTS gest_site text;
            ALTER TABLE ecocompensation_results.natura2000
                ADD COLUMN IF NOT EXISTS operateur text;
            ALTER TABLE ecocompensation_results.natura2000
                ADD COLUMN IF NOT EXISTS precision_qual text;
            ALTER TABLE ecocompensation_results.natura2000
                ADD COLUMN IF NOT EXISTS src_geom text;
            ALTER TABLE ecocompensation_results.natura2000
                ADD COLUMN IF NOT EXISTS src_annee text;
            ALTER TABLE ecocompensation_results.natura2000
                ADD COLUMN IF NOT EXISTS marin text;
            ALTER TABLE ecocompensation_results.natura2000
                ADD COLUMN IF NOT EXISTS p1_nature text;
            ALTER TABLE ecocompensation_results.natura2000
                ADD COLUMN IF NOT EXISTS p4_geologi text;
            ALTER TABLE ecocompensation_results.natura2000
                ADD COLUMN IF NOT EXISTS id_mnhn text;
            ALTER TABLE ecocompensation_results.natura2000
                ADD COLUMN IF NOT EXISTS area_sig double precision;
            ALTER TABLE ecocompensation_results.natura2000
                ADD COLUMN IF NOT EXISTS cd_sig text;
            ALTER TABLE ecocompensation_results.natura2000
                ADD COLUMN IF NOT EXISTS territoire text;
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS idx_natura2000_geom
                ON ecocompensation_results.natura2000 USING GIST (geom_2154);
            CREATE INDEX IF NOT EXISTS idx_natura2000_project
                ON ecocompensation_results.natura2000 (project_id);
            CREATE INDEX IF NOT EXISTS idx_natura2000_cd_sig
                ON ecocompensation_results.natura2000 (cd_sig);
            CREATE INDEX IF NOT EXISTS idx_natura2000_id_mnhn
                ON ecocompensation_results.natura2000 (id_mnhn);
            CREATE INDEX IF NOT EXISTS idx_natura2000_categorie
                ON ecocompensation_results.natura2000 (natura_categorie);
            """
        )
    )
    conn.execute(
        text(
            """
            ALTER TABLE ecocompensation_results.natura2000 DROP COLUMN IF EXISTS num_prs;
            ALTER TABLE ecocompensation_results.natura2000 DROP COLUMN IF EXISTS surf_graph;
            ALTER TABLE ecocompensation_results.natura2000 DROP COLUMN IF EXISTS date_maj;
            ALTER TABLE ecocompensation_results.natura2000 DROP COLUMN IF EXISTS id;
            """
        )
    )


def run(engine, project_id: str, aoi_id: str, cb=None) -> int:
    """
    Récupère les sites SIC et ZPS Patrinat intersectant l'AOI et les insère dans
    ecocompensation_results.natura2000 (colonne natura_categorie : habitats | oiseaux).
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
        log(f"⚠️ Aucune AOI trouvée pour id={aoi_id}, annulation.")
        return 0

    if aoi.crs is None or aoi.crs.to_string() != SRS_TARGET:
        aoi = aoi.set_crs(SRS_TARGET, allow_override=True)

    aoi_union = aoi.union_all()
    aoi_3857 = aoi.to_crs(SRS_WFS)
    minx, miny, maxx, maxy = aoi_3857.total_bounds
    bbox_3857 = (minx, miny, maxx, maxy)
    log(f"🔗 AOI utilisée : id={aoi_id}")
    log(f"🧭 BBOX AOI en {SRS_WFS} : {bbox_3857}")

    parts: list[gpd.GeoDataFrame] = []
    for i, cfg in enumerate(COUCHES, 1):
        log(f"[{i}/{len(COUCHES)}] {cfg['name']} ...")
        gdf = _fetch_layer(
            cfg["layer"], cfg["natura_categorie"], bbox_3857, log
        )
        if not gdf.empty:
            parts.append(gdf)

    if not parts:
        log("⚠️ Aucune entité Natura 2000 (SIC/ZPS) dans la BBOX AOI.")
        return 0

    gdf_all = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=SRS_WFS)
    gdf_2154 = gdf_all.to_crs(SRS_TARGET).rename_geometry("geom_2154")

    before = len(gdf_2154)
    gdf_2154 = gdf_2154[gdf_2154.geom_2154.intersects(aoi_union)].copy()
    after = len(gdf_2154)
    log(f"🎯 Sites intersectant l'AOI : {after}/{before}")
    if gdf_2154.empty:
        return 0

    gdf_2154["project_id"] = project_id
    gdf_2154["aoi_id"] = aoi_id

    insert_cols = [
        "project_id",
        "aoi_id",
        "natura_categorie",
        "id_local",
        "nom_site",
        "date_crea",
        "modif_adm",
        "modif_geo",
        "url_fiche",
        "surf_off",
        "acte_deb",
        "acte_fin",
        "gest_site",
        "operateur",
        "precision_qual",
        "src_geom",
        "src_annee",
        "marin",
        "p1_nature",
        "p4_geologi",
        "id_mnhn",
        "area_sig",
        "cd_sig",
        "territoire",
        "geom_2154",
    ]

    for dc in ("date_crea", "modif_adm", "modif_geo"):
        if dc in gdf_2154.columns:
            gdf_2154[dc] = pd.to_datetime(gdf_2154[dc], errors="coerce").dt.date

    with engine.begin() as conn:
        _ensure_table(conn)
        conn.execute(
            text(f"DELETE FROM {TABLE_FULL} WHERE project_id = :pid"),
            {"pid": project_id},
        )

    out = gdf_2154[[c for c in insert_cols if c in gdf_2154.columns]].copy()
    out.to_postgis(
        name="natura2000",
        con=engine,
        schema="ecocompensation_results",
        if_exists="append",
        index=False,
        chunksize=3000,
    )
    log(f"✅ {len(out)} sites insérés dans {TABLE_FULL} (SIC + ZPS).")
    return len(out)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    base_dir = Path(__file__).resolve().parent
    load_dotenv(base_dir / ".env")

    host = os.getenv("SUPABASE_HOST")
    port = os.getenv("SUPABASE_PORT", "6543")
    db = os.getenv("SUPABASE_DB", "postgres")
    user = os.getenv("SUPABASE_USER")
    pwd = os.getenv("SUPABASE_PASSWORD")
    if not all([host, db, user, pwd]):
        raise RuntimeError("Variables DB manquantes (.env) pour natura2000")

    db_url = f"postgresql+psycopg://{user}:{quote_plus(pwd)}@{host}:{port}/{db}"
    engine = create_engine(db_url)

    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, aoi_id
                FROM ecocompensation.projects
                WHERE aoi_id IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 1;
                """
            )
        ).mappings().one_or_none()
    if not row:
        print("Aucun projet avec AOI trouvé.")
        return
    project_id = str(row["id"])
    aoi_id = str(row["aoi_id"])
    n = run(engine, project_id, aoi_id, cb=print)
    print(f"Total Natura 2000 insérés : {n}")


if __name__ == "__main__":
    main()
