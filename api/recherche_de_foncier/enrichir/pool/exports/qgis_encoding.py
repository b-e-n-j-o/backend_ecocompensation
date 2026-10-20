"""
Encodage des exports CSV / Shapefile pour QGIS (accents, UTF-8).

- CSV : UTF-8 avec BOM (utf-8-sig), séparateur « ; » — détection fiable dans QGIS.
- SHP : DBF en UTF-8 + fichier sidecar ``.cpg`` (indispensable pour QGIS / GDAL).
"""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any

import geopandas as gpd

# Libellé attendu par QGIS dans le fichier .cpg (ASCII).
QGIS_SHAPEFILE_ENCODING = "UTF-8"

# CSV : BOM UTF-8 pour Excel et QGIS (Add delimited text layer).
QGIS_CSV_ENCODING = "utf-8-sig"


def normalize_unicode_text(value: Any) -> str:
    """Normalise en NFC pour un affichage stable des accents (é, è, œ…)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if value != value:  # NaN
            return ""
    s = str(value)
    return unicodedata.normalize("NFC", s)


def ensure_shapefile_cpg(
    shp_path: Path | str,
    encoding: str = QGIS_SHAPEFILE_ENCODING,
) -> None:
    """
    Écrit ou met à jour ``.cpg`` à côté du .shp.
    Sans ce fichier, QGIS interprète souvent le DBF en Latin-1 → accents corrompus.
    """
    p = Path(shp_path)
    if p.suffix.lower() != ".shp":
        p = p.with_suffix(".shp")
    cpg = p.with_suffix(".cpg")
    cpg.write_text(f"{encoding.strip()}\n", encoding="ascii")


def _normalize_gdf_text_columns(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    out = gdf.copy()
    geom_name = out.geometry.name
    for col in out.columns:
        if col == geom_name:
            continue
        if out[col].dtype != object:
            continue

        def _norm_cell(v: object) -> object:
            if v is None:
                return v
            if isinstance(v, (int, float, bool)):
                return v
            return normalize_unicode_text(v)

        out[col] = out[col].map(_norm_cell)
    return out


def write_geodataframe_shapefile_qgis(
    gdf: gpd.GeoDataFrame,
    output_path: Path | str,
    *,
    encoding: str = "utf-8",
) -> Path:
    """
    Écrit un shapefile ESRI en UTF-8 et force le sidecar ``.cpg`` pour QGIS.
    """
    output_path = Path(output_path)
    if output_path.suffix.lower() != ".shp":
        output_path = output_path.with_suffix(".shp")

    gdf_norm = _normalize_gdf_text_columns(gdf)
    gdf_norm.to_file(output_path, driver="ESRI Shapefile", encoding=encoding)
    ensure_shapefile_cpg(output_path)
    return output_path
