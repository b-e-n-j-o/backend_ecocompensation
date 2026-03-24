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
    unites = results_uf.get("unites_foncieres") or []
    if not unites:
        raise ValueError("Aucune unité foncière dans les résultats UF.")

    # Ordre = même que le tableau (UF puis sous-ensembles)
    ordered_subset_ids: list[str] = []
    meta_by_subset: dict[str, dict] = {}
    for uf in unites:
        uf_rang = uf.get("rang", 0)
        uf_id = str(uf.get("uf_id", ""))
        for ss in uf.get("sous_ensembles", []) or []:
            sid = ss.get("subset_id")
            if not sid:
                continue
            s = str(sid)
            ordered_subset_ids.append(s)
            meta_by_subset[s] = {
                "uf_rang": uf_rang,
                "uf_id": uf_id,
                "nb_parcelles_uf": uf.get("nb_parcelles", 0),
                "k": ss.get("k", 0),
                "surface_ha": ss.get("surface_ha", 0),
                "miller": ss.get("miller", 0),
                "distance_centre_km": ss.get("distance_centre_km", 0),
                "dist_hydro_m": ss.get("dist_hydro_m"),
                "score": ss.get("score", 0),
                "siren": ss.get("siren") or "",
                "denomination": ss.get("denomination") or "",
            }

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

    geoms = []
    rows_attr = []
    missing: list[str] = []
    for sid in ordered_subset_ids:
        geom = geom_by_subset.get(sid)
        m = meta_by_subset.get(sid, {})
        if geom is None:
            missing.append(sid)
            continue
        geoms.append(geom)
        dh = m.get("dist_hydro_m")
        rows_attr.append(
            {
                "rang_uf": int(m.get("uf_rang", 0)),
                "uf_id": m.get("uf_id", "")[:254],
                "subset_id": sid[:254],
                "k": int(m.get("k", 0) or 0),
                "surf_ha": round(float(m.get("surface_ha") or 0), 2),
                "miller": round(float(m.get("miller") or 0), 4),
                "dist_km": round(float(m.get("distance_centre_km") or 0), 3),
                "dist_hyd": round(float(dh), 0) if dh is not None else -1,
                "score": int(m.get("score", 0) or 0),
                "siren": str(m.get("siren") or "")[:254],
                "denom": str(m.get("denomination") or "")[:254],
                "zdv": zdv_str[:254],
                "troncon": troncon_str[:254],
                "surf_hyd": surf_hyd_str[:254],
                "rayon_km": round(final_radius_km, 1),
            }
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

    gdf.to_file(output_path, driver="ESRI Shapefile", encoding="utf-8")
