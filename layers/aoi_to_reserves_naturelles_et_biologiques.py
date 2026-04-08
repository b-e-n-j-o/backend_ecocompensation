#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aoi_to_reserves_naturelles_et_biologiques.py
============================================

Plusieurs flux Patrinat (MNHN / Géoplateforme), même famille d’attributs,
agrégés dans une seule table légère : ``nom_site``, ``geom_2154`` et
``categorie_reserve`` (type de réserve).

WFS : https://data.geopf.fr/wfs/ows (EPSG:3857)

→ ecocompensation_results.reserves_naturelles
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
TABLE_FULL = "ecocompensation_results.reserves_naturelles"

# Couches WFS + valeur stockée dans ``categorie_reserve``.
COUCHES: list[dict] = [
    {
        "name": "RB — réserves biologiques",
        "layer": "patrinat_rb:reserve_biologique",
        "categorie_reserve": "reserve_biologique",
    },
    {
        "name": "RIPN — réserve intégrale",
        "layer": "patrinat_ripn:ripn",
        "categorie_reserve": "reserve_integrale",
    },
    {
        "name": "RNC — Corse (PNM)",
        "layer": "patrinat_rnc:pnm",
        "categorie_reserve": "reserve_naturelle_corse",
    },
    {
        "name": "RNCFS — chasse et faune sauvage",
        "layer": "patrinat_rncfs:rncfs",
        "categorie_reserve": "reserve_chasse_faune_sauvage",
    },
    {
        "name": "RNN — réserves naturelles nationales",
        "layer": "patrinat_rnn:rnn",
        "categorie_reserve": "reserves_naturelles_nationales",
    },
    {
        "name": "RNR — réserves naturelles régionales",
        "layer": "patrinat_rnr:rnr",
        "categorie_reserve": "reserve_naturelle_regionale",
    },
]


def _nom_site_col(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    lower = {c.lower(): c for c in gdf.columns}
    if "nom_site" in lower:
        src = lower["nom_site"]
        if src != "nom_site":
            gdf = gdf.rename(columns={src: "nom_site"})
    elif "nom_site" not in gdf.columns:
        gdf["nom_site"] = None
    return gdf


def _fetch_one_layer(
    layer: str,
    categorie_reserve: str,
    bbox_3857: tuple[float, float, float, float],
    log,
) -> gpd.GeoDataFrame:
    log(f"📡 WFS {layer} [{categorie_reserve}] (cap={CAP}) ...")
    gdf, _ = harvest_adaptive(WFS_URL, layer, bbox_3857, srs=SRS_WFS, cap=CAP)
    if gdf.empty:
        log(f"⚠️ Aucune entité dans la BBOX pour {layer}")
        return gpd.GeoDataFrame()
    gdf = dedup_on_id_or_geom(gdf)
    gdf = _nom_site_col(gdf)
    gdf["categorie_reserve"] = categorie_reserve
    return gdf


def _ensure_table(conn) -> None:
    conn.execute(
        text(
            """
            CREATE SCHEMA IF NOT EXISTS ecocompensation_results;

            CREATE TABLE IF NOT EXISTS ecocompensation_results.reserves_naturelles (
                id uuid NOT NULL DEFAULT gen_random_uuid(),
                project_id uuid NULL,
                aoi_id uuid NULL,
                categorie_reserve text NOT NULL,
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
            ALTER TABLE ecocompensation_results.reserves_naturelles
                ADD COLUMN IF NOT EXISTS categorie_reserve text;
            ALTER TABLE ecocompensation_results.reserves_naturelles
                ADD COLUMN IF NOT EXISTS nom_site text;
            ALTER TABLE ecocompensation_results.reserves_naturelles
                ADD COLUMN IF NOT EXISTS aoi_id uuid;
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS idx_reserves_naturelles_geom
                ON ecocompensation_results.reserves_naturelles USING GIST (geom_2154);
            CREATE INDEX IF NOT EXISTS idx_reserves_naturelles_project
                ON ecocompensation_results.reserves_naturelles (project_id);
            CREATE INDEX IF NOT EXISTS idx_reserves_naturelles_categorie
                ON ecocompensation_results.reserves_naturelles (categorie_reserve);
            """
        )
    )


def run(engine, project_id: str, aoi_id: str, cb=None) -> int:
    """
    Récupère les entités des six couches Patrinat intersectant l’AOI et les insère dans
    ecocompensation_results.reserves_naturelles (nom_site, geom_2154, categorie_reserve).
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
        gdf = _fetch_one_layer(
            cfg["layer"], cfg["categorie_reserve"], bbox_3857, log
        )
        if not gdf.empty:
            parts.append(gdf)

    if not parts:
        log("⚠️ Aucune réserve dans la BBOX AOI.")
        return 0

    gdf_all = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=SRS_WFS)
    gdf_2154 = gdf_all.to_crs(SRS_TARGET).rename_geometry("geom_2154")

    keep = ["nom_site", "categorie_reserve", "geom_2154"]
    gdf_2154 = gdf_2154[[c for c in keep if c in gdf_2154.columns]].copy()

    before = len(gdf_2154)
    gdf_2154 = gdf_2154[gdf_2154.geom_2154.intersects(aoi_union)].copy()
    after = len(gdf_2154)
    log(f"🎯 Réserves intersectant l’AOI : {after}/{before}")
    if gdf_2154.empty:
        log("⚠️ Aucune entité n’intersecte l’AOI.")
        return 0

    gdf_2154["project_id"] = project_id
    gdf_2154["aoi_id"] = aoi_id

    insert_cols = ["project_id", "aoi_id", "categorie_reserve", "nom_site", "geom_2154"]

    with engine.begin() as conn:
        _ensure_table(conn)
        conn.execute(
            text(f"DELETE FROM {TABLE_FULL} WHERE project_id = :pid"),
            {"pid": project_id},
        )

    out = gdf_2154[insert_cols].copy()
    out.to_postgis(
        name="reserves_naturelles",
        con=engine,
        schema="ecocompensation_results",
        if_exists="append",
        index=False,
        chunksize=3000,
    )
    n = len(out)
    log(f"✅ {n} entités insérées dans {TABLE_FULL}.")
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
        raise RuntimeError("Variables DB manquantes (.env) pour réserves naturelles")

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
    print(f"Total réserves insérées : {n}")


if __name__ == "__main__":
    main()
