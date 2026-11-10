#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
enrich_uf.py
============

Enrichit ecocompensation_results.sous_ensembles avec :
  - veg_libelles    text[]  : libellés CESBIO intersectant l'union de parcelles
  - fauna_distances jsonb   : { "nom_vernaculaire": dist_m }

Colonne clé = subset_id, géom = geom_2154.
"""
from __future__ import annotations
import json
import math
import time
from collections import defaultdict
from typing import Callable
from sqlalchemy import text

from api.recherche_de_foncier.filtrage.layers.cesbio_tables import VEG_TABLE_CALC
from api.recherche_de_foncier.filtrage.layers.faune import (
    aliases_only,
    chain_sql,
    criterion_names,
    fauna_exists_sql,
    nearest_fauna_distance_sql,
)

VEG_BATCH_SIZE   = 100
UF_VEG_BATCH_SIZE = 1
FAUNA_BATCH_SIZE = 300
STMT_TIMEOUT     = "90s"

# ── DDL ───────────────────────────────────────────────────────────────────────
_ALTER_DDL = """
ALTER TABLE ecocompensation_results.sous_ensembles
    ADD COLUMN IF NOT EXISTS veg_libelles    text[]  NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS veg_profile     jsonb   NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS fauna_distances jsonb   NOT NULL DEFAULT '{}';

ALTER TABLE ecocompensation_results.unites_foncieres
    ADD COLUMN IF NOT EXISTS veg_libelles text[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS veg_profile  jsonb  NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_ss_veg
    ON ecocompensation_results.sous_ensembles USING GIN (veg_libelles);
CREATE INDEX IF NOT EXISTS idx_ss_fauna
    ON ecocompensation_results.sous_ensembles USING GIN (fauna_distances);
"""

def _parse_profile(raw) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return dict(raw) if isinstance(raw, dict) else {}


def _attach_uf_veg(engine, project_id: str, unites: list[dict]) -> None:
    """Occupation du sol stockée une fois par unité foncière."""
    ids = [str(u.get("uf_id") or "") for u in unites if u.get("uf_id")]
    if not ids:
        return
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT DISTINCT ON (uf_id) uf_id, veg_libelles, veg_profile
                FROM ecocompensation_results.unites_foncieres
                WHERE project_id = CAST(:pid AS uuid)
                  AND uf_id = ANY(CAST(:ids AS text[]))
                ORDER BY uf_id
            """),
            {"pid": project_id, "ids": ids},
        ).mappings().all()
    by_id = {str(r["uf_id"]): r for r in rows}
    for uf in unites:
        row = by_id.get(str(uf.get("uf_id") or ""))
        if not row:
            continue
        profile = _parse_profile(row["veg_profile"])
        if profile:
            uf["veg_profile"] = profile
        if row["veg_libelles"]:
            uf["veg_libelles"] = list(row["veg_libelles"])


def attach_stored_veg_profiles(engine, project_id: str, results: dict | None) -> dict | None:
    """Complète l'occupation du sol de l'unité foncière si l'instantané ne l'a pas."""
    if not isinstance(results, dict):
        return results
    unites = results.get("unites_foncieres") or []
    _attach_uf_veg(engine, project_id, unites)
    return results


def ensure_columns(engine) -> None:
    """Migration one-shot. Appeler au démarrage de l'app avec filter_pipeline.ensure_columns."""
    with engine.begin() as conn:
        conn.execute(text(_ALTER_DDL))

# ── SQL enrichissement ────────────────────────────────────────────────────────

