#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
enrich_uf.py
============

Enrichit ecocompensation_results.sous_ensembles avec :
  - veg_libelles    text[]  : libellés CESBIO intersectant l'union de parcelles
  - fauna_distances jsonb   : { "nom_vernaculaire": dist_m }

Même pattern que enrich_candidates.py, colonne clé = subset_id, géom = geom_2154.
"""
from __future__ import annotations
import math
from collections import defaultdict
from typing import Callable
from sqlalchemy import text

VEG_BATCH_SIZE   = 100
FAUNA_BATCH_SIZE = 300
STMT_TIMEOUT     = "90s"

# ── DDL ───────────────────────────────────────────────────────────────────────
_ALTER_DDL = """
ALTER TABLE ecocompensation_results.sous_ensembles
    ADD COLUMN IF NOT EXISTS veg_libelles    text[]  NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS fauna_distances jsonb   NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_ss_veg
    ON ecocompensation_results.sous_ensembles USING GIN (veg_libelles);
CREATE INDEX IF NOT EXISTS idx_ss_fauna
    ON ecocompensation_results.sous_ensembles USING GIN (fauna_distances);
"""

def ensure_columns(engine) -> None:
    """Migration one-shot. Appeler au démarrage de l'app avec ensure_columns de enrich_candidates."""
    with engine.begin() as conn:
        conn.execute(text(_ALTER_DDL))

# ── SQL enrichissement ────────────────────────────────────────────────────────

_SQL_VEG_BATCH = """
WITH veg_agg AS (
    SELECT
        ss.subset_id,
        array_agg(DISTINCT v.libelle_prio ORDER BY v.libelle_prio)
            FILTER (WHERE v.libelle_prio IS NOT NULL) AS veg_libelles
    FROM ecocompensation_results.sous_ensembles ss
    JOIN ecocompensation.vegetation_sur_cesbio v
        ON ss.geom_2154 && v.geom
       AND ST_Intersects(ss.geom_2154, v.geom)
    WHERE ss.project_id = :pid
      AND ss.subset_id  = ANY(CAST(:batch AS text[]))
    GROUP BY ss.subset_id
)
UPDATE ecocompensation_results.sous_ensembles ss
SET    veg_libelles = COALESCE(va.veg_libelles, '{}')
FROM   veg_agg va
WHERE  ss.project_id = :pid
  AND  ss.subset_id  = va.subset_id
"""

_SQL_FAUNA_BATCH = """
WITH fauna_agg AS (
    SELECT
        ss.subset_id,
        COALESCE(
            (
                SELECT ROUND(ST_Distance(ss.geom_2154, f.geometry))::int
                FROM   ecocompensation.fauna f
                WHERE  f.nom_vernaculaire = :species
                ORDER  BY ss.geom_2154 <-> f.geometry
                LIMIT  1
            ),
            -1
        ) AS dist_m
    FROM ecocompensation_results.sous_ensembles ss
    WHERE ss.project_id = :pid
      AND ss.subset_id  = ANY(CAST(:batch AS text[]))
)
UPDATE ecocompensation_results.sous_ensembles ss
SET    fauna_distances = fauna_distances || jsonb_build_object(:species, fa.dist_m)
FROM   fauna_agg fa
WHERE  ss.project_id = :pid
  AND  ss.subset_id  = fa.subset_id
"""

# ── run() ─────────────────────────────────────────────────────────────────────

def run(
    engine,
    project_id:   str,
    aoi_id:       str,
    cb:           Callable[[str], None] | None = None,
    *,
    species_list: list[str] | None = None,
) -> int:
    """
    Enrichit tous les sous-ensembles du projet avec veg_libelles + fauna_distances.
    Signature compatible layer_runner. Retourne le nombre de sous-ensembles traités.
    """
    log = cb or (lambda m: None)
    species = species_list or []

    with engine.begin() as conn:
        n = conn.execute(
            text("SELECT COUNT(*) FROM ecocompensation_results.sous_ensembles WHERE project_id = :pid"),
            {"pid": project_id},
        ).scalar_one()

    if n == 0:
        log("[ENRICH_UF] Aucun sous-ensemble — lancer d'abord sous_ensembles.")
        return 0

    log(f"[ENRICH_UF] {n:,} sous-ensembles à enrichir.")

    with engine.begin() as conn:
        subset_ids: list[str] = [
            row[0] for row in conn.execute(
                text("SELECT subset_id FROM ecocompensation_results.sous_ensembles "
                     "WHERE project_id = :pid ORDER BY subset_id"),
                {"pid": project_id},
            ).fetchall()
        ]

    # Végétation
    n_veg = math.ceil(len(subset_ids) / VEG_BATCH_SIZE)
    log(f"[ENRICH_UF] 🌿 Vég : {n_veg} batch(s) × {VEG_BATCH_SIZE}")
    for bi, start in enumerate(range(0, len(subset_ids), VEG_BATCH_SIZE), 1):
        batch = subset_ids[start:start + VEG_BATCH_SIZE]
        with engine.begin() as conn:
            conn.execute(text(f"SET LOCAL statement_timeout = '{STMT_TIMEOUT}'"))
            conn.execute(text(_SQL_VEG_BATCH), {"pid": project_id, "batch": batch})
        log(f"TILE_PROGRESS:{bi}/{n_veg}:{bi*VEG_BATCH_SIZE} 🌿 Vég {bi}/{n_veg}")
    log("[ENRICH_UF] ✓ veg_libelles mis à jour.")

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
                                 {"pid": project_id, "batch": batch, "species": sp})
                log(f"TILE_PROGRESS:{bi}/{n_fauna}:{bi*FAUNA_BATCH_SIZE} 🦎 {sp} {bi}/{n_fauna}")
            log(f"[ENRICH_UF] ✓ fauna '{sp}' mis à jour.")
    else:
        log("[ENRICH_UF] Aucune espèce — fauna_distances ignorée.")

    return n


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
    miller_thresh: float = 0.39,
    limit_uf: int = 50,
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
    }
    fauna_sql = ""
    if fauna_species:
        params["fauna_species"] = fauna_species
        fauna_sql = """
              AND (ss.fauna_distances->>:fauna_species) IS NOT NULL
              AND (ss.fauna_distances->>:fauna_species)::int BETWEEN 0 AND :fauna_dist_m"""

    with engine.begin() as conn:
        rows = conn.execute(text(f"""
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
                ss.fauna_distances,
                {"(ss.fauna_distances->>:fauna_species)::int" if fauna_species else "NULL::int"} AS dist_fauna_m
            FROM ecocompensation_results.sous_ensembles ss
            WHERE ss.project_id         = :project_id
              AND ss.miller              >= :miller_th
              AND ss.veg_libelles        && CAST(:cesbio_libelles AS text[]){fauna_sql}
            ORDER BY ss.uf_id, ss.surface_ha DESC
        """), params).mappings().all()

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
            "fauna_distances":    dict(r["fauna_distances"] or {}),
            "dist_fauna_m":       r["dist_fauna_m"],
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

    # Ranking des UF par meilleur surface_ha (premier ss de chaque groupe, déjà trié DESC)
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

    total_ss = sum(len(v) for v in by_uf.values())
    return {
        "total_uf":              len(by_uf),
        "total_sous_ensembles":  total_ss,
        "unites_foncieres":      result,
    }