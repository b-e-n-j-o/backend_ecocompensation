#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_classement_csv.py
========================

Export des parcelles classées en CSV (sans géométrie).
Colonnes alignées sur export_classement_shp + colonne optionnelle pool_metrics_json.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import TYPE_CHECKING, Any

from exports.classement_export_attrs import (
    EXPORT_ATTR_KEYS,
    build_parcelle_export_row,
    mmap_for_parcelle,
)
from exports.export_classement_pool_text import pool_metrics_json_compact
from exports.qgis_encoding import QGIS_CSV_ENCODING, normalize_unicode_text
from filtre_options import FiltreOptions

if TYPE_CHECKING:
    pass

# Ordre stable, aligné sur le contrat SHP (+ métriques brutes en fin)
CSV_FIELDNAMES: list[str] = [*EXPORT_ATTR_KEYS, "pool_metrics_json"]


def _csv_cell(v: Any) -> str:
    """Valeurs scalaires pour CSV (booléens en true/false minuscules, UTF-8 NFC)."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        from math import isfinite

        if not isfinite(v):
            return ""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    return normalize_unicode_text(v)


def export_classement_csv(
    parcelles: list[dict],
    output_path: Path | io.StringIO,
    metrics_by_idu: dict[str, list[dict[str, Any]]] | None = None,
    options: Any | None = None,
) -> None:
    """
    Exporte les parcelles classées en CSV (même schéma attributaire que le SHP).

    :param options: FiltreOptions — requis pour remplir ebc, zdv, troncon, etc.
                    comme sur le SHP. Si None, valeurs neutres via un objet minimal.
    """
    if not parcelles:
        return

    if options is None:
        options = FiltreOptions.defaut()

    if isinstance(output_path, Path):
        file_obj = open(output_path, "w", newline="", encoding=QGIS_CSV_ENCODING)
    else:
        file_obj = output_path

    writer = csv.DictWriter(
        file_obj,
        fieldnames=CSV_FIELDNAMES,
        delimiter=";",
        quoting=csv.QUOTE_MINIMAL,
    )
    writer.writeheader()

    for p in parcelles:
        mmap = mmap_for_parcelle(p, metrics_by_idu)
        row = build_parcelle_export_row(p, mmap, options, clip_for_shapefile=False)
        row["pool_metrics_json"] = pool_metrics_json_compact(mmap)
        writer.writerow({k: _csv_cell(row.get(k)) for k in CSV_FIELDNAMES})

    if isinstance(output_path, Path):
        file_obj.close()
