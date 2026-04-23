"""
core/unites_foncieres.py
Fusionne plusieurs parcelles contiguës en une Unité Foncière (UF).
"""
from __future__ import annotations

import logging
from typing import List, Optional

import geopandas as gpd
from shapely.geometry import mapping
from shapely.ops import unary_union

from .parcelle import ParcelleRef, ParcelleResult

logger = logging.getLogger(__name__)


def build_uf(results: List[ParcelleResult]) -> gpd.GeoDataFrame:
    """
    Construit le GeoDataFrame de l'UF (union des parcelles ok).
    Retourne un GeoDataFrame EPSG:4326 avec une seule ligne (l'union).
    """
    ok = [r for r in results if r.ok and not r.gdf.empty]
    if not ok:
        raise ValueError("Aucune parcelle valide pour construire l'UF")

    gdfs = [r.gdf.to_crs(4326) for r in ok]

    if len(gdfs) == 1:
        uf_geom = unary_union(gdfs[0].geometry)
    else:
        all_geoms = [g for gdf in gdfs for g in gdf.geometry]
        uf_geom = unary_union(all_geoms)

    uf_gdf = gpd.GeoDataFrame([{"geometry": uf_geom}], crs="EPSG:4326")
    area_m2 = uf_gdf.to_crs(3857).geometry.area.sum()
    logger.info(
        "🗺️  UF construite : %d parcelle(s), surface ≈ %.0f m²",
        len(ok), area_m2,
    )
    return uf_gdf


def uf_geojson(uf_gdf: gpd.GeoDataFrame) -> dict:
    """GeoJSON geometry (type + coordinates) de l'UF."""
    return mapping(unary_union(uf_gdf.geometry))


def uf_surface_m2(uf_gdf: gpd.GeoDataFrame) -> float:
    return float(uf_gdf.to_crs(3857).geometry.area.sum())


def parcelles_detail(results: List[ParcelleResult]) -> List[dict]:
    """Liste de dicts par parcelle pour la page de garde du PDF."""
    ok = [r for r in results if r.ok]
    total_m2 = sum(
        float(r.gdf.to_crs(3857).geometry.area.sum()) for r in ok
    )
    out = []
    for r in ok:
        m2 = float(r.gdf.to_crs(3857).geometry.area.sum())
        out.append({
            "ref": r.ref.label,
            "section": r.ref.section,
            "numero": r.ref.numero,
            "commune": r.ref.commune,
            "insee": r.ref.insee,
            "idu": r.idu or "",
            "contenance_m2": round(r.contenance or m2, 2),
            "pct_uf": round(100 * m2 / total_m2, 2) if total_m2 > 0 else 0.0,
        })
    return out
