# carroyage_utils.py
import logging, time
import geopandas as gpd
import pandas as pd
from typing import Tuple, List, Dict, Any

from owslib.wfs import WebFeatureService

# -----------------------------
# Découpe BBOX en 4
# -----------------------------
def subdivide_bbox(b):
    minx, miny, maxx, maxy = b
    mx, my = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    return [
        (minx, miny, mx, my),
        (mx, miny, maxx, my),
        (minx, my, mx, maxy),
        (mx, my, maxx, maxy),
    ]


# -----------------------------
# Fetch simple via OWSLib
# -----------------------------
def fetch_wfs_bbox(wfs_url, layer, bbox, srs="EPSG:4326"):
    """Récupère une tuile WFS (bbox)"""
    minx, miny, maxx, maxy = bbox
    try:
        wfs = WebFeatureService(wfs_url, version="2.0.0")
        resp = wfs.getfeature(
            typename=layer,
            bbox=(minx, miny, maxx, maxy, srs),
            outputFormat="application/json",
        )
        gdf = gpd.read_file(resp)
        return gdf, "json"
    except Exception as e:
        logging.error(f"❌ fetch_wfs_bbox({layer}, {bbox}) → {e}")
        return gpd.GeoDataFrame(), None


# -----------------------------
# Carroyage adaptatif
# -----------------------------
def harvest_adaptive(wfs_url, layer, bbox, srs="EPSG:4326", cap=5000, max_level=8, sleep_s=0.1):
    stack: List[Tuple[Tuple[float, float, float, float], int]] = [(bbox, 0)]
    parts: List[gpd.GeoDataFrame] = []
    tiles_stats: List[Dict[str, Any]] = []
    total = 0
    tile_idx = 0

    logging.info(f"📡 Carroyage adaptatif sur {layer}")
    while stack:
        b, level = stack.pop()
        tile_idx += 1
        logging.info(f"  📦 Tuile #{tile_idx} (niveau {level})")

        gdf_tile, fmt_used = fetch_wfs_bbox(wfs_url, layer, b, srs=srs)
        if isinstance(gdf_tile, tuple):
            gdf_tile = gdf_tile[0]

        if not gdf_tile.empty:
            # 🔧 Uniformiser CRS de la tuile
            if gdf_tile.crs is None:
                gdf_tile = gdf_tile.set_crs(srs, allow_override=True)
            elif str(gdf_tile.crs) != srs:
                logging.info(f"    🔄 Reprojection {gdf_tile.crs} → {srs}")
                gdf_tile = gdf_tile.to_crs(srs)

        n = len(gdf_tile)
        tiles_stats.append({"bbox": b, "level": level, "n": n, "format": fmt_used})

        if n >= cap and level < max_level:
            logging.info(f"    → Saturation (≥{cap}) → subdivision niveau {level+1}")
            for child in subdivide_bbox(b):
                stack.append((child, level + 1))
        elif n > 0:
            parts.append(gdf_tile)
            total += n
            logging.info(f"    → +{n} entités (total cumulé: {total})")

        time.sleep(sleep_s)

    if not parts:
        logging.warning("⚠️ Aucune donnée collectée")
        return gpd.GeoDataFrame(geometry=[], crs=srs), tiles_stats

    # ✅ Toutes les tuiles sont désormais en CRS uniforme
    full = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=srs)
    logging.info(f" Carroyage terminé → {len(full)} entités au total")
    return full, tiles_stats

# -----------------------------
# Déduplication (id ou géométrie)
# -----------------------------
def dedup_on_id_or_geom(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Supprime les doublons dans un GeoDataFrame :
      - si une colonne identifiant est détectée (gml_id, id, fid, etc.), on déduplique dessus
      - sinon on déduplique sur la géométrie (WKB)
    """
    if gdf is None or gdf.empty:
        return gdf

    # Colonnes candidates
    candidates = [c for c in gdf.columns if c.lower() in ("gml_id", "gmlid", "id", "fid", "identifiant", "uuid")]
    if candidates:
        id_col = candidates[0]
        logging.info(f"🔎 Déduplication par identifiant: {id_col}")
        return gdf.drop_duplicates(subset=id_col, keep="first")

    # Sinon déduplication géométrique
    logging.info("🔎 Déduplication par géométrie (WKB)")
    gdf["_wkb"] = gdf.geometry.apply(lambda g: g.wkb_hex if g is not None else None)
    gdf = gdf.drop_duplicates(subset="_wkb", keep="first").drop(columns="_wkb")
    return gdf