_SQL_VEG_BATCH = f"""
WITH parc AS (
    SELECT DISTINCT ON (uf.uf_id, uf.idu)
        uf.uf_id, uf.idu, uf.geom_2154
    FROM ecocompensation_results.unites_foncieres uf
    WHERE uf.project_id = :pid
      AND uf.uf_id = ANY(CAST(:batch AS text[]))
    ORDER BY uf.uf_id, uf.idu
),
parc_veg AS (
    SELECT
        parc.uf_id,
        parc.idu,
        v.libelle_prio,
        SUM(
            ST_Area(
                ST_Intersection(
                    parc.geom_2154,
                    ST_ClipByBox2D(v.geom, parc.geom_2154)
                )
            )
        ) AS area_m2,
        (array_agg(DISTINCT v.source) FILTER (WHERE v.source IS NOT NULL))[1] AS source
    FROM parc
    JOIN {VEG_TABLE_CALC} v
        ON parc.geom_2154 && v.geom
       AND ST_Intersects(parc.geom_2154, v.geom)
    WHERE v.libelle_prio IS NOT NULL
    GROUP BY parc.uf_id, parc.idu, v.libelle_prio
),
class_areas AS (
    SELECT
        pv.uf_id,
        pv.libelle_prio,
        SUM(pv.area_m2) AS area_m2,
        (array_agg(DISTINCT pv.source) FILTER (WHERE pv.source IS NOT NULL))[1] AS source
    FROM parc_veg pv
    GROUP BY pv.uf_id, pv.libelle_prio
    HAVING SUM(pv.area_m2) > 0
),
geom_areas AS (
    SELECT uf_id, SUM(ST_Area(geom_2154)) AS area_m2
    FROM parc
    GROUP BY uf_id
),
veg_agg AS (
    SELECT
        ca.uf_id,
        array_agg(ca.libelle_prio ORDER BY ca.libelle_prio) AS veg_libelles,
        jsonb_object_agg(
            ca.libelle_prio,
            jsonb_build_object(
                'area_m2', ROUND(ca.area_m2::numeric, 1),
                'pct', ROUND((ca.area_m2 / NULLIF(ga.area_m2, 0) * 100)::numeric, 2),
                'source', ca.source
            )
        ) AS veg_profile
    FROM class_areas ca
    JOIN geom_areas ga ON ga.uf_id = ca.uf_id
    GROUP BY ca.uf_id
)
UPDATE ecocompensation_results.unites_foncieres uf
SET    veg_libelles = COALESCE(va.veg_libelles, '{{}}'),
       veg_profile  = COALESCE(va.veg_profile, '{{}}')
FROM   veg_agg va
WHERE  uf.project_id = :pid
  AND  uf.uf_id = va.uf_id
"""

_SQL_FAUNA_BATCH = f"""
WITH fauna_agg AS (
    SELECT
        ss.subset_id,
        COALESCE({nearest_fauna_distance_sql("ss.geom_2154")}, -1) AS dist_m
    FROM ecocompensation_results.sous_ensembles ss
    WHERE ss.project_id = :pid
      AND ss.subset_id  = ANY(CAST(:batch AS text[]))
)
UPDATE ecocompensation_results.sous_ensembles ss
SET    fauna_distances = fauna_distances || jsonb_build_object(CAST(:species AS text), fa.dist_m)
FROM   fauna_agg fa
WHERE  ss.project_id = :pid
  AND  ss.subset_id  = fa.subset_id
"""

# ── run() ─────────────────────────────────────────────────────────────────────

