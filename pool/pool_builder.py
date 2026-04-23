"""
Construction du pool après le filtre géométrique : stratégie « faune-first » en deux tiers.

Tier 1 : parcelles survivantes qui intersectent au moins une observation faune
         (même espèces / filtres géométriques que le filtre principal), triées par
         richesse d'espèces intersectées (distinct) puis distance au centre AOI.

Tier 2 : complément parmi les survivantes hors Tier 1, triées par distance minimale
         aux observations des espèces sélectionnées, puis distance au centre.

Fallback : pas de critères faune exploitables ou table absente → tri distance AOI
         (+ surface), comme l'ancien comportement.

Si le pool ne peut pas être rempli jusqu'à ``target_count``, le reste est complété
par distance au centre (Tier 3 implicite).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import bindparam, text

log = logging.getLogger(__name__)


def _table_exists(conn, full_name: str) -> bool:
    return bool(
        conn.execute(
            text("SELECT to_regclass(:r) IS NOT NULL").execution_options(no_prepare=True),
            {"r": full_name},
        ).scalar_one()
    )


def _geom_sources_sql(crit: dict) -> str:
    selected_sources = crit.get("sources", ["pct", "lin", "surf"])
    if not isinstance(selected_sources, list):
        selected_sources = ["pct", "lin", "surf"]
    selected_sources = [s for s in selected_sources if s in ("pct", "lin", "surf")]
    if not selected_sources:
        selected_sources = ["pct", "lin", "surf"]
    src_clauses: list[str] = []
    if "pct" in selected_sources:
        src_clauses.append("(f.geom_type ILIKE '%POINT%' OR ST_GeometryType(f.geom_2154) ILIKE '%POINT%')")
    if "lin" in selected_sources:
        src_clauses.append("(f.geom_type ILIKE '%LINE%' OR ST_GeometryType(f.geom_2154) ILIKE '%LINE%')")
    if "surf" in selected_sources:
        src_clauses.append("(f.geom_type ILIKE '%POLYGON%' OR ST_GeometryType(f.geom_2154) ILIKE '%POLYGON%')")
    return f"({' OR '.join(src_clauses)})" if src_clauses else "TRUE"


def _cleaned_faune_criteria(faune_criteria: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(faune_criteria, list):
        return out
    for crit in faune_criteria:
        if not isinstance(crit, dict):
            continue
        tax = str(crit.get("tax_nom_val", "") or "").strip()
        mode = str(crit.get("mode", "intersect") or "").strip().lower()
        radius = float(crit.get("radius_m", 500.0) or 0.0)
        if not tax or mode not in ("intersect", "within_radius"):
            continue
        out.append(
            {
                "tax": tax,
                "mode": mode,
                "radius_m": radius,
                "geom_sources_sql": _geom_sources_sql(crit),
            }
        )
    return out


def _assemble_tier1_or_sql(criteria: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    extra: dict[str, Any] = {}
    or_parts: list[str] = []
    for i, c in enumerate(criteria):
        extra[f"faune_tax_{i}"] = c["tax"]
        g = c["geom_sources_sql"]
        or_parts.append(
            f"""
            EXISTS (
                SELECT 1
                FROM ecocompensation_results.fauna f
                WHERE f.project_id = CAST(:project_id AS uuid)
                  AND lower(btrim(f.nom_vernaculaire::text)) = lower(btrim(CAST(:faune_tax_{i} AS text)))
                  AND ({g})
                  AND ST_Intersects(p.geom_2154, f.geom_2154)
            )
            """.strip()
        )
    return "(" + " OR ".join(or_parts) + ")", extra


def _assemble_nb_especes_sql(criteria: list[dict[str, Any]]) -> str:
    or_match: list[str] = []
    for i, c in enumerate(criteria):
        or_match.append(
            f"""
            (
                lower(btrim(f.nom_vernaculaire::text)) = lower(btrim(CAST(:faune_tax_{i} AS text)))
                AND ({c["geom_sources_sql"]})
            )
            """.strip()
        )
    or_sql = "(" + " OR ".join(or_match) + ")"
    return f"""
        (
            SELECT COUNT(DISTINCT lower(btrim(f.nom_vernaculaire::text)))
            FROM ecocompensation_results.fauna f
            WHERE f.project_id = CAST(:project_id AS uuid)
              AND ST_Intersects(p.geom_2154, f.geom_2154)
              AND {or_sql}
        )
        """.strip()


def _assemble_min_dist_faune_sql(criteria: list[dict[str, Any]]) -> str:
    or_match: list[str] = []
    for i, c in enumerate(criteria):
        or_match.append(
            f"""
            (
                lower(btrim(f.nom_vernaculaire::text)) = lower(btrim(CAST(:faune_tax_{i} AS text)))
                AND ({c["geom_sources_sql"]})
            )
            """.strip()
        )
    or_sql = "(" + " OR ".join(or_match) + ")"
    return f"""
        (
            SELECT MIN(ST_Distance(p.geom_2154, f.geom_2154))
            FROM ecocompensation_results.fauna f
            WHERE f.project_id = CAST(:project_id AS uuid)
              AND f.geom_2154 IS NOT NULL
              AND {or_sql}
        )
        """.strip()


def _parcel_dict_from_raw(raw_by_idu: dict[str, dict[str, Any]], idu: str) -> dict[str, Any] | None:
    p = raw_by_idu.get(idu)
    if not p:
        return None
    return {
        "idu": p.get("idu"),
        "code_insee": p.get("code_insee"),
        "section": p.get("section"),
        "numero": p.get("numero"),
        "surface_ha": round(float(p.get("surface_ha") or 0), 2),
        "miller": round(float(p.get("miller") or 0), 4),
        "distance_km": round((p.get("distance_centre_m") or 0) / 1000, 2),
        "dist_hydro_m": p.get("dist_surface_hydro_m"),
    }


def build_faune_first_pool(
    engine,
    project_id: str,
    _aoi_id: str,
    cx: float,
    cy: float,
    parcelles_raw: list[dict[str, Any]],
    options: Any,
    target_count: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Retourne (parcelles_rankées, meta) avec au plus ``target_count`` entrées.
    ``options`` : typiquement ``FiltreOptions`` (attribut ``faune_criteria``).
    """
    meta: dict[str, Any] = {
        "mode": "distance_aoi",
        "tier1_n": 0,
        "tier2_n": 0,
        "tier3_n": 0,
        "target_count": int(target_count),
        "survivors_after_filter": 0,
    }
    raw_by_idu = {str(p.get("idu")): p for p in parcelles_raw if p.get("idu")}
    survivor_idus = list(raw_by_idu.keys())
    if not survivor_idus:
        log.info(
            "POOL_BUILD | project_id=%s survivants_apres_filtre=0 → pool vide",
            project_id,
        )
        return [], meta

    n_survivors = len(survivor_idus)
    meta["survivors_after_filter"] = n_survivors
    log.info(
        "POOL_BUILD | project_id=%s survivants_apres_filtre=%s (toutes les parcelles passant le filtre géométrique) "
        "target_count=%s",
        project_id,
        n_survivors,
        target_count,
    )

    if target_count <= 0:
        ranked = []
        for idu in survivor_idus:
            d = _parcel_dict_from_raw(raw_by_idu, idu)
            if d:
                ranked.append(d)
        ranked.sort(key=lambda x: (x["distance_km"], -x["surface_ha"]))
        meta["mode"] = "all_survivors_distance"
        log.info(
            "POOL_BUILD | project_id=%s mode=%s pool_retenu=%s (= survivants, pas de limite) | "
            "Les profilers traitent autant de parcelles que ce nombre.",
            project_id,
            meta["mode"],
            len(ranked),
        )
        return ranked, meta

    criteria = _cleaned_faune_criteria(getattr(options, "faune_criteria", []) or [])

    with engine.begin() as conn:
        fauna_ok = _table_exists(conn, "ecocompensation_results.fauna")

    use_faune = bool(criteria) and fauna_ok
    if not use_faune:
        ranked = []
        for idu in survivor_idus:
            d = _parcel_dict_from_raw(raw_by_idu, idu)
            if d:
                ranked.append(d)
        ranked.sort(key=lambda x: (x["distance_km"], -x["surface_ha"]))
        ranked = ranked[:target_count]
        meta["mode"] = "distance_aoi"
        meta["reason"] = "no_faune_criteria" if not criteria else "fauna_table_missing"
        log.info(
            "POOL_BUILD | project_id=%s mode=%s raison=%s survivants=%s → pool_retenu=%s (découpe distance AOI) | "
            "Profilage métriques / dureté : uniquement sur ces %s parcelles, pas sur les %s survivants.",
            project_id,
            meta["mode"],
            meta["reason"],
            n_survivors,
            len(ranked),
            len(ranked),
            n_survivors,
        )
        return ranked, meta

    meta["mode"] = "faune_first"
    tier1_sql_extra, params_base = _assemble_tier1_or_sql(criteria)
    nb_especes_sql = _assemble_nb_especes_sql(criteria)
    min_dist_sql = _assemble_min_dist_faune_sql(criteria)

    params_common: dict[str, Any] = {
        "project_id": project_id,
        "cx": cx,
        "cy": cy,
        **params_base,
    }

    stmt_tier1 = text(
        f"""
        SELECT p.idu,
               ({nb_especes_sql}) AS nb_especes,
               ST_Distance(p.geom_2154, ST_SetSRID(ST_MakePoint(:cx, :cy), 2154)) AS distance_centre_m
        FROM ecocompensation_results.parcelles p
        WHERE p.project_id = CAST(:project_id AS uuid)
          AND p.idu IN :survivor_idus
          AND {tier1_sql_extra}
        ORDER BY nb_especes DESC NULLS LAST,
                 distance_centre_m ASC NULLS LAST,
                 p.idu ASC
        """
    ).bindparams(bindparam("survivor_idus", expanding=True))

    with engine.begin() as conn:
        rows1 = conn.execute(
            stmt_tier1,
            {**params_common, "survivor_idus": survivor_idus},
        ).mappings().all()

    tier1_order = [str(r["idu"]) for r in rows1]
    n1 = len(tier1_order)
    meta["tier1_candidates"] = n1
    log.info(
        "POOL_BUILD | project_id=%s faune_first tier1_candidats(intersection_obs)=%s (sur %s survivants)",
        project_id,
        n1,
        n_survivors,
    )

    picked: list[str] = []
    if n1 >= target_count:
        picked = tier1_order[:target_count]
        meta["tier1_n"] = target_count
        meta["tier2_n"] = 0
    else:
        picked = list(tier1_order)
        meta["tier1_n"] = n1
        need2 = target_count - len(picked)
        tier1_idus_list = list(picked)
        params_t2 = {**params_common, "survivor_idus": survivor_idus, "need2": need2}

        stmt_tier2_no_exclude = text(
            f"""
            SELECT p.idu,
                   ({min_dist_sql}) AS dist_faune_m,
                   ST_Distance(p.geom_2154, ST_SetSRID(ST_MakePoint(:cx, :cy), 2154)) AS distance_centre_m
            FROM ecocompensation_results.parcelles p
            WHERE p.project_id = CAST(:project_id AS uuid)
              AND p.idu IN :survivor_idus
            ORDER BY dist_faune_m ASC NULLS LAST,
                     distance_centre_m ASC NULLS LAST,
                     p.idu ASC
            LIMIT :need2
            """
        ).bindparams(bindparam("survivor_idus", expanding=True))

        stmt_tier2_exclude = text(
            f"""
            SELECT p.idu,
                   ({min_dist_sql}) AS dist_faune_m,
                   ST_Distance(p.geom_2154, ST_SetSRID(ST_MakePoint(:cx, :cy), 2154)) AS distance_centre_m
            FROM ecocompensation_results.parcelles p
            WHERE p.project_id = CAST(:project_id AS uuid)
              AND p.idu IN :survivor_idus
              AND p.idu NOT IN :tier1_idus
            ORDER BY dist_faune_m ASC NULLS LAST,
                     distance_centre_m ASC NULLS LAST,
                     p.idu ASC
            LIMIT :need2
            """
        ).bindparams(
            bindparam("survivor_idus", expanding=True),
            bindparam("tier1_idus", expanding=True),
        )

        with engine.begin() as conn:
            if tier1_idus_list:
                rows2 = conn.execute(
                    stmt_tier2_exclude,
                    {**params_t2, "tier1_idus": tier1_idus_list},
                ).mappings().all()
            else:
                rows2 = conn.execute(stmt_tier2_no_exclude, params_t2).mappings().all()

        tier2_order = [str(r["idu"]) for r in rows2]
        meta["tier2_n"] = len(tier2_order)
        picked.extend(tier2_order)
        log.info(
            "POOL_BUILD | project_id=%s tier2_ajoute=%s (proximité faune, complément jusqu'à %s)",
            project_id,
            meta["tier2_n"],
            target_count,
        )

    if len(picked) < target_count:
        before_t3 = len(picked)
        remaining = [i for i in survivor_idus if i not in set(picked)]
        remaining.sort(
            key=lambda idu: (
                (raw_by_idu[idu].get("distance_centre_m") or 0) / 1000.0,
                -float(raw_by_idu[idu].get("surface_ha") or 0),
            )
        )
        for idu in remaining:
            if len(picked) >= target_count:
                break
            picked.append(idu)
        meta["tier3_n"] = max(0, len(picked) - before_t3)
        if meta["tier3_n"]:
            log.info(
                "POOL_BUILD | project_id=%s tier3_ajoute=%s (complément distance AOI)",
                project_id,
                meta["tier3_n"],
            )

    ranked: list[dict[str, Any]] = []
    for idu in picked[:target_count]:
        d = _parcel_dict_from_raw(raw_by_idu, idu)
        if d:
            ranked.append(d)

    log.info(
        "POOL_BUILD | project_id=%s résumé survivants=%s pool_final=%s tier1=%s tier2=%s tier3=%s | "
        "Profilage (dureté, espèces, etc.) : %s parcelles — pas de pré-pool sur les %s survivants.",
        project_id,
        n_survivors,
        len(ranked),
        meta.get("tier1_n"),
        meta.get("tier2_n"),
        meta.get("tier3_n"),
        len(ranked),
        n_survivors,
    )

    return ranked, meta
