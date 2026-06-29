#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Profilage UF : score_eco par sous-ensemble + prospect compensation par SIREN (une fois par PM).
"""
from __future__ import annotations

import os

from sqlalchemy import bindparam, text

from pool.profilers.personnes_morales import (
    DEFAULT_PROSPECTS_TABLE,
    _prospects_compensation_payload,
)
from pool.profilers.score_eco import ScoreEcoProfiler

ECO_MAX = 6


def _fauna_criteria_from_config(
    fauna_criteria: list[dict] | None,
    fauna_species: str | None,
    fauna_dist_m: float,
) -> list[dict]:
    if fauna_criteria:
        out: list[dict] = []
        for c in fauna_criteria:
            if not isinstance(c, dict):
                continue
            species = str(c.get("species") or c.get("tax_nom_val") or "").strip()
            if not species:
                continue
            try:
                dist_m = float(c.get("dist_m") or 0)
            except (TypeError, ValueError):
                dist_m = 0.0
            out.append({"species": species, "dist_m": dist_m})
        return out
    sp = (fauna_species or "").strip()
    if not sp:
        return []
    return [{"species": sp, "dist_m": float(fauna_dist_m)}]


def _distance_score(distance_km: float) -> tuple[int, str]:
    if distance_km < 3:
        return 3, "<3km"
    if distance_km <= 7:
        return 2, "3-7km"
    if distance_km <= 10:
        return 1, "7-10km"
    return 0, ">10km"


def _zone_humide_points(zone_humide_ha: float) -> tuple[int, str]:
    zh = float(zone_humide_ha or 0)
    if zh >= 2.0:
        return 3, ">=2ha"
    if zh >= 1.0:
        return 2, "1-2ha"
    if zh > 0:
        return 1, "<1ha"
    return 0, "none"


def compute_subset_score_eco(
    distance_centre_km: float,
    fauna_distances: dict,
    fauna_criteria: list[dict],
    *,
    zone_humide_ha: float = 0.0,
    study_type: str = "faune_buffer",
) -> dict:
    """Score éco 0..6 pour un sous-ensemble."""
    if study_type == "zones_humides_intra":
        zh_points, zh_reason = _zone_humide_points(zone_humide_ha)
        if fauna_criteria:
            species_points, species_reason, species_extra = ScoreEcoProfiler._species_points_from_filter_enrich(
                fauna_distances or {},
                fauna_criteria,
            )
            # Cap faune à 3 pts en mode ZH (complémentaire à la surface humide)
            species_points = min(3, species_points)
        else:
            species_points, species_reason, species_extra = 0, "no_faune_criteria", {}
        total = int(zh_points + species_points)
        return {
            "total_score": total,
            "max_score": ECO_MAX,
            "breakdown": {
                "zone_humide": {
                    "points": zh_points,
                    "zone_humide_ha": round(float(zone_humide_ha or 0), 4),
                    "reason": zh_reason,
                },
                "especes": {
                    "points": species_points,
                    "reason": species_reason,
                    **species_extra,
                },
            },
        }

    distance_points, distance_bucket = _distance_score(float(distance_centre_km or 0))
    if fauna_criteria:
        species_points, species_reason, species_extra = ScoreEcoProfiler._species_points_from_filter_enrich(
            fauna_distances or {},
            fauna_criteria,
        )
    else:
        species_points, species_reason, species_extra = 0, "no_faune_criteria", {}

    total = int(species_points + distance_points)
    return {
        "total_score": total,
        "max_score": ECO_MAX,
        "breakdown": {
            "especes": {
                "points": species_points,
                "reason": species_reason,
                **species_extra,
            },
            "distance": {
                "points": distance_points,
                "distance_km": round(float(distance_centre_km or 0), 3),
                "bucket": distance_bucket,
            },
        },
    }


def lookup_prospects_by_siren(conn, sirens: list[str]) -> dict[str, dict]:
    """
    Prospect compensation agrégé par SIREN (une lecture pour toute l'UF).
    Si au moins une parcelle du SIREN figure dans parcelles_prospects_filtered → prospect.
    """
    clean = sorted({s.strip() for s in sirens if s and str(s).strip()})
    if not clean:
        return {}

    prospects_table = os.getenv("PARCELLES_PROSPECTS_TABLE", DEFAULT_PROSPECTS_TABLE)
    out: dict[str, dict] = {}
    sql = (
        text(
            f"""
            SELECT
                siren,
                bool_or(parcelle_deja_en_mc) AS parcelle_deja_en_mc,
                MAX(nb_mc_distinctes)         AS nb_mc_distinctes,
                MAX(nb_parcelles_deja_en_mc)  AS nb_parcelles_deja_en_mc,
                MAX(surface_deja_en_mc_m2)    AS surface_deja_en_mc_m2
            FROM {prospects_table}
            WHERE siren IN :sirens
            GROUP BY siren
            """
        )
        .bindparams(bindparam("sirens", expanding=True))
    )
    chunk = 500
    for i in range(0, len(clean), chunk):
        part = clean[i : i + chunk]
        rows = conn.execute(sql, {"sirens": part}).mappings().all()
        for r in rows:
            siren = str(r["siren"] or "").strip()
            if not siren:
                continue
            payload = _prospects_compensation_payload(
                r.get("parcelle_deja_en_mc"),
                r.get("nb_mc_distinctes"),
                r.get("nb_parcelles_deja_en_mc"),
                r.get("surface_deja_en_mc_m2"),
            )
            payload["intersects_pm_database"] = True
            payload["siren"] = siren
            out[siren] = payload
    return out


def _empty_pm_payload(siren: str | None = None, denomination: str | None = None) -> dict:
    return {
        "intersects_pm_database": bool(siren or denomination),
        "siren": siren,
        "denomination": denomination,
        "forme_juridique": None,
        "compensation_deja_realisee": False,
        "parcelle_deja_en_mc": None,
        "nb_mc_distinctes": None,
        "nb_parcelles_deja_en_mc": None,
        "surface_deja_en_mc_m2": None,
    }


def attach_uf_profiling(
    engine,
    pool_result: dict,
    *,
    fauna_criteria: list[dict] | None = None,
    fauna_species: str | None = None,
    fauna_dist_m: float = 1000.0,
    study_type: str = "faune_buffer",
) -> dict:
    """
    Enrichit le dict retourné par build_uf_pool_response :
      - score_eco sur chaque sous-ensemble ;
      - pm_prospect une fois par UF (clé SIREN).
    """
    criteria = _fauna_criteria_from_config(fauna_criteria, fauna_species, fauna_dist_m)
    unites = pool_result.get("unites_foncieres") or []
    if not unites:
        return pool_result

    sirens: list[str] = []
    for uf in unites:
        s = str(uf.get("siren") or "").strip()
        if s:
            sirens.append(s)

    prospects_by_siren: dict[str, dict] = {}
    try:
        with engine.begin() as conn:
            prospects_by_siren = lookup_prospects_by_siren(conn, sirens)
    except Exception:
        prospects_by_siren = {}

    for uf in unites:
        siren = str(uf.get("siren") or "").strip() or None
        denom = uf.get("denomination")
        pm = prospects_by_siren.get(siren or "", _empty_pm_payload(siren, denom))
        if siren and pm.get("siren") != siren:
            pm = {**pm, "siren": siren}
        if denom and not pm.get("denomination"):
            pm = {**pm, "denomination": denom}
        uf["pm_prospect"] = pm

        ss_list = uf.get("sous_ensembles") or []
        for ss in ss_list:
            fd = ss.get("fauna_distances") or {}
            if not isinstance(fd, dict):
                fd = dict(fd) if fd else {}
            ss["score_eco"] = compute_subset_score_eco(
                float(ss.get("distance_centre_km") or 0),
                fd,
                criteria,
                zone_humide_ha=float(ss.get("zone_humide_ha") or 0),
                study_type=study_type,
            )

    # Re-classement UF par meilleur score_eco puis surface
    def _best_score(uf: dict) -> tuple[int, float]:
        ss_list = uf.get("sous_ensembles") or []
        if not ss_list:
            return (0, 0.0)
        best = max(
            ss_list,
            key=lambda s: (
                int((s.get("score_eco") or {}).get("total_score") or 0),
                float(s.get("surface_ha") or 0),
            ),
        )
        return (
            int((best.get("score_eco") or {}).get("total_score") or 0),
            float(best.get("surface_ha") or 0),
        )

    ranked = sorted(unites, key=lambda u: (_best_score(u)[0], _best_score(u)[1]), reverse=True)
    for rang, uf in enumerate(ranked, 1):
        uf["rang"] = rang

    pool_result["unites_foncieres"] = ranked
    return pool_result


def build_uf_pool_with_profiling(
    engine,
    project_id: str,
    cx: float,
    cy: float,
    *,
    cesbio_libelles: list[str],
    fauna_species: str | None = None,
    fauna_dist_m: float = 1000.0,
    fauna_criteria: list[dict] | None = None,
    miller_thresh: float = 0.39,
    limit_uf: int = 50,
    study_type: str = "faune_buffer",
    min_zone_humide_ha: float = 0.0,
) -> dict:
    from layers.enrich_uf import build_uf_pool_response

    raw = build_uf_pool_response(
        engine,
        project_id,
        cx,
        cy,
        cesbio_libelles=cesbio_libelles,
        fauna_species=fauna_species,
        fauna_dist_m=fauna_dist_m,
        fauna_criteria=fauna_criteria,
        miller_thresh=miller_thresh,
        limit_uf=limit_uf,
        study_type=study_type,
        min_zone_humide_ha=min_zone_humide_ha,
    )
    return attach_uf_profiling(
        engine,
        raw,
        fauna_criteria=fauna_criteria,
        fauna_species=fauna_species,
        fauna_dist_m=fauna_dist_m,
        study_type=study_type,
    )