def _subset_ids_passing_cheap_filters(
    engine,
    project_id: str,
    *,
    miller_thresh: float | None,
    cesbio_libelles: list[str] | None,
    fauna_criteria: list[dict] | None,
    fauna_combine: str,
    fauna_joins: list[str] | None,
    log: Callable[[str], None],
) -> list[str]:
    """Miller, distance faune, puis intersection CESBIO sans calcul d'aire.

    Même ordre que le filtre parcelles : la surface est déjà appliquée à la
    construction des sous-ensembles. ST_Area(ST_Intersection) ne porte ensuite
    que sur les survivants.
    """
    params: dict = {"pid": project_id}
    where = ["ss.project_id = :pid"]
    if miller_thresh is not None:
        where.append("ss.miller >= :miller_th")
        params["miller_th"] = float(miller_thresh)

    with engine.begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = '3min'"))
        ids = [
            row[0] for row in conn.execute(
                text(
                    "SELECT ss.subset_id FROM ecocompensation_results.sous_ensembles ss "
                    f"WHERE {' AND '.join(where)} ORDER BY ss.subset_id"
                ),
                params,
            ).fetchall()
        ]
    log(f"[ENRICH_UF] Après Miller : {len(ids):,} sous-ensembles.")
    if not ids:
        return []

    criteria = [c for c in (fauna_criteria or []) if str(c.get("species") or "").strip()]
    if criteria:
        parts = []
        fauna_params: dict = {"pid": project_id, "batch": ids}
        for i, c in enumerate(criteria):
            parts.append(f"({fauna_exists_sql('ss.geom_2154', i)})")
            fauna_params[f"fauna_species_{i}"] = str(c["species"]).strip()
            fauna_params[f"fauna_aliases_{i}"] = aliases_only([str(a) for a in (c.get("aliases") or [])])
            fauna_params[f"fauna_dist_m_{i}"] = float(c.get("dist_m") or 0)
        expr = chain_sql(parts, fauna_joins, fauna_combine)
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL statement_timeout = '3min'"))
            ids = [
                row[0] for row in conn.execute(
                    text(f"""
                        SELECT ss.subset_id
                        FROM ecocompensation_results.sous_ensembles ss
                        WHERE ss.project_id = :pid
                          AND ss.subset_id = ANY(CAST(:batch AS text[]))
                          AND ({expr})
                        ORDER BY ss.subset_id
                    """),
                    fauna_params,
                ).fetchall()
            ]
        log(f"[ENRICH_UF] Après faune : {len(ids):,} sous-ensembles.")
        if not ids:
            return []

    labels = [str(x).strip() for x in (cesbio_libelles or []) if str(x).strip()]
    if labels:
        kept: list[str] = []
        for start in range(0, len(ids), VEG_BATCH_SIZE):
            batch = ids[start:start + VEG_BATCH_SIZE]
            with engine.begin() as conn:
                conn.execute(text(f"SET LOCAL statement_timeout = '{STMT_TIMEOUT}'"))
                kept.extend(
                    row[0] for row in conn.execute(
                        text(f"""
                            SELECT ss.subset_id
                            FROM ecocompensation_results.sous_ensembles ss
                            WHERE ss.project_id = :pid
                              AND ss.subset_id = ANY(CAST(:batch AS text[]))
                              AND EXISTS (
                                  SELECT 1
                                  FROM {VEG_TABLE_CALC} v
                                  WHERE v.libelle_prio = ANY(CAST(:labels AS text[]))
                                    AND ss.geom_2154 && v.geom
                                    AND ST_Intersects(ss.geom_2154, v.geom)
                              )
                            ORDER BY ss.subset_id
                        """),
                        {"pid": project_id, "batch": batch, "labels": labels},
                    ).fetchall()
                )
        ids = kept
        log(f"[ENRICH_UF] Après CESBIO (intersection, sans aire) : {len(ids):,} sous-ensembles.")
    return ids


