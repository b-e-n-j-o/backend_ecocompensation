"""
build_vegetation_hybride.py
----------------------------
Fusion BD TOPO zone_de_vegetation (prioritaire) + CESBIO (fond)
par tuilage spatial pour la Gironde (dept=33).

Résultat -> ecocompensation.vegetation_sur_cesbio
"""

import os
import time
import logging
from pathlib import Path
from urllib.parse import quote_plus

import geopandas as gpd
import pandas as pd
from shapely.ops import unary_union
from shapely.validation import make_valid
from sqlalchemy import create_engine, text as _text
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GRID_SIZE    = 10       # grille 10x10 = 100 tuiles
MIN_AREA_M2  = 1        # filtre les residus < 1 m2
DEPT_CESBIO  = "33"     # filtre CESBIO sur la Gironde
TARGET_SCHEMA = "ecocompensation"
TARGET_TABLE  = "vegetation_sur_cesbio"
FULL_TABLE    = f"{TARGET_SCHEMA}.{TARGET_TABLE}"
CHUNK_WRITE   = 2000    # nb de lignes par INSERT batch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Connexion
# ---------------------------------------------------------------------------
def get_engine():
    BASE_DIR = Path(__file__).resolve().parent
    load_dotenv(BASE_DIR / ".env")

    host     = os.getenv("SUPABASE_HOST")
    port     = os.getenv("SUPABASE_PORT", "6543")
    db       = os.getenv("SUPABASE_DB", "postgres")
    user     = os.getenv("SUPABASE_USER")
    password = os.getenv("SUPABASE_PASSWORD")

    if not all([host, db, user, password]):
        raise RuntimeError("Variables de connexion manquantes dans le .env")

    pwd = quote_plus(password)
    return create_engine(
        f"postgresql+psycopg://{user}:{pwd}@{host}:{port}/{db}",
        pool_pre_ping=True,
    )


# ---------------------------------------------------------------------------
# Creation table cible
# ---------------------------------------------------------------------------
DDL = f"""
DROP TABLE IF EXISTS {FULL_TABLE};

CREATE TABLE {FULL_TABLE} (
    id      bigserial PRIMARY KEY,
    source  text        NOT NULL,   -- 'bdtopo' | 'cesbio'
    nature  text,                   -- depuis zone_de_vegetation
    libelle text,                   -- depuis cesbio.libelle_classe
    geom    geometry(MultiPolygon, 2154)
);

CREATE INDEX ON {FULL_TABLE} USING gist(geom);
"""

def create_target_table(engine):
    log.info("Creation de la table cible %s ...", FULL_TABLE)
    with engine.begin() as conn:
        conn.execute(_text(DDL))
    log.info("Table creee.")


# ---------------------------------------------------------------------------
# Chargement des donnees source (une seule fois en RAM)
# ---------------------------------------------------------------------------
def load_sources(engine):
    log.info("Chargement zone_de_vegetation (BD TOPO) ...")
    bdtopo = gpd.read_postgis(
        "SELECT id, nature, geom_2154 AS geom FROM geo.zone_de_vegetation WHERE geom_2154 IS NOT NULL",
        engine,
        geom_col="geom",
        crs="EPSG:2154",
    )
    bdtopo["geom"] = bdtopo["geom"].apply(make_valid)
    log.info("  -> %d entites BD TOPO chargees", len(bdtopo))

    log.info("Chargement CESBIO (dept=%s) ...", DEPT_CESBIO)
    cesbio = gpd.read_postgis(
        f"SELECT id, libelle_classe, geom FROM ecocompensation.cesbio "
        f"WHERE departement = '{DEPT_CESBIO}' AND geom IS NOT NULL",
        engine,
        geom_col="geom",
        crs="EPSG:2154",
    )
    cesbio["geom"] = cesbio["geom"].apply(make_valid)
    log.info("  -> %d entites CESBIO chargees", len(cesbio))

    return bdtopo, cesbio


# ---------------------------------------------------------------------------
# Generation de la grille de tuiles
# ---------------------------------------------------------------------------
def make_grid(gdf_a, gdf_b, n=GRID_SIZE):
    """Retourne une liste de Polygon shapely couvrant l'union des deux bbox."""
    from shapely.geometry import box

    bounds = (
        pd.concat([gdf_a.bounds, gdf_b.bounds])
        .agg({"minx": "min", "miny": "min", "maxx": "max", "maxy": "max"})
    )
    minx, miny = bounds["minx"], bounds["miny"]
    maxx, maxy = bounds["maxx"], bounds["maxy"]

    dx = (maxx - minx) / n
    dy = (maxy - miny) / n

    tiles = [
        box(minx + i * dx, miny + j * dy, minx + (i+1) * dx, miny + (j+1) * dy)
        for i in range(n)
        for j in range(n)
    ]
    log.info("Grille %dx%d = %d tuiles generees", n, n, len(tiles))
    return tiles


