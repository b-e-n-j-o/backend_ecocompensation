"""
Couches de contexte étude pour l'export GeoPackage (même famille que Données internes) :
  - zone_projet  : emprise foncier (sinon union des parcelles projet)
  - aire_etude   : AOI de recherche
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import geopandas as gpd
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Engine

LAYER_ZONE_PROJET = "zone_projet"
LAYER_AIRE_ETUDE = "aire_etude"


def _set_crs_2154(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if gdf.crs is None:
        return gdf.set_crs(2154)
    return gdf.to_crs(2154)


def load_project_context_layers(
    engine: "Engine",
    project_id: str,
) -> dict[str, gpd.GeoDataFrame]:
    """GeoDataFrames EPSG:2154, clés ``zone_projet`` / ``aire_etude`` si géométrie présente."""
    out: dict[str, gpd.GeoDataFrame] = {}
    with engine.connect() as conn:
        proj = conn.execute(
            text(
                """
                SELECT id, name, foncier_id, aoi_id
                FROM ecocompensation.projects
                WHERE id = CAST(:pid AS uuid)
                """
            ),
            {"pid": project_id},
        ).mappings().one_or_none()
        if not proj:
            return out
        projet = str(proj.get("name") or "")
        pid = str(proj["id"])
        foncier_id = proj.get("foncier_id")
        aoi_id = proj.get("aoi_id")

        foncier_gdf = None
        if foncier_id:
            foncier_gdf = gpd.read_postgis(
                text(
                    """
                    SELECT
                        id AS foncier_id,
                        name AS foncier_nom,
                        area_ha,
                        geom_2154
                    FROM ecocompensation.foncier
                    WHERE id = CAST(:fid AS uuid)
                      AND geom_2154 IS NOT NULL
                    """
                ),
                conn,
                geom_col="geom_2154",
                params={"fid": str(foncier_id)},
            )
        if foncier_gdf is None or foncier_gdf.empty:
            foncier_gdf = gpd.read_postgis(
                text(
                    """
                    SELECT
                        CAST(:pid AS uuid) AS foncier_id,
                        CAST(:projet AS text) AS foncier_nom,
                        ROUND((ST_Area(ST_Union(geom_2154)) / 10000.0)::numeric, 2) AS area_ha,
                        ST_Multi(ST_Union(geom_2154)) AS geom_2154
                    FROM ecocompensation.project_parcelles
                    WHERE project_id = CAST(:pid AS uuid)
                      AND geom_2154 IS NOT NULL
                    HAVING ST_Union(geom_2154) IS NOT NULL
                    """
                ),
                conn,
                geom_col="geom_2154",
                params={"pid": pid, "projet": projet},
            )

        if foncier_gdf is not None and not foncier_gdf.empty and foncier_gdf.geometry.notna().any():
            gdf = _set_crs_2154(foncier_gdf.copy())
            gdf["libelle"] = "Zone projet"
            gdf["projet"] = projet
            gdf["project_id"] = pid
            out[LAYER_ZONE_PROJET] = gdf

        if aoi_id:
            aoi_gdf = gpd.read_postgis(
                text(
                    """
                    SELECT
                        id AS aoi_id,
                        code_insee,
                        buffer_m,
                        geom_2154
                    FROM ecocompensation.aoi
                    WHERE id = CAST(:aid AS uuid)
                      AND geom_2154 IS NOT NULL
                    """
                ),
                conn,
                geom_col="geom_2154",
                params={"aid": str(aoi_id)},
            )
            if aoi_gdf is not None and not aoi_gdf.empty and aoi_gdf.geometry.notna().any():
                gdf = _set_crs_2154(aoi_gdf.copy())
                buf = gdf["buffer_m"].iloc[0]
                gdf["libelle"] = "Aire d'étude"
                gdf["projet"] = projet
                gdf["project_id"] = pid
                try:
                    gdf["buffer_km"] = round(float(buf) / 1000.0, 2) if buf is not None else None
                except (TypeError, ValueError):
                    gdf["buffer_km"] = None
                out[LAYER_AIRE_ETUDE] = gdf
    return out


def write_geopackage_etude(
    gpkg_path: Path,
    main_gdf: gpd.GeoDataFrame,
    main_layer: str,
    context: dict[str, gpd.GeoDataFrame],
) -> Path:
    """
    Un GeoPackage : couche métier + zone projet + aire d'étude.
    ``mode=a`` pour empiler les couches sans écraser le fichier.
    """
    gpkg_path = Path(gpkg_path)
    if gpkg_path.exists():
        gpkg_path.unlink()
    main_gdf.to_file(gpkg_path, driver="GPKG", layer=main_layer)
    for layer_name, gdf in (
        (LAYER_ZONE_PROJET, context.get(LAYER_ZONE_PROJET)),
        (LAYER_AIRE_ETUDE, context.get(LAYER_AIRE_ETUDE)),
    ):
        if gdf is None or gdf.empty:
            continue
        gdf.to_file(gpkg_path, driver="GPKG", layer=layer_name, mode="a")
    return gpkg_path
