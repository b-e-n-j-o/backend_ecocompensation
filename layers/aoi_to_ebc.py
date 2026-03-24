#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aoi_to_ebc.py
=============
Récupère les espaces boisés (prescriptions surfaciques) qui intersectent l'AOI,
à partir de la couche WFS wfs_du:prescription_surf (Geoportail), filtrés sur
le champ "libelle" avec des mots-clés "boise"/"boisé", puis les enregistre
dans ecocompensation_results.ebc.
"""

import logging
import os
import sys
from pathlib import Path
from datetime import datetime
from urllib.parse import quote_plus

import geopandas as gpd
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

# Import des utils carroyage du projet WFS_TO_SUPA (même logique que AOC)
from carroyage_utils import harvest_adaptive  # on peut se passer du dedup ici

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

BASE_DIR = Path(__file__).resolve().parent

# -----------------------------
# CONFIG
# -----------------------------

URL = "https://data.geopf.fr/wfs/ows"
LAYER = "wfs_du:prescription_surf"
SRS_WFS = "EPSG:3857"   # SCR de la couche (Pseudo-Mercator)
SRS_TARGET = "EPSG:2154"
CAP = 5000  # taille max de batch pour le carroyage

def run(engine, project_id: str, aoi_id: str, cb=None) -> int:
    """
    Récupère les EBC intersectant l'AOI donnée et les insère
    dans ecocompensation_results.ebc.

    :param engine: Engine SQLAlchemy déjà connecté.
    :param project_id: Identifiant du projet (écrit dans les lignes de résultat).
    :param aoi_id: Identifiant de l'AOI pour l'intersection géométrique.
    :param cb: Callback de log optionnel (cb(str)).
    :return: Nombre d'entités insérées.
    """
    log = cb or (lambda msg: None)

    # 1) AOI depuis la base (geom_2154) pour CE aoi_id
    log("Chargement AOI depuis ecocompensation.aoi ...")
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
        log(f"Aucune AOI trouvée dans ecocompensation.aoi pour id={aoi_id}, insertion annulée.")
        return 0

    if aoi.crs is None or aoi.crs.to_string() != "EPSG:2154":
        aoi = aoi.set_crs("EPSG:2154", allow_override=True)

    aoi_union = aoi.union_all()

    # BBOX AOI en 3857 pour le WFS (carroyage)
    aoi_3857 = aoi.to_crs(SRS_WFS)
    minx, miny, maxx, maxy = aoi_3857.total_bounds
    bbox_3857 = (minx, miny, maxx, maxy)
    log(f"AOI utilisée pour le lien : id={aoi_id}")
    log(f"BBOX AOI (EPSG:3857) : {bbox_3857}")

    # 3) Récupération prescriptions surfaciques par carroyage adaptatif
    log(f"Récupération prescriptions surfaciques (wfs_du:prescription_surf, carroyage adaptatif, cap={CAP})...")

    # On récupère la couche WFS sur la BBOX de l'AOI, SANS filtre côté WFS.
    gdf, _ = harvest_adaptive(URL, LAYER, bbox_3857, srs=SRS_WFS, cap=CAP)
    if gdf.empty:
        log("Aucune entité de prescription surfacique récupérée dans la BBOX.")
        return 0

    # 4) Filtre côté Python sur les libellés "espace boisé", "boisé(e)(s)", etc.
    if "libelle" not in gdf.columns:
        log("Champ 'libelle' absent de la réponse WFS, impossible de filtrer les EBC.")
        return 0

    lib = gdf["libelle"].astype(str).str.lower()
    # Approche large : tout libellé contenant "boise" ou "boisé"
    mask = lib.str.contains("boise") | lib.str.contains("boisé")
    gdf = gdf[mask].copy()

    log(f"Prescriptions après filtre 'boisé/boise' sur libelle : {len(gdf)}")
    if gdf.empty:
        log("Aucune prescription 'boisée' trouvée dans la BBOX.")
        return 0

    # 5) Reprojection 2154 et intersection exacte avec l'AOI
    gdf_2154 = gdf.to_crs(SRS_TARGET)
    gdf_2154 = gdf_2154.rename_geometry("geom_2154")
    gdf_2154 = gdf_2154[gdf_2154.geom_2154.intersects(aoi_union)]
    log(f"Entités EBC après intersection AOI : {len(gdf_2154)}")

    if gdf_2154.empty:
        log("Aucune entité EBC n'intersecte l'AOI.")
        return 0

    gdf_2154["project_id"] = project_id

    # 6) Création table results et insertion dans ecocompensation_results.ebc
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE SCHEMA IF NOT EXISTS ecocompensation_results;
                CREATE TABLE IF NOT EXISTS ecocompensation_results.ebc (
                    id uuid NOT NULL DEFAULT gen_random_uuid(),
                    project_id uuid NULL,
                    libelle text,
                    insee text,
                    nature text,
                    typepsc text,
                    stypepsc text,
                    gpu_doc_id text,
                    gpu_status text,
                    geom_2154 geometry(Geometry, 2154) NOT NULL,
                    created_at timestamptz NULL DEFAULT now(),
                    PRIMARY KEY (id)
                );
                CREATE INDEX IF NOT EXISTS idx_ecocomp_results_ebc_geom
                    ON ecocompensation_results.ebc USING GIST (geom_2154);
                CREATE INDEX IF NOT EXISTS idx_ecocomp_results_ebc_project
                    ON ecocompensation_results.ebc (project_id);
                """
            )
        )
        conn.execute(text("DELETE FROM ecocompensation_results.ebc WHERE project_id = :pid"), {"pid": project_id})

    # Colonnes qu'on envoie en base
    cols = []
    for c in ["libelle", "insee", "nature", "typepsc", "stypepsc", "gpu_doc_id", "gpu_status"]:
        if c in gdf_2154.columns:
            cols.append(c)

    out = gdf_2154[["project_id"] + cols + ["geom_2154"]].copy()

    out.to_postgis(
        name="ebc",
        con=engine,
        schema="ecocompensation_results",
        if_exists="append",
        index=False,
        chunksize=2000,
    )
    log(f"{len(out)} entités EBC insérées dans ecocompensation_results.ebc pour project_id={project_id}.")
    return len(out)


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
        raise RuntimeError("Variables de connexion à la base manquantes dans le .env (EBC).")

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
    print(f"Total EBC insérées : {n}")


if __name__ == "__main__":
    main()
