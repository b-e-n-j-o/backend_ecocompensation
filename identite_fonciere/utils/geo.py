"""
utils/geo.py
Helpers géométriques partagés : SRID, bbox, GeoDataFrame depuis WKT/GeoJSON.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import geopandas as gpd
from pyproj import Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform, unary_union


# ---------------------------------------------------------------------------
# SRID detection (repris de identite_parcelle.py)
# ---------------------------------------------------------------------------

def _first_xy(coords: Any) -> Optional[Tuple[float, float]]:
    if isinstance(coords, list):
        if len(coords) >= 2 and all(isinstance(c, (int, float)) for c in coords[:2]):
            return float(coords[0]), float(coords[1])
        for item in coords:
            got = _first_xy(item)
            if got:
                return got
    return None


def detect_srid(geometry: Dict[str, Any], explicit: Optional[int] = None) -> int:
    if explicit in (4326, 2154, 3857):
        return explicit
    pair = _first_xy(geometry.get("coordinates"))
    if not pair:
        return 4326
    x, y = pair
    if -180 <= x <= 180 and -90 <= y <= 90:
        return 4326
    if abs(x) <= 20_037_508 and abs(y) <= 20_037_508:
        return 3857
    if 0 <= x <= 1_300_000 and 5_800_000 <= y <= 7_300_000:
        return 2154
    return 4326


# ---------------------------------------------------------------------------
# GeoDataFrame helpers
# ---------------------------------------------------------------------------

def geojson_to_gdf(geometry: Dict[str, Any], srid: Optional[int] = None) -> gpd.GeoDataFrame:
    """GeoJSON geometry dict → GeoDataFrame EPSG:4326."""
    detected = detect_srid(geometry, srid)
    g = shape(geometry)
    gdf = gpd.GeoDataFrame([{"geometry": g}], crs=f"EPSG:{detected}")
    if detected != 4326:
        gdf = gdf.to_crs(4326)
    return gdf


def wkt_to_gdf(wkt: str, srid: int = 4326) -> gpd.GeoDataFrame:
    from shapely import wkt as shapely_wkt
    g = shapely_wkt.loads(wkt)
    gdf = gpd.GeoDataFrame([{"geometry": g}], crs=f"EPSG:{srid}")
    if srid != 4326:
        gdf = gdf.to_crs(4326)
    return gdf


# ---------------------------------------------------------------------------
# Bbox helpers
# ---------------------------------------------------------------------------

def gdf_bbox_4326(gdf: gpd.GeoDataFrame, buffer_m: float = 0.0) -> Tuple[float, float, float, float]:
    """
    Retourne (minx, miny, maxx, maxy) en EPSG:4326.
    buffer_m : buffer en mètres appliqué avant conversion (via EPSG:3857).
    """
    g4326 = gdf.to_crs(4326) if gdf.crs and gdf.crs.to_epsg() != 4326 else gdf
    if buffer_m > 0:
        g3857 = gdf.to_crs(3857)
        union = unary_union(g3857.geometry).buffer(buffer_m)
        g4326 = gpd.GeoDataFrame([{"geometry": union}], crs="EPSG:3857").to_crs(4326)
    union4326 = unary_union(g4326.geometry)
    return union4326.bounds  # (minx, miny, maxx, maxy)


def bbox_str(minx: float, miny: float, maxx: float, maxy: float) -> str:
    """Chaîne bbox pour paramètre WFS OGC (lon/lat)."""
    return f"{minx},{miny},{maxx},{maxy},EPSG:4326"


# ---------------------------------------------------------------------------
# Intersection Shapely (simule ST_Intersects sans PostGIS)
# ---------------------------------------------------------------------------

def intersects_gdf(
    ref_gdf: gpd.GeoDataFrame,
    candidates_gdf: gpd.GeoDataFrame,
) -> gpd.GeoDataFrame:
    """
    Filtre candidates_gdf aux entités intersectant l'union de ref_gdf.
    Les deux GeoDataFrames doivent être en EPSG:4326.
    """
    if candidates_gdf.empty:
        return candidates_gdf
    ref_union = unary_union(ref_gdf.to_crs(4326).geometry)
    mask = candidates_gdf.geometry.intersects(ref_union)
    return candidates_gdf[mask].copy()


def area_intersection_pct(
    ref_gdf: gpd.GeoDataFrame,
    candidates_gdf: gpd.GeoDataFrame,
    group_col: str,
) -> Dict[str, float]:
    """
    % de surface de ref couvert par chaque valeur de group_col dans candidates.
    Calcul en EPSG:3857 (mètres).
    """
    ref_3857 = ref_gdf.to_crs(3857)
    cand_3857 = candidates_gdf.to_crs(3857)
    ref_union = unary_union(ref_3857.geometry)
    total = ref_union.area
    if total <= 0:
        return {}
    stats: Dict[str, float] = {}
    for val, group in cand_3857.groupby(group_col):
        inter_area = sum(
            ref_union.intersection(geom).area
            for geom in group.geometry
            if geom and not geom.is_empty
        )
        if inter_area > 0:
            stats[str(val)] = round(100 * inter_area / total, 2)
    return stats
