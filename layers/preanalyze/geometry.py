"""
preanalyze/geometry.py
======================
Calcul des métriques géométriques de la parcelle cible :
  - surface_ha : superficie en hectares
  - miller     : indice de compacité de Miller (4π·A / P²)
  - perimeter_m : périmètre en mètres
"""
from __future__ import annotations
import math
from shapely.geometry.base import BaseGeometry


def compute_geometry_metrics(parcel_geom: BaseGeometry) -> dict:
    """
    Retourne surface_ha, perimeter_m et miller pour la géométrie parcelle.
    """
    area_m2    = float(parcel_geom.area)
    perim_m    = float(parcel_geom.length)
    surface_ha = round(area_m2 / 10_000.0, 4)
    miller     = round(
        (4 * math.pi * area_m2) / (perim_m ** 2) if perim_m > 0 else 0.0,
        6,
    )
    return {
        "surface_ha":   surface_ha,
        "perimeter_m":  round(perim_m, 1),
        "miller":       miller,
    }