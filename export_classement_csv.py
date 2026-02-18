#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_classement_csv.py
========================

Export des parcelles classées en CSV (sans géométrie).
Contient toutes les colonnes visibles dans le tableau de résultats.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


def export_classement_csv(parcelles: list[dict], output_path: Path | io.StringIO) -> None:
    """
    Exporte les parcelles classées en CSV.
    parcelles : liste de dicts avec rank, idu, code_insee, section, numero,
                surface_ha, miller, distance_km, dist_hydro_m, score, score_details.
    output_path : Path vers le fichier CSV ou StringIO pour retourner en mémoire.
    """
    if not parcelles:
        return

    # Colonnes à exporter (ordre d'affichage dans le tableau)
    fieldnames = [
        "rang",
        "idu",
        "code_insee",
        "section",
        "numero",
        "surface_ha",
        "miller",
        "distance_km",
        "dist_hydro_m",
        "score",
        "score_details",
    ]

    # Ouvrir le fichier ou StringIO
    if isinstance(output_path, Path):
        file_obj = open(output_path, "w", newline="", encoding="utf-8-sig")
    else:
        file_obj = output_path

    writer = csv.DictWriter(file_obj, fieldnames=fieldnames, delimiter=";")
    writer.writeheader()

    for p in parcelles:
        # Formater les détails du score en texte lisible
        score_details = p.get("score_details", [])
        details_str = " | ".join(
            [
                f"{d.get('critere', '')}: {d.get('raison', '')} (+{d.get('points', 0)} pts)"
                for d in score_details
                if d.get("points", 0) > 0
            ]
        )

        row = {
            "rang": p.get("rank", 0),
            "idu": p.get("idu", ""),
            "code_insee": p.get("code_insee", ""),
            "section": p.get("section", ""),
            "numero": p.get("numero", ""),
            "surface_ha": round(float(p.get("surface_ha") or 0), 2),
            "miller": round(float(p.get("miller") or 0), 4),
            "distance_km": round(float(p.get("distance_km") or 0), 2),
            "dist_hydro_m": round(float(p.get("dist_hydro_m") or 0), 0)
            if p.get("dist_hydro_m") is not None
            else "",
            "score": p.get("score", 0),
            "score_details": details_str,
        }
        writer.writerow(row)

    if isinstance(output_path, Path):
        file_obj.close()
