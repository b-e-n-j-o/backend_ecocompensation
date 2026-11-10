#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Un seul GeoPackage QGIS pour l'étude.

Couches selon la sélection :
  parcelles
  unites_foncieres, uf_parcelles
  ilots_rpg, ilots_parcelles
  zone_projet, aire_etude
  dictionnaire (table sans géométrie, si des UF sont exportées)
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import geopandas as gpd
import pandas as pd

from api.recherche_de_foncier.enrichir.pool.exports.export_classement_shp import build_parcelles_geodataframes
from api.recherche_de_foncier.enrichir.pool.exports.export_ilots_rpg_layers import build_ilots_geodataframes
from api.recherche_de_foncier.enrichir.pool.exports.export_uf_layers import build_uf_export, write_attribute_table
from api.recherche_de_foncier.enrichir.pool.exports.project_context_layers import (
    LAYER_AIRE_ETUDE,
    LAYER_ZONE_PROJET,
    load_project_context_layers,
)

if TYPE_CHECKING:
    from sqlalchemy import Engine

LAYER_ORDER = (
    "parcelles",
    "unites_foncieres",
    "uf_parcelles",
    "ilots_rpg",
    "ilots_parcelles",
)


def write_etude_geopackage(
    engine: "Engine",
    project_id: str,
    gpkg_path: Path,
    *,
    include_parcelles: bool,
    include_uf: bool,
    include_ilots: bool,
    parcelles: list[dict] | None,
    parcelle_options,
    aoi_id: str | None,
    metrics_by_idu: dict[str, list[dict[str, Any]]] | None,
    results_uf: dict | None,
    ilot_ids: set[str] | None = None,
) -> list[str]:
    layers: dict[str, gpd.GeoDataFrame] = {}
    skipped: list[str] = []
    dictionnaire: pd.DataFrame | None = None

    if include_parcelles:
        try:
            _, gdf = build_parcelles_geodataframes(
                engine, project_id, parcelles or [], parcelle_options,
                aoi_id=aoi_id, metrics_by_idu=metrics_by_idu,
            )
            layers["parcelles"] = gdf
        except ValueError as exc:
            skipped.append(f"parcelles : {exc}")

    if include_uf and results_uf:
        try:
            exp = build_uf_export(engine, project_id, results_uf)
            layers["unites_foncieres"] = exp.unites_foncieres
            layers["uf_parcelles"] = exp.uf_parcelles
            dictionnaire = exp.dictionnaire
        except ValueError as exc:
            skipped.append(f"unités foncières : {exc}")
    elif include_uf:
        skipped.append("unités foncières : aucun résultat")

    if include_ilots:
        try:
            layers.update(build_ilots_geodataframes(engine, project_id, num_ilots=ilot_ids))
        except ValueError as exc:
            skipped.append(f"îlots RPG : {exc}")

    if not layers:
        detail = " ; ".join(skipped) or "aucune couche demandée"
        raise ValueError(f"Rien à exporter ({detail}).")

    gpkg_path = Path(gpkg_path)
    if gpkg_path.exists():
        gpkg_path.unlink()
    first = True
    written: list[str] = []
    for name in LAYER_ORDER:
        gdf = layers.get(name)
        if gdf is None or gdf.empty:
            continue
        gdf.to_file(gpkg_path, driver="GPKG", layer=name, mode="w" if first else "a")
        first = False
        written.append(name)

    context = load_project_context_layers(engine, project_id)
    for name in (LAYER_ZONE_PROJET, LAYER_AIRE_ETUDE):
        gdf = context.get(name)
        if gdf is None or gdf.empty:
            continue
        gdf.to_file(gpkg_path, driver="GPKG", layer=name, mode="a")
        written.append(name)
    if dictionnaire is not None:
        write_attribute_table(gpkg_path, dictionnaire, "dictionnaire")
        written.append("dictionnaire")
    return written
