from __future__ import annotations

from typing import Any


def _parse_idu_parts(raw_idu: str) -> tuple[str, str, str]:
    raw = (raw_idu or "").strip()
    if not raw:
        return "", "", ""
    return raw[:5], raw[8:10], raw[-4:]


def build_subset_export_inputs(results_uf: dict) -> list[dict[str, Any]]:
    """
    Normalise les sous-ensembles UF vers un format compatible export parcelles.
    Retourne une liste triée dans l'ordre de classement UF puis ordre local des sous-ensembles.
    """
    out: list[dict[str, Any]] = []
    unites = results_uf.get("unites_foncieres") or []
    rank = 1
    for uf in unites:
        for ss in (uf.get("sous_ensembles") or []):
            subset_id = str(ss.get("subset_id") or "").strip()
            if not subset_id:
                continue
            idus = ss.get("idus") or []
            first_idu = str(idus[0]) if idus else ""
            code_insee, section, numero = _parse_idu_parts(first_idu)
            score = ss.get("score")
            score_num = float(score) if isinstance(score, (int, float)) else None
            parcelle = {
                "rank": rank,
                "idu": subset_id,
                "code_insee": code_insee,
                "section": section,
                "numero": numero,
                "surface_ha": float(ss.get("surface_ha") or 0),
                "miller": float(ss.get("miller") or 0),
                "distance_km": float(ss.get("distance_centre_km") or 0),
                "dist_hydro_m": ss.get("dist_hydro_m"),
            }
            mmap: dict[str, Any] = {
                "parcelles_personnes_morales": {
                    "intersects_pm_database": bool(ss.get("siren") or ss.get("denomination")),
                    "siren": (ss.get("siren") or None),
                    "denomination": (ss.get("denomination") or None),
                    "forme_juridique": None,
                },
            }
            if score_num is not None:
                mmap["score_eco"] = {"total_score": score_num, "max_score": 6}
            out.append(
                {
                    "subset_id": subset_id,
                    "parcelle": parcelle,
                    "mmap": mmap,
                }
            )
            rank += 1
    return out

