#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_uf_classement_shp.py
===========================

Export des sous-ensembles UF classés (dernier filtre UF) en Shapefile EPSG:2154.
Géométries : table ecocompensation_results.sous_ensembles (comme le GeoJSON UF).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import geopandas as gpd
from sqlalchemy import text
from exports.classement_export_attrs import build_parcelle_export_row
from exports.qgis_encoding import write_geodataframe_shapefile_qgis
from exports.uf_export_adapter import build_subset_export_inputs

if TYPE_CHECKING:
    from sqlalchemy import Engine


def export_uf_classement_shp(
    engine: "Engine",
    project_id: str,
    results_uf: dict,
    options,  # FiltreOptions
    output_path: Path,
    final_radius_km: float = 0.0,
) -> None:
    items = build_subset_export_inputs(results_uf)
    if not items:
        raise ValueError("Aucune unité foncière dans les résultats UF.")

    ordered_subset_ids: list[str] = []
    by_subset: dict[str, dict] = {}
    for it in items:
        sid = str(it["subset_id"])
        ordered_subset_ids.append(sid)
        by_subset[sid] = it

    if not ordered_subset_ids:
        raise ValueError("Aucun sous-ensemble (subset_id) dans les résultats UF.")

    sql = text("""
        SELECT s.subset_id, s.geom_2154
        FROM ecocompensation_results.sous_ensembles s
        WHERE s.project_id = :pid
          AND s.subset_id = ANY(:subset_ids)
    """)
    with engine.connect() as conn:
        gdf_geom = gpd.read_postgis(
            sql,
            conn,
            geom_col="geom_2154",
            params={"pid": project_id, "subset_ids": ordered_subset_ids},
        )

    geom_by_subset = {str(row["subset_id"]): row["geom_2154"] for _, row in gdf_geom.iterrows()}

    geoms = []
    rows_attr = []
    missing: list[str] = []
    for sid in ordered_subset_ids:
        geom = geom_by_subset.get(sid)
        item = by_subset.get(sid)
        if geom is None:
            missing.append(sid)
            continue
        if not item:
            continue
        geoms.append(geom)
        rows_attr.append(
            build_parcelle_export_row(
                item["parcelle"],
                item["mmap"],
                options,
                clip_for_shapefile=True,
            )
        )

    if missing:
        raise ValueError(
            "Géométrie manquante en base pour "
            f"{len(missing)} sous-ensemble(s) : {missing[:5]}"
            + ("…" if len(missing) > 5 else "")
        )

    if not geoms:
        raise ValueError(
            "Aucune géométrie sous-ensemble trouvée en base pour ce classement UF."
        )

    gdf = gpd.GeoDataFrame(rows_attr, geometry=geoms, crs="EPSG:2154")

    output_path = Path(output_path)
    if output_path.suffix.lower() != ".shp":
        output_path = output_path.with_suffix(".shp")

    write_geodataframe_shapefile_qgis(gdf, output_path)