def run(
    engine,
    project_id:   str,
    aoi_id:       str,
    cb:           Callable[[str], None] | None = None,
    *,
    species_list: list[str] | None = None,
    miller_thresh: float | None = None,
    cesbio_libelles: list[str] | None = None,
    fauna_criteria: list[dict] | None = None,
    fauna_combine: str = "or",
    fauna_joins: list[str] | None = None,
) -> int:
    """
    Enrichit tous les sous-ensembles du projet avec veg_libelles + fauna_distances.
    Signature compatible layer_runner. Retourne le nombre de sous-ensembles traités.
    """
    log = cb or (lambda m: None)
    ensure_columns(engine)
    species = species_list or []
    name_by_species = {
        str(c.get("species") or "").strip(): criterion_names(
            str(c.get("species") or "").strip(),
            [str(a) for a in (c.get("aliases") or [])],
        )
        for c in (fauna_criteria or [])
        if str(c.get("species") or "").strip()
    }

    with engine.begin() as conn:
        n = conn.execute(
            text("SELECT COUNT(*) FROM ecocompensation_results.sous_ensembles WHERE project_id = :pid"),
            {"pid": project_id},
        ).scalar_one()

    if n == 0:
        log("[ENRICH_UF] Aucun sous-ensemble — lancer d'abord sous_ensembles.")
        return 0

    log(f"[ENRICH_UF] {n:,} sous-ensembles en base (surface déjà filtrée).")

    use_cheap = (
        miller_thresh is not None
        or bool(cesbio_libelles)
        or bool(fauna_criteria)
    )
    if use_cheap:
        subset_ids = _subset_ids_passing_cheap_filters(
            engine,
            project_id,
            miller_thresh=miller_thresh,
            cesbio_libelles=cesbio_libelles,
            fauna_criteria=fauna_criteria,
            fauna_combine=fauna_combine,
            fauna_joins=fauna_joins,
            log=log,
        )
    else:
        with engine.begin() as conn:
            subset_ids = [
                row[0] for row in conn.execute(
                    text("SELECT subset_id FROM ecocompensation_results.sous_ensembles "
                         "WHERE project_id = :pid ORDER BY subset_id"),
                    {"pid": project_id},
                ).fetchall()
            ]

    if not subset_ids:
        log("[ENRICH_UF] Aucun sous-ensemble après filtres légers — pas d'intersection d'aires.")
        return 0

    with engine.begin() as conn:
        uf_ids = [
            row[0] for row in conn.execute(
                text("""
                    SELECT DISTINCT uf_id
                    FROM ecocompensation_results.sous_ensembles
                    WHERE project_id = :pid
                      AND subset_id = ANY(CAST(:batch AS text[]))
                    ORDER BY uf_id
                """),
                {"pid": project_id, "batch": subset_ids},
            ).fetchall()
        ]
    n_veg = math.ceil(len(uf_ids) / UF_VEG_BATCH_SIZE) or 0
    log(f"[ENRICH_UF] 🌿 Vég : {len(uf_ids)} unités foncières, lots de {UF_VEG_BATCH_SIZE}")
    for bi, start in enumerate(range(0, len(uf_ids), UF_VEG_BATCH_SIZE), 1):
        batch = uf_ids[start:start + UF_VEG_BATCH_SIZE]
        t0 = time.perf_counter()
        with engine.begin() as conn:
            conn.execute(text(f"SET LOCAL statement_timeout = '{STMT_TIMEOUT}'"))
            conn.execute(text(_SQL_VEG_BATCH), {"pid": project_id, "batch": batch})
        dt = time.perf_counter() - t0
        log(f"[ENRICH_UF] 🌿 Vég lot {bi}/{n_veg} : {len(batch)} UF, {dt:.1f}s")
    log("[ENRICH_UF] ✓ occupation du sol écrite sur les unités foncières.")

    # Faune
    if species:
        n_fauna = math.ceil(len(subset_ids) / FAUNA_BATCH_SIZE)
        for sp in species:
            log(f"[ENRICH_UF] 🦎 Fauna '{sp}' : {n_fauna} batch(s)")
            for bi, start in enumerate(range(0, len(subset_ids), FAUNA_BATCH_SIZE), 1):
                batch = subset_ids[start:start + FAUNA_BATCH_SIZE]
                with engine.begin() as conn:
                    conn.execute(text(f"SET LOCAL statement_timeout = '{STMT_TIMEOUT}'"))
                    conn.execute(text(_SQL_FAUNA_BATCH),
                                 {"pid": project_id, "batch": batch, "species": sp, "names": name_by_species.get(sp, [sp])})
                log(f"TILE_PROGRESS:{bi}/{n_fauna}:{bi*FAUNA_BATCH_SIZE} 🦎 {sp} {bi}/{n_fauna}")
            log(f"[ENRICH_UF] ✓ fauna '{sp}' mis à jour.")
    else:
        log("[ENRICH_UF] Aucune espèce — fauna_distances ignorée.")

    return len(subset_ids)


