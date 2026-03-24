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
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Engine


def export_classement_shp(
    engine: "Engine",
    project_id: str,
    parcelles: list[dict],
    options,  # FiltreOptions
    final_radius_km: float,
    output_path: Path,
    aoi_id: str | None = None,
) -> None:
    """
    Exporte les parcelles classées en un Shapefile (une couche).
    parcelles : liste de dicts avec rank, idu, code_insee, section, numero,
                surface_ha, miller, distance_km, dist_hydro_m, score, score_details.
    """
    if not parcelles:
        raise ValueError("Liste de parcelles vide.")

    idus = [str(p.get("idu")) for p in parcelles if p.get("idu")]

    if not idus:
        raise ValueError("Aucun identifiant parcelle (idu) dans le classement.")

    # Même logique que /geojson parcelles : lier par project_id OU aoi_id (psycopg3 + text()).
    aoi_id_str = str(aoi_id or "")
    sql_geom = text("""
        SELECT p.idu, p.geom_2154
        FROM ecocompensation_results.parcelles p
        WHERE (p.project_id = :pid OR p.aoi_id = :aoi_id_str)
          AND p.idu = ANY(:idus)
    """)
    with engine.connect() as conn:
        gdf_geom = gpd.read_postgis(
            sql_geom,
            conn,
            geom_col="geom_2154",
            params={"pid": project_id, "aoi_id_str": aoi_id_str, "idus": idus},
        )
    idu_to_geom = {str(row["idu"]): row["geom_2154"] for _, row in gdf_geom.iterrows()}

    def get_geom(idu_val):
        return idu_to_geom.get(str(idu_val))

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
        raise ValueError(
            "Aucune géométrie parcelle trouvée en base pour ce classement. "
            "Vérifiez que les parcelles sont bien chargées (couche parcelles / même projet ou AOI)."
        )

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
