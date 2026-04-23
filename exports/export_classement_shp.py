#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_classement_shp.py
========================

Export des parcelles classées (sortie du vrai filtre + scoring) en une couche
Shapefile pour visualisation dans QGIS. Attributs alignés sur le classement
actuel (scores pool) ; les textes longs respectent la limite DBF (254 c.).

Un **GeoPackage** homonyme (``.gpkg``) est écrit à côté du SHP : mêmes géométries
et champs, avec ``txt_dure`` **non tronqué** (texte long) — à privilégier dans QGIS
pour la justification dureté complète.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import geopandas as gpd
from sqlalchemy import text

from exports.classement_export_attrs import build_parcelle_export_row, mmap_for_parcelle

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
    metrics_by_idu: dict[str, list[dict[str, Any]]] | None = None,
) -> None:
    """
    Exporte les parcelles classées en un Shapefile (une couche).
    metrics_by_idu : { idu: lignes métriques pool } — optionnel (sinon champs scores NaN / vides).
    """
    if not parcelles:
        raise ValueError("Liste de parcelles vide.")

    idus = [str(p.get("idu")) for p in parcelles if p.get("idu")]

    if not idus:
        raise ValueError("Aucun identifiant parcelle (idu) dans le classement.")

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

    geoms = []
    rows_shp: list[dict[str, Any]] = []
    rows_gpkg: list[dict[str, Any]] = []

    for p in parcelles:
        idu = p.get("idu")
        if not idu:
            continue
        geom = get_geom(idu)
        if geom is None:
            continue

        geoms.append(geom)
        mmap = mmap_for_parcelle(p, metrics_by_idu)
        rows_shp.append(build_parcelle_export_row(p, mmap, options, clip_for_shapefile=True))
        rows_gpkg.append(build_parcelle_export_row(p, mmap, options, clip_for_shapefile=False))

    if not geoms:
        raise ValueError(
            "Aucune géométrie parcelle trouvée en base pour ce classement. "
            "Vérifiez que les parcelles sont bien chargées (couche parcelles / même projet ou AOI)."
        )

    gdf_shp = gpd.GeoDataFrame(
        rows_shp,
        geometry=geoms,
        crs="EPSG:2154",
    )
    gdf_gpkg = gpd.GeoDataFrame(
        rows_gpkg,
        geometry=geoms,
        crs="EPSG:2154",
    )

    output_path = Path(output_path)
    if output_path.suffix.lower() != ".shp":
        output_path = output_path.with_suffix(".shp")

    gdf_shp.to_file(output_path, driver="ESRI Shapefile", encoding="utf-8")
    gpkg_path = output_path.with_suffix(".gpkg")
    gdf_gpkg.to_file(gpkg_path, driver="GPKG", layer="parcelles")