# ---------------------------------------------------------------------------
# Ecriture batch en base
# ---------------------------------------------------------------------------
def write_rows(rows: list, engine):
    """Insere une liste de dicts {source, nature, libelle, geom_wkt} en base."""
    if not rows:
        return

    def sql_str(v):
        return "NULL" if v is None else "'" + v.replace("'", "''") + "'"

    values = ", ".join(
        f"({sql_str(r['source'])}, {sql_str(r['nature'])}, {sql_str(r['libelle'])}, "
        f"ST_Multi(ST_MakeValid(ST_GeomFromText({sql_str(r['geom_wkt'])}, 2154))))"
        for r in rows
    )
    sql = f"INSERT INTO {FULL_TABLE} (source, nature, libelle, geom) VALUES {values}"
    with engine.begin() as conn:
        conn.execute(_text(sql))


# ---------------------------------------------------------------------------
# Traitement par tuile
# ---------------------------------------------------------------------------
def process_tiles(bdtopo: gpd.GeoDataFrame, cesbio: gpd.GeoDataFrame, tiles: list, engine):
    total = len(tiles)
    seen_bdtopo_ids = set()  # evite les doublons sur les bords de tuiles
    seen_cesbio_ids = set()

    for idx, tile in enumerate(tiles, 1):
        t0 = time.time()

        bd_tile = bdtopo[bdtopo.intersects(tile)].copy()
        cs_tile = cesbio[cesbio.intersects(tile)].copy()

        if bd_tile.empty and cs_tile.empty:
            continue

        rows = []

        # -- BD TOPO (prioritaire) ------------------------------------------
        new_bd = bd_tile[~bd_tile["id"].isin(seen_bdtopo_ids)]
        for _, row in new_bd.iterrows():
            geom = row["geom"]
            if geom is None or geom.is_empty:
                continue
            seen_bdtopo_ids.add(row["id"])
            rows.append({
                "source":   "bdtopo",
                "nature":   row["nature"],
                "libelle":  None,
                "geom_wkt": geom.wkt,
            })

        # -- CESBIO decoupé -------------------------------------------------
        bdtopo_union = unary_union(bd_tile["geom"].values) if not bd_tile.empty else None

        new_cs = cs_tile[~cs_tile["id"].isin(seen_cesbio_ids)]
        for _, row in new_cs.iterrows():
            geom = row["geom"]
            if geom is None or geom.is_empty:
                continue

            if bdtopo_union is not None and geom.intersects(bdtopo_union):
                diff = make_valid(geom.difference(bdtopo_union))
                if diff.is_empty or diff.area < MIN_AREA_M2:
                    seen_cesbio_ids.add(row["id"])
                    continue
                geom = diff

            seen_cesbio_ids.add(row["id"])
            rows.append({
                "source":   "cesbio",
                "nature":   None,
                "libelle":  row["libelle_classe"],
                "geom_wkt": geom.wkt,
            })

        # Ecriture par batch
        for i in range(0, len(rows), CHUNK_WRITE):
            write_rows(rows[i:i + CHUNK_WRITE], engine)

        elapsed = time.time() - t0
        log.info(
            "Tuile %3d/%d — %3d bdtopo | %4d cesbio -> %d lignes  (%.1fs)",
            idx, total, len(new_bd), len(new_cs), len(rows), elapsed,
        )

    log.info(
        "Traitement termine. BD TOPO uniques : %d | CESBIO uniques : %d",
        len(seen_bdtopo_ids), len(seen_cesbio_ids),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    t_start = time.time()

    engine = get_engine()
    create_target_table(engine)

    bdtopo, cesbio = load_sources(engine)
    tiles = make_grid(bdtopo, cesbio, n=GRID_SIZE)
    process_tiles(bdtopo, cesbio, tiles, engine)

    total_time = time.time() - t_start
    log.info("=== Termine en %.1f secondes (%.1f min) ===", total_time, total_time / 60)


if __name__ == "__main__":
    main()