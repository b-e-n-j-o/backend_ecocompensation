#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_uf_classement_csv.py
===========================

CSV QGIS / Excel : une ligne par parcelle, groupable par uf_id / uf_rang.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from api.recherche_de_foncier.enrichir.pool.exports.export_classement_csv import _csv_cell
from api.recherche_de_foncier.enrichir.pool.exports.qgis_encoding import QGIS_CSV_ENCODING
from api.recherche_de_foncier.enrichir.pool.exports.uf_export_adapter import (
    UF_PARCELLE_CSV_FIELDS,
    build_uf_parcelle_attr_rows,
)


def export_uf_classement_csv(results_uf: dict, output_path: Path | io.StringIO) -> None:
    rows = build_uf_parcelle_attr_rows(results_uf)
    if not rows:
        return

    if isinstance(output_path, Path):
        file_obj = open(output_path, "w", newline="", encoding=QGIS_CSV_ENCODING)
    else:
        file_obj = output_path

    writer = csv.DictWriter(file_obj, fieldnames=UF_PARCELLE_CSV_FIELDS, delimiter=";")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _csv_cell(row.get(k)) for k in UF_PARCELLE_CSV_FIELDS})

    if isinstance(output_path, Path):
        file_obj.close()
