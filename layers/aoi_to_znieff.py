#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aoi_to_znieff.py
================

ZNIEFF type I et II (Patrinat / Géoplateforme) — deux flux WFS au même format,
agrégés dans une seule table avec une colonne ``znieff_type`` (1 ou 2).

WFS :
  - patrinat_znieff1:znieff1
  - patrinat_znieff2:znieff2
  URL : https://data.geopf.fr/wfs/ows (EPSG:3857)

→ ecocompensation_results.znieff (geom_2154, nom_site, znieff_type uniquement).
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
TABLE_FULL = "ecocompensation_results.znieff"

COUCHES: list[dict] = [
    {
        "name": "ZNIEFF type I",
        "layer": "patrinat_znieff1:znieff1",
        "znieff_type": 1,
    },
    {
        "name": "ZNIEFF type II",
        "layer": "patrinat_znieff2:znieff2",
        "znieff_type": 2,
    },
]


def _nom_site_col(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Expose ``nom_site`` (casse WFS variable)."""
    lower = {c.lower(): c for c in gdf.columns}
    if "nom_site" in lower:
        src = lower["nom_site"]
        if src != "nom_site":
            gdf = gdf.rename(columns={src: "nom_site"})
    elif "nom_site" not in gdf.columns:
        gdf["nom_site"] = None
    return gdf


def _fetch_one_layer(
    layer: str, znieff_type: int, bbox_3857: tuple[float, float, float, float], log
) -> gpd.GeoDataFrame:
    log(f"📡 WFS {layer} (type {znieff_type}, carroyage adaptatif, cap={CAP}) ...")
    gdf, _ = harvest_adaptive(WFS_URL, layer, bbox_3857, srs=SRS_WFS, cap=CAP)
    if gdf.empty:
        log(f"⚠️ Aucune entité dans la BBOX pour {layer}")
        return gpd.GeoDataFrame()
    gdf = dedup_on_id_or_geom(gdf)
    gdf = _nom_site_col(gdf)
    gdf["znieff_type"] = znieff_type
    return gdf


def _ensure_table(conn) -> None:
    conn.execute(
        text(
            """
            CREATE SCHEMA IF NOT EXISTS ecocompensation_results;

            CREATE TABLE IF NOT EXISTS ecocompensation_results.znieff (
                id uuid NOT NULL DEFAULT gen_random_uuid(),
                project_id uuid NULL,
                aoi_id uuid NULL,
                znieff_type smallint NULL,
                nom_site text NULL,
                geom_2154 geometry(Geometry, 2154) NOT NULL,
                created_at timestamptz NULL DEFAULT now(),
                PRIMARY KEY (id)
            );
            """
        )
    )
    conn.execute(
        text(
            """
            ALTER TABLE ecocompensation_results.znieff
                ADD COLUMN IF NOT EXISTS znieff_type smallint;
            ALTER TABLE ecocompensation_results.znieff
                ADD COLUMN IF NOT EXISTS nom_site text;
            ALTER TABLE ecocompensation_results.znieff
                ADD COLUMN IF NOT EXISTS aoi_id uuid;
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS znieff_geom_gix
                ON ecocompensation_results.znieff USING GIST (geom_2154);
            CREATE INDEX IF NOT EXISTS znieff_project_idx
                ON ecocompensation_results.znieff (project_id);
            CREATE INDEX IF NOT EXISTS znieff_type_idx
                ON ecocompensation_results.znieff (znieff_type);
            """
        )
    )
    conn.execute(
        text(
            """
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'ecocompensation_results'
                  AND table_name = 'znieff' AND column_name = 'nom'
              ) THEN
                UPDATE ecocompensation_results.znieff
                SET nom_site = nom
                WHERE nom_site IS NULL AND nom IS NOT NULL;
              END IF;
              IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'ecocompensation_results'
                  AND table_name = 'znieff' AND column_name = 'type_patrimoine'
              ) THEN
                UPDATE ecocompensation_results.znieff SET znieff_type = 2
                WHERE znieff_type IS NULL
                  AND (type_patrimoine ILIKE '%II%' OR type_patrimoine ILIKE '%type II%');
                UPDATE ecocompensation_results.znieff SET znieff_type = 1
                WHERE znieff_type IS NULL;
              END IF;
            END $$;
            """
        )
    )
    conn.execute(
        text(
            """
            UPDATE ecocompensation_results.znieff SET znieff_type = 1
            WHERE znieff_type IS NULL;

            ALTER TABLE ecocompensation_results.znieff DROP COLUMN IF EXISTS uid;
            ALTER TABLE ecocompensation_results.znieff DROP COLUMN IF EXISTS type_patrimoine;
            ALTER TABLE ecocompensation_results.znieff DROP COLUMN IF EXISTS nom;
            ALTER TABLE ecocompensation_results.znieff DROP COLUMN IF EXISTS updated_at;

            ALTER TABLE ecocompensation_results.znieff DROP CONSTRAINT IF EXISTS znieff_type_chk;
            ALTER TABLE ecocompensation_results.znieff
              ALTER COLUMN znieff_type SET NOT NULL;
            ALTER TABLE ecocompensation_results.znieff
              ADD CONSTRAINT znieff_type_chk CHECK (znieff_type IN (1, 2));
            """
        )
    )


def run(engine, project_id: str, aoi_id: str, cb=None) -> int:
    """
    Récupère les ZNIEFF I et II intersectant l'AOI et les insère dans
    ecocompensation_results.znieff (nom_site, geom_2154, znieff_type).
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
        gdf = _fetch_one_layer(cfg["layer"], cfg["znieff_type"], bbox_3857, log)
        if not gdf.empty:
            parts.append(gdf)

    if not parts:
        log("⚠️ Aucune géométrie ZNIEFF dans la BBOX AOI.")
        return 0

    gdf_all = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=SRS_WFS)
    gdf_2154 = gdf_all.to_crs(SRS_TARGET).rename_geometry("geom_2154")

    keep = ["nom_site", "znieff_type", "geom_2154"]
    gdf_2154 = gdf_2154[[c for c in keep if c in gdf_2154.columns]].copy()

    before = len(gdf_2154)
    gdf_2154 = gdf_2154[gdf_2154.geom_2154.intersects(aoi_union)].copy()
    after = len(gdf_2154)
    log(f"🎯 ZNIEFF intersectant l'AOI : {after}/{before}")
    if gdf_2154.empty:
        log("⚠️ Aucune entité ZNIEFF n'intersecte l'AOI.")
        return 0

    gdf_2154["project_id"] = project_id
    gdf_2154["aoi_id"] = aoi_id

    insert_cols = ["project_id", "aoi_id", "znieff_type", "nom_site", "geom_2154"]

    with engine.begin() as conn:
        _ensure_table(conn)
        conn.execute(
            text(f"DELETE FROM {TABLE_FULL} WHERE project_id = :pid"),
            {"pid": project_id},
        )

    out = gdf_2154[insert_cols].copy()
    out.to_postgis(
        name="znieff",
        con=engine,
        schema="ecocompensation_results",
        if_exists="append",
        index=False,
        chunksize=3000,
    )
    n = len(out)
    log(f"✅ {n} entités insérées dans {TABLE_FULL} (znieff_type 1 ou 2).")
    return n


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
        raise RuntimeError("Variables DB manquantes (.env) pour znieff")

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
    print(f"Total ZNIEFF insérées : {n}")


if __name__ == "__main__":
    main()
