#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Export GeoJSON pour la page Bancarisation (MapLibre ready).
Connexion Supabase via .env
Projection 2154 → 4326
"""

import os
from pathlib import Path
from urllib.parse import quote_plus

import geopandas as gpd
from sqlalchemy import create_engine
from dotenv import load_dotenv

# ==========================================================
# CONFIG
# ==========================================================

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

SUPABASE_HOST = os.getenv("SUPABASE_HOST")
SUPABASE_PORT = os.getenv("SUPABASE_PORT", "6543")
SUPABASE_DB = os.getenv("SUPABASE_DB", "postgres")
SUPABASE_USER = os.getenv("SUPABASE_USER")
SUPABASE_PASSWORD = os.getenv("SUPABASE_PASSWORD")

if not all([SUPABASE_HOST, SUPABASE_DB, SUPABASE_USER, SUPABASE_PASSWORD]):
    raise RuntimeError("Variables de connexion Supabase manquantes dans le .env")

password_quoted = quote_plus(SUPABASE_PASSWORD)

db_url = (
    f"postgresql+psycopg://{SUPABASE_USER}:{password_quoted}"
    f"@{SUPABASE_HOST}:{SUPABASE_PORT}/{SUPABASE_DB}"
)

engine = create_engine(db_url)

# ==========================================================
# PARAMETRES EXPORT
# ==========================================================

# Chemin de sortie (frontend)
OUTPUT_PATH = Path(
    "/Volumes/T7/Travaux_Freelance/KERELIA/CUAs/"
    "COMPENSATION_PARCELLE/COMPENSATION_ECO/"
    "frontend/public/mock/mesures.geojson"
)

# Simplification en mètres (Lambert93)
SIMPLIFY_TOLERANCE = 0.5  # 0.5 m = très léger

# ==========================================================
# EXTRACTION
# ==========================================================

print("Connexion à Supabase...")

query = """
SELECT
    id,
    categorie,
    classe,
    type,
    sous_categorie,
    type_procedure,
    theme,
    projet,
    maitre_ouvrage,
    origine_si,
    dossier_no,
    duree,
    date_decision,
    identifiant,
    l_dep,
    liste_communes,
    geom_2154
FROM geo.mesures_compensatoire_surf
"""

gdf = gpd.read_postgis(
    query,
    engine,
    geom_col="geom_2154"
)

print(f"{len(gdf)} mesures récupérées.")

if gdf.empty:
    raise RuntimeError("La table est vide.")

# ==========================================================
# TRAITEMENT GEO
# ==========================================================

print("Simplification géométrique (optionnelle)...")
gdf["geom_2154"] = gdf["geom_2154"].simplify(
    SIMPLIFY_TOLERANCE,
    preserve_topology=True
)

print("Reprojection vers EPSG:4326...")
gdf = gdf.set_geometry("geom_2154")
gdf = gdf.set_crs(2154, allow_override=True)
gdf = gdf.to_crs(4326)

# ==========================================================
# NORMALISATION PROPRIETES FRONT
# ==========================================================

print("Préparation des propriétés pour le front...")

gdf["catalog"] = "geomce"
gdf["code_insee"] = gdf["l_dep"]
gdf["section"] = "AA"
gdf["numero"] = gdf["identifiant"]
gdf["ref_cadastrale"] = (
    gdf["code_insee"].astype(str)
    + "_"
    + gdf["section"].astype(str)
    + "_"
    + gdf["numero"].astype(str)
)

gdf["commune"] = gdf["liste_communes"]
gdf["duree_mois"] = 360  # temporaire pour test
gdf["statut"] = "active"

# ==========================================================
# EXPORT
# ==========================================================

print(f"Ecriture du GeoJSON vers {OUTPUT_PATH} ...")

OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

gdf.to_file(
    OUTPUT_PATH,
    driver="GeoJSON"
)

print("Export terminé.")
print("➡ Ouvre maintenant : http://localhost:5173/bancarisation")