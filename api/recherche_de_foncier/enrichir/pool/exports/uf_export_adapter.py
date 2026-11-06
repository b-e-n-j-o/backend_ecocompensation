from __future__ import annotations

from typing import Any

from api.recherche_de_foncier.filtrage.layers.uf_parcelles import summarize_uf_parcelles


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _parse_idu_parts(raw_idu: str) -> tuple[str, str, str]:
    raw = (raw_idu or "").strip()
    if not raw:
        return "", "", ""
    return raw[:5], raw[8:10], raw[-4:]


def _pm_fields(uf: dict[str, Any]) -> dict[str, Any]:
    pm = _as_dict(uf.get("pm_prospect"))
    return {
        "siren": uf.get("siren") or pm.get("siren"),
        "denomination": uf.get("denomination") or pm.get("denomination"),
        "forme_juridique": pm.get("forme_juridique"),
        "deja_compense": bool(pm.get("compensation_deja_realisee")),
    }


def _best_score(summary: dict[str, Any]) -> tuple[float | None, float | None]:
    best = summary.get("best_score_eco") if isinstance(summary.get("best_score_eco"), dict) else {}
    tot = best.get("total_score")
    mx = best.get("max_score")
    return (
        float(tot) if isinstance(tot, (int, float)) else None,
        float(mx) if isinstance(mx, (int, float)) else None,
    )


def iter_uf_summaries(results_uf: dict) -> list[dict[str, Any]]:
    """Une entrée par UF : attributs société + agrégats parcelles (présence / noyau)."""
    out: list[dict[str, Any]] = []
    for uf in results_uf.get("unites_foncieres") or []:
        uid = str(uf.get("uf_id") or "").strip()
        if not uid:
            continue
        if uf.get("parcelles"):
            summary = {
                "parcelles": uf["parcelles"],
                "nb_combinaisons": uf.get("nb_combinaisons") or len(uf.get("sous_ensembles") or []),
                "nb_parcelles_retenues": uf.get("nb_parcelles_retenues") or len(uf["parcelles"]),
                "nb_parcelles_noyau": uf.get("nb_parcelles_noyau") or sum(1 for p in uf["parcelles"] if p.get("noyau")),
                "surface_ha_parcelles": uf.get("surface_ha_parcelles"),
                "best_score_eco": uf.get("best_score_eco"),
                "best_subset_id": uf.get("best_subset_id"),
            }
        else:
            summary = summarize_uf_parcelles(uf, {}, set())
        score_tot, score_max = _best_score(summary)
        pm = _pm_fields(uf)
        out.append({
            "uf_id": uid,
            "uf_rang": uf.get("rang"),
            "siren": pm["siren"],
            "denomination": pm["denomination"],
            "forme_juridique": pm["forme_juridique"],
            "deja_compense": pm["deja_compense"],
            "nb_parcelles": summary["nb_parcelles_retenues"],
            "nb_parcelles_noyau": summary["nb_parcelles_noyau"],
            "nb_combinaisons": summary["nb_combinaisons"],
            "surface_ha": summary["surface_ha_parcelles"],
            "dist_centre_km": uf.get("distance_centre_km"),
            "score_eco": score_tot,
            "score_eco_max": score_max,
            "best_subset_id": summary.get("best_subset_id"),
            "parcelles": summary["parcelles"],
            "sous_ensembles": uf.get("sous_ensembles") or [],
        })
    return out


UF_PARCELLE_CSV_FIELDS = [
    "uf_rang",
    "uf_id",
    "siren",
    "denomination",
    "idu",
    "surface_ha",
    "nb_combinaisons",
    "nb_combinaisons_uf",
    "presence",
    "noyau",
    "in_pool",
    "dist_centre_km",
    "score_eco",
    "score_eco_max",
]


def build_uf_parcelle_attr_rows(results_uf: dict) -> list[dict[str, Any]]:
    """Une ligne par parcelle retenue, triée par UF puis présence (groupable dans QGIS / tableur)."""
    rows: list[dict[str, Any]] = []
    for uf in iter_uf_summaries(results_uf):
        for p in uf["parcelles"]:
            rows.append({
                "uf_rang": uf["uf_rang"],
                "uf_id": uf["uf_id"],
                "siren": uf["siren"],
                "denomination": uf["denomination"],
                "idu": p["idu"],
                "surface_ha": p["surface_ha"],
                "nb_combinaisons": p["nb_combinaisons"],
                "nb_combinaisons_uf": uf["nb_combinaisons"],
                "presence": p["presence"],
                "noyau": bool(p["noyau"]),
                "in_pool": bool(p["in_pool"]),
                "dist_centre_km": uf["dist_centre_km"],
                "score_eco": uf["score_eco"],
                "score_eco_max": uf["score_eco_max"],
            })
    return rows


def _parcelles_idu(uf: dict[str, Any]) -> str:
    refs: list[str] = []
    for p in uf.get("parcelles") or []:
        idu = str((p or {}).get("idu") or "").strip()
        if idu and idu not in refs:
            refs.append(idu)
    return "; ".join(refs)


def uf_layer_attr_row(uf: dict[str, Any]) -> dict[str, Any]:
    return {
        "uf_rang": uf["uf_rang"],
        "uf_id": uf["uf_id"],
        "siren": uf["siren"],
        "denomination": uf["denomination"],
        "forme_juridique": uf["forme_juridique"],
        "deja_compense": bool(uf["deja_compense"]),
        "nb_parcelles": uf["nb_parcelles"],
        "nb_parcelles_noyau": uf["nb_parcelles_noyau"],
        "nb_combinaisons": uf["nb_combinaisons"],
        "surface_ha": uf["surface_ha"],
        "dist_centre_km": uf["dist_centre_km"],
        "score_eco": uf["score_eco"],
        "score_eco_max": uf["score_eco_max"],
        "best_subset_id": uf["best_subset_id"],
        "parcelles": _parcelles_idu(uf),
    }


def subset_layer_attr_row(uf: dict[str, Any], ss: dict[str, Any]) -> dict[str, Any] | None:
    sid = str(ss.get("subset_id") or "").strip()
    if not sid:
        return None
    sc = ss.get("score_eco") if isinstance(ss.get("score_eco"), dict) else {}
    idus = ss.get("idus") or []
    return {
        "subset_id": sid,
        "uf_id": uf["uf_id"],
        "uf_rang": uf["uf_rang"],
        "siren": uf["siren"],
        "denomination": uf["denomination"],
        "nb_parcelles": len(idus) or ss.get("k"),
        "surface_ha": ss.get("surface_ha"),
        "miller": ss.get("miller"),
        "dist_centre_km": ss.get("distance_centre_km"),
        "score_eco": sc.get("total_score"),
        "score_eco_max": sc.get("max_score"),
        "idus": " ".join(str(x) for x in idus),
    }


def build_subset_export_inputs(results_uf: dict) -> list[dict[str, Any]]:
    """
    Normalise les sous-ensembles UF vers un format compatible export parcelles (legacy).
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
            out.append({"subset_id": subset_id, "parcelle": parcelle, "mmap": mmap})
            rank += 1
    return out
