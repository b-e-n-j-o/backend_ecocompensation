"""geom.py — union d'un terrain en mémoire (shapely) : surface, Miller, distance au centre AOI."""
from __future__ import annotations

import math

from shapely import wkt as shapely_wkt
from shapely.geometry import Point
from shapely.ops import unary_union


def _valid(g):
    if g is None or g.is_empty:
        return None
    if g.is_valid:
        return g
    fixed = g.buffer(0)
    return None if fixed is None or fixed.is_empty else fixed


def union_metrics(wkts: list[str], cx: float, cy: float) -> dict | None:
    geoms = []
    for w in wkts:
        if not w:
            continue
        try:
            g = _valid(shapely_wkt.loads(w))
        except Exception:
            continue
        if g is not None:
            geoms.append(g)
    if not geoms:
        return None
    union = _valid(unary_union(geoms))
    if union is None:
        return None
    area, peri = float(union.area), float(union.length)
    return {
        "wkt": union.wkt,
        "surface_ha": area / 10_000.0,
        "miller": (4.0 * math.pi * area) / (peri ** 2) if peri else 0.0,
        "dist_centre_m": float(union.centroid.distance(Point(cx, cy))),
    }
