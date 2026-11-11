"""
pool.py — filtre les îlots enrichis (colonnes, pas de jointure spatiale) et construit le JSON de réponse.

Mêmes seuils que les terrains de personnes morales :
  - miller >= miller_thresh
  - mode faune : veg_libelles && cesbio_libelles (si fournis), distance 1re espèce <= dist_m
  - mode ZH    : zone_humide_ha = somme sur les parcelles de l'îlot présentes dans le pool parcelles
Puis score éco et prospect PM (pm_dominant = SIREN).
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from .params import RES_TABLE, IlotsRpgParams

log_ = logging.getLogger(__name__)


def select_ilots(engine, project_id: str, p: IlotsRpgParams) -> list[dict]:
    """Îlots qui passent les seuils, triés (ZH : surface humide ; sinon surface)."""
    is_zh = p.study_type == "zones_humides_intra"
    params: dict = {"pid": project_id, "miller": p.miller_thresh, "limit": p.limit,
                    "cesbio": p.cesbio_libelles, "min_zh": p.min_zone_humide_ha}
    where = ["il.project_id = CAST(:pid AS uuid)", "COALESCE(il.miller, 0) >= :miller"]
    fauna_col = "NULL::int"
    if p.fauna_criteria:
        params["species"] = p.fauna_criteria[0]["species"]
        params["dist_m"] = int(float(p.fauna_criteria[0].get("dist_m") or 0))
        fauna_col = "(il.fauna_distances->>:species)::int"
        where.append("(il.fauna_distances->>:species)::int BETWEEN 0 AND :dist_m")
    if not is_zh and p.cesbio_libelles:
        where.append("il.veg_libelles && CAST(:cesbio AS text[])")
    if is_zh and p.min_zone_humide_ha > 0:
        where.append("COALESCE(zh.zone_humide_ha, 0) >= :min_zh")
    order = "zh.zone_humide_ha DESC NULLS LAST, il.surface_ha DESC" if is_zh else "il.surface_ha DESC"

    sql = f"""
        SELECT il.num_ilot, il.idus, il.nb_parcelles,
               ROUND(il.surface_ha::numeric, 2)               AS surface_ha,
               ROUND(il.miller::numeric, 3)                   AS miller,
               ROUND((il.dist_centre_m / 1000.0)::numeric, 3) AS distance_centre_km,
               il.cat_dominante, il.code_group_dominant, il.code_cultu_dominant,
               il.part_pp, il.part_jachere, il.part_vigne,
               il.nb_pm, il.pm_dominant, il.pm_unique,
               il.veg_libelles, il.veg_profile, il.fauna_distances,
               ROUND(COALESCE(zh.zone_humide_ha, 0)::numeric, 4) AS zone_humide_ha,
               {fauna_col} AS dist_fauna_m
        FROM {RES_TABLE} il
        LEFT JOIN LATERAL (
            SELECT SUM(pa.zone_humide_ha) AS zone_humide_ha
            FROM ecocompensation_results.parcelles pa
            WHERE pa.project_id = il.project_id AND pa.idu = ANY(il.idus)
        ) zh ON {"TRUE" if is_zh else "FALSE"}
        WHERE {" AND ".join(where)}
        ORDER BY {order}
        LIMIT :limit
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()

    out = []
    for r in rows:
        out.append({
            "num_ilot": r["num_ilot"],
            "idus": list(r["idus"] or []),
            "nb_parcelles": int(r["nb_parcelles"] or 0),
            "surface_ha": float(r["surface_ha"] or 0),
            "miller": float(r["miller"] or 0),
            "distance_centre_km": float(r["distance_centre_km"] or 0),
            "cat_dominante": r["cat_dominante"],
            "code_group_dominant": r["code_group_dominant"],
            "code_cultu_dominant": r["code_cultu_dominant"],
            "part_pp": float(r["part_pp"] or 0),
            "part_jachere": float(r["part_jachere"] or 0),
            "part_vigne": float(r["part_vigne"] or 0),
            "nb_pm": int(r["nb_pm"] or 0),
            "pm_dominant": r["pm_dominant"],
            "pm_unique": bool(r["pm_unique"]),
            "veg_libelles": list(r["veg_libelles"] or []),
            "veg_profile": dict(r["veg_profile"] or {}),
            "fauna_distances": dict(r["fauna_distances"] or {}),
            "dist_fauna_m": r["dist_fauna_m"],
            "zone_humide_ha": float(r["zone_humide_ha"] or 0),
        })
    return out


def attach_profiling(engine, ilots: list[dict], p: IlotsRpgParams) -> list[dict]:
    """Score éco + prospect PM, puis classement par (score, surface)."""
    from api.recherche_de_foncier.filtrage.layers.unites_foncieres.pool import (
        _empty_pm, compute_score_eco, lookup_prospects_by_siren)

    sirens = [il["pm_dominant"] for il in ilots if il.get("pm_dominant")]
    try:
        with engine.begin() as conn:
            prospects = lookup_prospects_by_siren(conn, sirens)
    except Exception:
        log_.exception("[ILOTS_RPG] lookup prospects PM échoué")
        prospects = {}

    for il in ilots:
        il["score_eco"] = compute_score_eco(
            il["distance_centre_km"], il["fauna_distances"], p.fauna_criteria,
            zone_humide_ha=il["zone_humide_ha"], study_type=p.study_type)
        siren = il.get("pm_dominant")
        il["pm_prospect"] = prospects.get(siren, _empty_pm(siren, None)) if siren else None
    return ilots


def rank(ilots: list[dict]) -> list[dict]:
    ranked = sorted(ilots, key=lambda il: (int((il.get("score_eco") or {}).get("total_score") or 0),
                                           il["surface_ha"]), reverse=True)
    for i, il in enumerate(ranked, 1):
        il["rang"] = i
    return ranked


def build_response(engine, project_id: str, p: IlotsRpgParams, *, profiling: bool = True) -> dict:
    ilots = select_ilots(engine, project_id, p)
    if profiling and ilots:
        ilots = attach_profiling(engine, ilots, p)
    ilots = rank(ilots)
    with engine.connect() as conn:
        n_candidats = conn.execute(
            text(f"SELECT count(*) FROM {RES_TABLE} WHERE project_id = CAST(:pid AS uuid)"),
            {"pid": project_id}).scalar_one()
    log_.info("[ILOTS_RPG] pool : %d / %d îlots passent les seuils", len(ilots), n_candidats)
    return {
        "source": "rpg_ilots",
        "millesime": p.millesime,
        "total_candidats": int(n_candidats),
        "total_ilots": len(ilots),
        "ilots": ilots,
    }
