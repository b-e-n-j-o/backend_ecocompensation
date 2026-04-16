#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_classement_shp.py
========================

Export des parcelles classées (sortie du vrai filtre + scoring) en une couche
Shapefile pour visualisation dans QGIS. Attributs alignés sur le classement
actuel (scores pool) + textes détail tronqués à 254 car. (limite DBF).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import geopandas as gpd
from sqlalchemy import text

from export_classement_pool_text import (
    build_detail_columns,
    extract_table_scalars,
    metrics_rows_to_map,
    shp_trunc,
)

if TYPE_CHECKING:
    from sqlalchemy import Engine


def _shp_num_eco(v: Any) -> float:
    if isinstance(v, (int, float)) and v == v:
        return float(v)
    return -9999.0


def _shp_num_eco_max(v: Any) -> float:
    if isinstance(v, (int, float)) and v == v and v > 0:
        return float(v)
    return -1.0


def _shp_num_composite(v: Any) -> float:
    if isinstance(v, (int, float)) and v == v:
        return round(float(v), 4)
    return -9999.0


def _shp_num_durete(v: Any) -> float:
    if isinstance(v, (int, float)) and v == v:
        return float(int(v))
    return -1.0


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
    metrics_by_idu : { idu: lignes métriques pool } — optionnel (sinon champs scores vides / -9999).
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

    by_idu = metrics_by_idu or {}

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

        mmap = metrics_rows_to_map(by_idu.get(str(idu)) or [])
        scalars = extract_table_scalars(mmap)
        details = build_detail_columns(mmap)

        rows_attr.append(
            {
                "rang": int(p.get("rank", 0) or 0),
                "idu": str(idu)[:254],
                "cinsee": str(p.get("code_insee") or "")[:10],
                "section": str(p.get("section") or "")[:10],
                "numero": str(p.get("numero") or "")[:10],
                "surf_ha": round(float(p.get("surface_ha") or 0), 2),
                "miller": round(float(p.get("miller") or 0), 4),
                "dist_km": round(float(dist_km or 0), 2),
                "dist_hyd": round(float(dist_hyd), 0) if dist_hyd is not None else -1,
                "eco_tot": _shp_num_eco(scalars.get("score_eco")),
                "eco_max": _shp_num_eco_max(scalars.get("score_eco_max")),
                "cmp_tot": _shp_num_composite(scalars.get("score_composite")),
                "dur_tot": _shp_num_durete(scalars.get("durete")),
                "zdv": (zdv_str or "—")[:254],
                "troncon": str(troncon_str)[:254],
                "surf_hyd": str(surf_hyd_str)[:254],
                "rayon_km": round(float(final_radius_km or 0), 1),
                "txt_scor": shp_trunc(details["scoring_details"]),
                "txt_comp": shp_trunc(details["composite_details"]),
                "txt_dure": shp_trunc(details["durete_details"]),
                "txt_espe": shp_trunc(details["especes_details"]),
                "txt_vege": shp_trunc(details["vegetation_hybride_details"]),
                "txt_cosi": shp_trunc(details["cosia_details"]),
                "txt_carb": shp_trunc(details["carhab_details"]),
                "txt_arra": shp_trunc(details["arrachage_vignes_details"]),
                "txt_pppm": shp_trunc(details["personnes_morales_details"]),
                "txt_zhum": shp_trunc(details["zone_humide_details"]),
            }
        )

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

    output_path = Path(output_path)
    if output_path.suffix.lower() != ".shp":
        output_path = output_path.with_suffix(".shp")

    gdf.to_file(output_path, driver="ESRI Shapefile", encoding="utf-8")
