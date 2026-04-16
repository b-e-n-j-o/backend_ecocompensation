#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_classement_csv.py
========================

Export des parcelles classées en CSV (sans géométrie).
Colonnes alignées sur le tableau RankingTable + blocs texte par profiler (comme RankingLine).
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import TYPE_CHECKING, Any

from export_classement_pool_text import (
    build_detail_columns,
    extract_table_scalars,
    metrics_rows_to_map,
    pool_metrics_json_compact,
)

if TYPE_CHECKING:
    pass


def export_classement_csv(
    parcelles: list[dict],
    output_path: Path | io.StringIO,
    metrics_by_idu: dict[str, list[dict[str, Any]]] | None = None,
) -> None:
    """
    Exporte les parcelles classées en CSV.
    parcelles : liste de dicts (rank, idu, code_insee, section, numero, surface_ha, miller,
                distance_km, dist_hydro_m, …).
    metrics_by_idu : optionnel, { idu: [ { metric_key, metric_value_jsonb }, … ] } depuis le pool run.
    """
    if not parcelles:
        return

    fieldnames = [
        "rang",
        "insee",
        "section",
        "numero",
        "idu",
        "distance_km",
        "dist_hydro_m",
        "score_eco",
        "score_eco_max",
        "score_composite",
        "durete",
        "surface_ha",
        "miller",
        "scoring_details",
        "composite_details",
        "durete_details",
        "especes_details",
        "vegetation_hybride_details",
        "cosia_details",
        "carhab_details",
        "arrachage_vignes_details",
        "personnes_morales_details",
        "zone_humide_details",
        "pool_metrics_json",
    ]

    if isinstance(output_path, Path):
        file_obj = open(output_path, "w", newline="", encoding="utf-8-sig")
    else:
        file_obj = output_path

    writer = csv.DictWriter(
        file_obj,
        fieldnames=fieldnames,
        delimiter=";",
        quoting=csv.QUOTE_MINIMAL,
    )
    writer.writeheader()

    by_idu = metrics_by_idu or {}

    for p in parcelles:
        idu = str(p.get("idu") or "")
        mmap = metrics_rows_to_map(by_idu.get(idu) or [])
        scalars = extract_table_scalars(mmap)
        details = build_detail_columns(mmap)

        dh = p.get("dist_hydro_m")
        row = {
            "rang": p.get("rank", 0),
            "insee": p.get("code_insee") or "",
            "section": p.get("section") or "",
            "numero": p.get("numero") or "",
            "idu": idu,
            "surface_ha": round(float(p.get("surface_ha") or 0), 2),
            "miller": round(float(p.get("miller") or 0), 4),
            "distance_km": round(float(p.get("distance_km") or 0), 2),
            "dist_hydro_m": round(float(dh), 0) if dh is not None else "",
            "score_eco": scalars["score_eco"] if scalars["score_eco"] != "" else "",
            "score_eco_max": scalars["score_eco_max"] if scalars["score_eco_max"] != "" else "",
            "score_composite": (
                f"{float(scalars['score_composite']):.1f}"
                if isinstance(scalars.get("score_composite"), (int, float))
                else ""
            ),
            "durete": scalars["durete"] if scalars["durete"] != "" else "",
            "scoring_details": details["scoring_details"],
            "composite_details": details["composite_details"],
            "durete_details": details["durete_details"],
            "especes_details": details["especes_details"],
            "vegetation_hybride_details": details["vegetation_hybride_details"],
            "cosia_details": details["cosia_details"],
            "carhab_details": details["carhab_details"],
            "arrachage_vignes_details": details["arrachage_vignes_details"],
            "personnes_morales_details": details["personnes_morales_details"],
            "zone_humide_details": details["zone_humide_details"],
            "pool_metrics_json": pool_metrics_json_compact(mmap),
        }
        writer.writerow(row)

    if isinstance(output_path, Path):
        file_obj.close()
