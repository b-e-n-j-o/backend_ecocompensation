#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aoi_to_prairies_sensibles.py
============================
Récupère les « Prairies sensibles BCAE » via WFS :
    PRAIRIES.SENSIBLES.BCAE:prairies_sensibles
et insère les entités intersectant l'AOI dans :
    ecocompensation_results.prairies_sensibles

(La table ``natura2000`` est réservée au module ``aoi_to_natura_2000`` — SIC & ZPS Patrinat.)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import quote_plus

import geopandas as gpd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

try:
    from .carroyage_utils import harvest_adaptive, dedup_on_id_or_geom
except Exception:
    from carroyage_utils import harvest_adaptive, dedup_on_id_or_geom


WFS_URL = "https://data.geopf.fr/wfs/ows"
LAYER = "PRAIRIES.SENSIBLES.BCAE:prairies_sensibles"
SRS_WFS = "EPSG:3857"
SRS_TARGET = "EPSG:2154"
CAP = 5000


def run(engine, project_id: str, aoi_id: str, cb=None) -> int:
    """
    Récupère les prairies sensibles BCAE intersectant l'AOI
    et les insère dans ecocompensation_results.prairies_sensibles.
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

    log(f"📡 Récupération WFS {LAYER} (carroyage adaptatif, cap={CAP}) ...")
    gdf, _ = harvest_adaptive(WFS_URL, LAYER, bbox_3857, srs=SRS_WFS, cap=CAP)
    if gdf.empty:
        log("⚠️ Aucune entité récupérée dans la BBOX AOI.")
        return 0

    gdf = dedup_on_id_or_geom(gdf)
    gdf_2154 = gdf.to_crs(SRS_TARGET).rename_geometry("geom_2154")
    before = len(gdf_2154)
    gdf_2154 = gdf_2154[gdf_2154.geom_2154.intersects(aoi_union)].copy()
    after = len(gdf_2154)
    log(f"🎯 Entités intersectant l'AOI : {after}/{before}")
    if gdf_2154.empty:
        return 0

    # Colonnes attendues d'après le schéma WFS.
    if "id" not in gdf_2154.columns:
        gdf_2154["id"] = None
    if "num_prs" not in gdf_2154.columns:
        gdf_2154["num_prs"] = None
    if "surf_graph" not in gdf_2154.columns:
        gdf_2154["surf_graph"] = None
    if "date_maj" not in gdf_2154.columns:
        gdf_2154["date_maj"] = None

    gdf_2154["project_id"] = project_id
    gdf_2154["aoi_id"] = aoi_id

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE SCHEMA IF NOT EXISTS ecocompensation_results;
                CREATE TABLE IF NOT EXISTS ecocompensation_results.natura2000 (
                    rid uuid NOT NULL DEFAULT gen_random_uuid(),
                    project_id uuid NULL,
                    aoi_id uuid NULL,
                    id text NULL,
                    num_prs text NULL,
                    surf_graph double precision NULL,
                    date_maj date NULL,
                    geom_2154 geometry(Geometry, 2154) NOT NULL,
                    created_at timestamptz NULL DEFAULT now(),
                    PRIMARY KEY (rid)
                );
                CREATE INDEX IF NOT EXISTS idx_prairies_sensibles_geom
                    ON ecocompensation_results.prairies_sensibles USING GIST (geom_2154);
                CREATE INDEX IF NOT EXISTS idx_prairies_sensibles_project
                    ON ecocompensation_results.prairies_sensibles (project_id);
                """
            )
        )
        conn.execute(
            text("DELETE FROM ecocompensation_results.natura2000 WHERE project_id = :pid"),
            {"pid": project_id},
        )

    out = gdf_2154[["project_id", "aoi_id", "id", "num_prs", "surf_graph", "date_maj", "geom_2154"]].copy()
    out.to_postgis(
        name="prairies_sensibles",
        con=engine,
        schema="ecocompensation_results",
        if_exists="append",
        index=False,
        chunksize=3000,
    )
    log(f"✅ {len(out)} entités insérées dans ecocompensation_results.prairies_sensibles.")
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
        raise RuntimeError("Variables DB manquantes (.env) pour NATURA2000")

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
    print(f"Total prairies_sensibles insérées : {n}")


if __name__ == "__main__":
    main()

