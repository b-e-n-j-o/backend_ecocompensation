#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_uf_classement_csv.py
===========================

Export CSV des sous-ensembles UF (une ligne par sous-ensemble, ordre = classement).
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


def export_uf_classement_csv(results_uf: dict, output_path: Path | io.StringIO) -> None:
    unites = results_uf.get("unites_foncieres") or []
    if not unites:
        return

    fieldnames = [
        "rang_uf",
        "uf_id",
        "nb_parcelles_uf",
        "subset_id",
        "k",
        "idus",
        "surface_ha",
        "miller",
        "distance_centre_km",
        "dist_hydro_m",
        "score",
        "score_details",
        "siren",
        "denomination",
    ]

    if isinstance(output_path, Path):
        file_obj = open(output_path, "w", newline="", encoding="utf-8-sig")
    else:
        file_obj = output_path

    writer = csv.DictWriter(file_obj, fieldnames=fieldnames, delimiter=";")
    writer.writeheader()

    for uf in unites:
        uf_rang = uf.get("rang", 0)
        uf_id = uf.get("uf_id", "")
        nb_parcelles = uf.get("nb_parcelles", 0)
        for ss in uf.get("sous_ensembles", []) or []:
            score_details = ss.get("score_details", []) or []
            details_str = " | ".join(
                [
                    f"{d.get('critere', '')}: {d.get('raison', '')} (+{d.get('points', 0)} pts)"
                    for d in score_details
                    if d.get("points", 0) > 0
                ]
            )
            idus = ss.get("idus") or []
            idus_str = ", ".join(str(x) for x in idus)
            dh = ss.get("dist_hydro_m")
            writer.writerow(
                {
                    "rang_uf": uf_rang,
                    "uf_id": uf_id,
                    "nb_parcelles_uf": nb_parcelles,
                    "subset_id": ss.get("subset_id", ""),
                    "k": ss.get("k", ""),
                    "idus": idus_str,
                    "surface_ha": round(float(ss.get("surface_ha") or 0), 2),
                    "miller": round(float(ss.get("miller") or 0), 4),
                    "distance_centre_km": round(float(ss.get("distance_centre_km") or 0), 3),
                    "dist_hydro_m": round(float(dh), 0) if dh is not None else "",
                    "score": ss.get("score", 0),
                    "score_details": details_str,
                    "siren": ss.get("siren") or "",
                    "denomination": ss.get("denomination") or "",
                }
            )

    if isinstance(output_path, Path):
        file_obj.close()
