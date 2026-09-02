from __future__ import annotations

from typing import Any


def _parse_idu_parts(raw_idu: str) -> tuple[str, str, str]:
    raw = (raw_idu or "").strip()
    if not raw:
        return "", "", ""
    return raw[:5], raw[8:10], raw[-4:]


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def build_subset_export_inputs(results_uf: dict) -> list[dict[str, Any]]:
    """
    Normalise les sous-ensembles UF vers un format compatible export parcelles.
    Retourne une liste triée dans l'ordre de classement UF puis ordre local des sous-ensembles.
    """
    out: list[dict[str, Any]] = []
    unites = results_uf.get("unites_foncieres") or []
    rank = 1
    for uf in unites:
        pm_uf = _as_dict(uf.get("pm_prospect"))
        for ss in (uf.get("sous_ensembles") or []):
            subset_id = str(ss.get("subset_id") or "").strip()
            if not subset_id:
                continue
            idus = ss.get("idus") or []
            first_idu = str(idus[0]) if idus else ""
            code_insee, section, numero = _parse_idu_parts(first_idu)
            score_eco = ss.get("score_eco") if isinstance(ss.get("score_eco"), dict) else None
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
                "zone_humide_ha": ss.get("zone_humide_ha"),
                "surface_hydro_ha": ss.get("surface_hydro_ha"),
                "dist_surface_hydro_m": ss.get("dist_surface_hydro_m"),
                "veg_libelles": ss.get("veg_libelles") or [],
                "fauna_distances": ss.get("fauna_distances") or {},
                "troncons_hydro_info": ss.get("troncons_hydro_info") or [],
                "surfaces_hydro_info": ss.get("surfaces_hydro_info") or [],
            }
            siren = ss.get("siren") or uf.get("siren") or pm_uf.get("siren")
            denom = ss.get("denomination") or uf.get("denomination") or pm_uf.get("denomination")
            pm: dict[str, Any] = {
                **pm_uf,
                "intersects_pm_database": bool(siren or denom or pm_uf.get("intersects_pm_database")),
                "siren": siren,
                "denomination": denom,
                "forme_juridique": pm_uf.get("forme_juridique"),
            }
            mmap: dict[str, Any] = {
                "parcelles_personnes_morales": pm,
                "filter_enrich": {
                    "veg_libelles": list(parcelle["veg_libelles"] or []),
                    "fauna_distances": dict(parcelle["fauna_distances"] or {}),
                    "zone_humide_ha": parcelle.get("zone_humide_ha"),
                    "dist_hydro_m": parcelle.get("dist_hydro_m"),
                    "troncons_hydro_info": list(parcelle["troncons_hydro_info"] or []),
                    "dist_surface_hydro_m": parcelle.get("dist_surface_hydro_m"),
                    "surface_hydro_ha": parcelle.get("surface_hydro_ha"),
                    "surfaces_hydro_info": list(parcelle["surfaces_hydro_info"] or []),
                },
            }
            if score_eco is not None:
                mmap["score_eco"] = score_eco
            elif score_num is not None:
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
