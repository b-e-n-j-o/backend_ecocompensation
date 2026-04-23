#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_uf_classement_csv.py
===========================

Export CSV des sous-ensembles UF (une ligne par sous-ensemble, ordre = classement).
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import TYPE_CHECKING

from exports.classement_export_attrs import build_parcelle_export_row
from exports.export_classement_csv import CSV_FIELDNAMES, _csv_cell
from exports.uf_export_adapter import build_subset_export_inputs

if TYPE_CHECKING:
    pass


def export_uf_classement_csv(results_uf: dict, output_path: Path | io.StringIO) -> None:
    items = build_subset_export_inputs(results_uf)
    if not items:
        return
    from vrai_filtre import FiltreOptions

    if isinstance(output_path, Path):
        file_obj = open(output_path, "w", newline="", encoding="utf-8-sig")
    else:
        file_obj = output_path

    import csv

    writer = csv.DictWriter(file_obj, fieldnames=CSV_FIELDNAMES, delimiter=";")
    writer.writeheader()
    options = FiltreOptions.defaut()

    for item in items:
        row = build_parcelle_export_row(
            item["parcelle"],
            item["mmap"],
            options,
            clip_for_shapefile=False,
        )
        writer.writerow({k: _csv_cell(row.get(k)) for k in CSV_FIELDNAMES})

    if isinstance(output_path, Path):
        file_obj.close()
