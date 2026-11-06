#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_uf_classement_shp.py
===========================

Export SIG des unités foncières PM (EPSG:2154) pensé pour QGIS :

  GeoPackage (uf.gpkg)
    unites_foncieres  — 1 polygone = union des parcelles de l'UF, attributs agrégés
    uf_parcelles      — 1 polygone = une parcelle, mêmes colonnes que l'export parcelles
    dictionnaire      — description des colonnes et règles d'agrégation
    zone_projet / aire_etude

  Shapefiles (mêmes objets, textes tronqués à 254 c.)
    unites_foncieres.shp
    parcelles.shp

Construction des couches : export_uf_layers.build_uf_export.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import geopandas as gpd

from api.recherche_de_foncier.enrichir.pool.exports.export_uf_layers import (
    build_uf_export,
    write_attribute_table,
)
from api.recherche_de_foncier.enrichir.pool.exports.project_context_layers import (
    load_project_context_layers,
    write_geopackage_etude,
)
from api.recherche_de_foncier.enrichir.pool.exports.qgis_encoding import write_geodataframe_shapefile_qgis

if TYPE_CHECKING:
    from sqlalchemy import Engine


def build_uf_geodataframes(
    engine: "Engine",
    project_id: str,
    results_uf: dict,
    options=None,  # noqa: ARG001 — signature historique ; le filtre UF est relu en base
    output_path: Path | None = None,  # noqa: ARG001
    final_radius_km: float = 0.0,  # noqa: ARG001
) -> dict[str, gpd.GeoDataFrame]:
    """Couches GPKG (textes complets) : unites_foncieres, uf_parcelles."""
    exp = build_uf_export(engine, project_id, results_uf)
    return {"unites_foncieres": exp.unites_foncieres, "uf_parcelles": exp.uf_parcelles}


def export_uf_classement_shp(
    engine: "Engine",
    project_id: str,
    results_uf: dict,
    options,  # noqa: ARG001
    output_path: Path,
    final_radius_km: float = 0.0,  # noqa: ARG001
) -> None:
    exp = build_uf_export(engine, project_id, results_uf)
    base = Path(output_path).parent
    write_geodataframe_shapefile_qgis(exp.unites_foncieres_shp, base / "unites_foncieres.shp")
    write_geodataframe_shapefile_qgis(exp.uf_parcelles_shp, base / "parcelles.shp")
    context = load_project_context_layers(engine, project_id)
    gpkg = base / "uf.gpkg"
    write_geopackage_etude(
        gpkg, exp.unites_foncieres, "unites_foncieres", context,
        extra_layers=[("uf_parcelles", exp.uf_parcelles)],
    )
    write_attribute_table(gpkg, exp.dictionnaire, "dictionnaire")