# ── build_uf_pool_response() ──────────────────────────────────────────────────

def build_uf_pool_response(
    engine,
    project_id: str,
    cx: float,
    cy: float,
    *,
    cesbio_libelles: list[str],
    fauna_species: str | None = None,
    fauna_dist_m: float,
    fauna_criteria: list[dict] | None = None,
    miller_thresh: float = 0.39,
    limit_uf: int = 50,
    study_type: str = "faune_buffer",
    min_zone_humide_ha: float = 0.0,
) -> dict:
    """
    Retourne les résultats UF filtrés et groupés par uf_id.

    Format retourné (compatible UnitesFoncieresTable.tsx) :
    {
        "total_uf": N,
        "total_sous_ensembles": M,
        "unites_foncieres": [
            {
                "rang": 1,
                "uf_id": "...",
                "siren": "...",
                "denomination": "...",
                "nb_parcelles": 5,
                "distance_centre_km": 7.0,
                "sous_ensembles": [...]
            }
        ]
    }

    Filtres appliqués (sur colonnes enrichies — aucun spatial join) :
      - miller >= miller_thresh (colonne précalculée)
      - veg_libelles && cesbio_libelles  (GIN)
      - veg_libelles && cesbio_libelles  (GIN)
      - fauna_distances (jsonb) si ``fauna_species`` fourni
    """
    params = {
        "project_id":      project_id,
        "miller_th":       miller_thresh,
        "cesbio_libelles": cesbio_libelles,
        "fauna_dist_m":    int(fauna_dist_m),
        "cx":              cx,
        "cy":              cy,
        "min_zone_humide_ha": float(min_zone_humide_ha or 0),
    }
    fauna_sql = ""
    if fauna_species:
        params["fauna_species"] = fauna_species
        fauna_sql = """
              AND (ss.fauna_distances->>:fauna_species) IS NOT NULL
              AND (ss.fauna_distances->>:fauna_species)::int BETWEEN 0 AND :fauna_dist_m"""

    is_zh = study_type == "zones_humides_intra"
    cesbio_sql = ""
    if not is_zh and cesbio_libelles:
        cesbio_sql = """
              AND EXISTS (
                  SELECT 1
                  FROM ecocompensation.vegetation_sur_cesbio v
                  WHERE v.libelle_prio = ANY(CAST(:cesbio_libelles AS text[]))
                    AND ss.geom_2154 && v.geom
                    AND ST_Intersects(ss.geom_2154, v.geom)
              )"""

    if is_zh:
        sql = f"""
            SELECT
                ss.subset_id,
                ss.uf_id,
                ss.k,
                ss.idus,
                ss.siren,
                ss.denomination,
                ROUND(ss.surface_ha::numeric, 2)              AS surface_ha,
                ROUND(ss.miller::numeric, 3)                  AS miller,
                ROUND((ss.dist_centre_m / 1000.0)::numeric, 3) AS distance_centre_km,
                ss.veg_libelles,
                ss.veg_profile,
                ss.fauna_distances,
                ROUND(COALESCE(zh.zone_humide_ha, 0)::numeric, 4) AS zone_humide_ha,
                {"(ss.fauna_distances->>:fauna_species)::int" if fauna_species else "NULL::int"} AS dist_fauna_m
            FROM ecocompensation_results.sous_ensembles ss
            LEFT JOIN LATERAL (
                SELECT SUM(p.zone_humide_ha) AS zone_humide_ha
                FROM ecocompensation_results.parcelles p
                WHERE p.project_id = ss.project_id
                  AND p.idu = ANY(ss.idus)
            ) zh ON TRUE
            WHERE ss.project_id = :project_id
              AND ss.miller >= :miller_th{fauna_sql}
            ORDER BY zh.zone_humide_ha DESC NULLS LAST, ss.uf_id, ss.surface_ha DESC
        """
    else:
        sql = f"""
            SELECT
                ss.subset_id,
                ss.uf_id,
                ss.k,
                ss.idus,
                ss.siren,
                ss.denomination,
                ROUND(ss.surface_ha::numeric, 2)              AS surface_ha,
                ROUND(ss.miller::numeric, 3)                  AS miller,
                ROUND((ss.dist_centre_m / 1000.0)::numeric, 3) AS distance_centre_km,
                ss.veg_libelles,
                ss.veg_profile,
                ss.fauna_distances,
                0::numeric AS zone_humide_ha,
                {"(ss.fauna_distances->>:fauna_species)::int" if fauna_species else "NULL::int"} AS dist_fauna_m
            FROM ecocompensation_results.sous_ensembles ss
            WHERE ss.project_id         = :project_id
              AND ss.miller              >= :miller_th
              {cesbio_sql}{fauna_sql}
            ORDER BY ss.uf_id, ss.surface_ha DESC
        """

    with engine.begin() as conn:
        rows = conn.execute(text(sql), params).mappings().all()

    # Groupement Python : uf_id → liste de sous-ensembles
    by_uf: dict[str, list[dict]] = defaultdict(list)
    uf_meta: dict[str, dict] = {}

    for r in rows:
        uf_id = r["uf_id"]
        by_uf[uf_id].append({
            "subset_id":          r["subset_id"],
            "k":                  r["k"],
            "idus":               list(r["idus"] or []),
            "siren":              r["siren"],
            "denomination":       r["denomination"],
            "surface_ha":         float(r["surface_ha"] or 0),
            "miller":             float(r["miller"] or 0),
            "distance_centre_km": float(r["distance_centre_km"] or 0),
            "veg_libelles":       list(r["veg_libelles"] or []),
            "veg_profile":        dict(r["veg_profile"] or {}),
            "fauna_distances":    dict(r["fauna_distances"] or {}),
            "dist_fauna_m":       r["dist_fauna_m"],
            "zone_humide_ha":     float(r["zone_humide_ha"] or 0),
        })
        if uf_id not in uf_meta:
            uf_meta[uf_id] = {
                "uf_id":              uf_id,
                "siren":              r["siren"],
                "denomination":       r["denomination"],
                # nb_parcelles = nb lignes dans unites_foncieres pour cet uf_id
                # on prend max(k) comme proxy (le plus grand sous-ensemble)
                "distance_centre_km": float(r["distance_centre_km"] or 0),
            }

    # Ranking des UF : surface humide (ZH) ou surface parcelle (faune)
    if is_zh:
        ranked_uf_ids = sorted(
            by_uf.keys(),
            key=lambda uid: by_uf[uid][0].get("zone_humide_ha", 0),
            reverse=True,
        )[:limit_uf]
    else:
        ranked_uf_ids = sorted(
            by_uf.keys(),
            key=lambda uid: by_uf[uid][0]["surface_ha"],
            reverse=True,
        )[:limit_uf]

    result = []
    for rang, uf_id in enumerate(ranked_uf_ids, 1):
        meta = uf_meta[uf_id]
        ss_list = by_uf[uf_id]
        result.append({
            "rang":               rang,
            "uf_id":              uf_id,
            "siren":              meta["siren"],
            "denomination":       meta["denomination"],
            "nb_parcelles":       ss_list[0]["k"],  # max k = nb parcelles du meilleur ss
            "distance_centre_km": meta["distance_centre_km"],
            "sous_ensembles":     ss_list,
        })

    _attach_uf_veg(engine, project_id, result)

    total_ss = sum(len(v) for v in by_uf.values())
    return {
        "total_uf":              len(by_uf),
        "total_sous_ensembles":  total_ss,
        "unites_foncieres":      result,
    }