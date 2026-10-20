"""
geometry.py — Outils géométriques : GeoJSON <-> shapely, reprojection vers
Lambert-93 (EPSG:2154, toutes les mesures se font en mètres), réparation.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Dict, Tuple

import numpy as np
import shapely
from pyproj import Transformer
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

TARGET_EPSG = 2154

# Emprise grossière de la métropole en Lambert-93 (contrôle d'axes).
_L93_X = (50_000, 1_300_000)
_L93_Y = (6_000_000, 7_200_000)


@lru_cache(maxsize=16)
def _transformer(src_epsg: int, dst_epsg: int) -> Transformer:
    return Transformer.from_crs(src_epsg, dst_epsg, always_xy=True)


def reproject(geom: BaseGeometry, src_epsg: int, dst_epsg: int = TARGET_EPSG) -> BaseGeometry:
    if src_epsg == dst_epsg or geom is None or geom.is_empty:
        return geom
    tr = _transformer(src_epsg, dst_epsg)

    def _f(coords: np.ndarray) -> np.ndarray:
        x, y = tr.transform(coords[:, 0], coords[:, 1])
        return np.column_stack([x, y])

    return shapely.transform(geom, _f)


def swap_xy(geom: BaseGeometry) -> BaseGeometry:
    return shapely.transform(geom, lambda c: c[:, ::-1])


def looks_like_l93(geom: BaseGeometry) -> bool:
    if geom is None or geom.is_empty:
        return True
    minx, miny, maxx, maxy = geom.bounds
    return _L93_X[0] <= minx <= _L93_X[1] and _L93_Y[0] <= miny <= _L93_Y[1]


def clean(geom: BaseGeometry) -> BaseGeometry:
    """Répare une géométrie invalide (auto-intersections, etc.)."""
    if geom is None or geom.is_empty:
        return geom
    if not geom.is_valid:
        geom = shapely.make_valid(geom)
    return geom


def from_geojson(obj: Dict[str, Any], src_epsg: int = TARGET_EPSG) -> BaseGeometry:
    """GeoJSON geometry (dict) -> shapely en 2154, réparée."""
    g = shape(obj)
    g = reproject(g, src_epsg, TARGET_EPSG)
    return clean(g)


def to_geojson(geom: BaseGeometry, dst_epsg: int = 4326) -> Dict[str, Any]:
    g = reproject(geom, TARGET_EPSG, dst_epsg) if dst_epsg != TARGET_EPSG else geom
    return shapely.geometry.mapping(g)


def bbox_to_epsg(bounds: Tuple[float, float, float, float], dst_epsg: int) -> Tuple[float, float, float, float]:
    """Bbox 2154 -> bbox dans dst_epsg (enveloppe des 4 coins, ordre x/y = lon/lat)."""
    if dst_epsg == TARGET_EPSG:
        return bounds
    tr = _transformer(TARGET_EPSG, dst_epsg)
    minx, miny, maxx, maxy = bounds
    xs, ys = tr.transform([minx, minx, maxx, maxx], [miny, maxy, miny, maxy])
    return (min(xs), min(ys), max(xs), max(ys))
