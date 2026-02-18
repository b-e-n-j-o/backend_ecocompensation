#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_classement_shp.py
========================

Export des parcelles classées (sortie du vrai filtre + scoring) en une couche
Shapefile pour visualisation dans QGIS. Chaque parcelle a en attributs les
valeurs utilisées pour le filtre et le départage (surface, Miller, ZDV,
hydro, distance, score, rang, etc.).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import geopandas as gpd

if TYPE_CHECKING:
    from sqlalchemy import Engine


def export_classement_shp(
    engine: "Engine",
    aoi_id: str,
    parcelles: list[dict],
    options,  # FiltreOptions
    final_radius_km: float,
    output_path: Path,
) -> None:
    """
    Exporte les parcelles classées en un Shapefile (une couche).
    parcelles : liste de dicts avec rank, idu, code_insee, section, numero,
                surface_ha, miller, distance_km, dist_hydro_m, score, score_details.
    """
    if not parcelles:
        return

    idus = [p.get("idu") for p in parcelles if p.get("idu")]

    if not idus:
        return

    # Récupérer les géométries (read_postgis gère le type geometry correctement)
    sql_geom = """
        SELECT idu, geom_2154
        FROM ecocompensation_results.parcelles
        WHERE aoi_id = %(aoi_id)s AND idu = ANY(%(idus)s)
    """
    gdf_geom = gpd.read_postgis(
        sql_geom, engine, geom_col="geom_2154",
        params={"aoi_id": aoi_id, "idus": idus},
    )
    idu_to_geom = gdf_geom.set_index("idu", drop=False)["geom_2154"].to_dict()
    # Clés peuvent être str ou autre selon le driver ; on accepte les deux
    def get_geom(idu_val):
        return idu_to_geom.get(idu_val) or idu_to_geom.get(str(idu_val))

    # Libellés des paramètres du filtre (pour les colonnes attributaires)
    zdv_str = ", ".join(options.zdv_natures) if options.zdv_natures else "—"
    troncon_str = (
        "Intersection"
        if options.troncon_hydro_mode == "intersect"
        else f"<{options.troncon_hydro_radius_m:.0f}m"
        if options.troncon_hydro_mode == "within_radius"
        else "—"
    )
    surf_hyd_str = (
        "Intersection"
        if options.surface_hydro_mode == "intersect"
        else f"<{options.surface_hydro_radius_m:.0f}m"
        if options.surface_hydro_mode == "within_radius"
        else "—"
    )

    # Construire une ligne par parcelle (ordre du classement)
    geoms = []
    rows_attr = []

    for p in parcelles:
        idu = p.get("idu")
        if not idu:
            continue
        geom = get_geom(idu)
        if geom is None:
            continue

        geoms.append(geom)
        dist_km = p.get("distance_km", 0)
        dist_hyd = p.get("dist_hydro_m")

        rows_attr.append({
            "rang": p.get("rank", 0),
            "score": p.get("score", 0),
            "idu": idu,
            "code_insee": p.get("code_insee") or "",
            "section": p.get("section") or "",
            "numero": p.get("numero") or "",
            "surf_ha": round(float(p.get("surface_ha") or 0), 2),
            "miller": round(float(p.get("miller") or 0), 4),
            "dist_km": round(dist_km, 2),
            "dist_hyd": round(float(dist_hyd), 0) if dist_hyd is not None else -1,
            "zdv": zdv_str or "—",
            "troncon": troncon_str,
            "surf_hyd": surf_hyd_str,
            "rayon_km": round(final_radius_km, 1),
        })

    if not geoms:
        return

    gdf = gpd.GeoDataFrame(
        rows_attr,
        geometry=geoms,
        crs="EPSG:2154",
    )

    # Noms de champs déjà ≤ 10 car pour ESRI Shapefile
    output_path = Path(output_path)
    if output_path.suffix.lower() != ".shp":
        output_path = output_path.with_suffix(".shp")

    gdf.to_file(output_path, driver="ESRI Shapefile", encoding="utf-8")
