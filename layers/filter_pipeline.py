#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
filter_pipeline.py
==================

Pipeline de filtrage écologique sans staging de couches.

Étapes :
  1. Tiling parcelles (aoi_to_parcelles_v2) avec filtre surface
  2. Filtrage spatial : hors emprise projet → Miller → CESBIO EXISTS → Faune ST_DWithin (optionnel)
  3. Purge des parcelles éliminées (ecocompensation_results.parcelles = pool final)
  4. Enrichissement léger sur survivantes (veg_libelles + fauna_distances)

Le profiling riche CESBIO (surfaces/pct) est dans pool/profilers/profile_cesbio.py — hors pipeline.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from sqlalchemy import text

from layers.aoi_to_parcelles_v2 import run as run_parcelles

logger = logging.getLogger(__name__)

ENRICH_BATCH_SIZE = 200
STMT_TIMEOUT = "90s"

Cb = Callable[[str], None] | None


def _make_log(project_id: str, cb: Cb) -> Callable[[str], None]:
    """Log serveur + callback WS optionnel."""
    def log(msg: str) -> None:
        logger.info("[filter_pipeline] project_id=%s %s", project_id, msg)
        if cb:
            cb(msg)
    return log


@dataclass
class FaunaCriterion:
    species: str
    dist_m: float


@dataclass
class FilterConfig:
    min_area_ha: float = 7.0
    miller_thresh: float = 0.39
    cesbio_libelles: list[str] = field(default_factory=list)
    fauna_criteria: list[FaunaCriterion] = field(default_factory=list)


@dataclass
class FilterPipelineResult:
    n_tiled: int = 0
    n_after_filter: int = 0
    n_purged: int = 0
    surviving_idus: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.error is None


# ── DDL colonnes enrichissement + filter_config projet ────────────────────────

_ENSURE_DDL = """
ALTER TABLE ecocompensation_results.parcelles
    ADD COLUMN IF NOT EXISTS veg_libelles    text[]  NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS fauna_distances jsonb   NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_parcelles_results_veg
    ON ecocompensation_results.parcelles USING GIN (veg_libelles);
CREATE INDEX IF NOT EXISTS idx_parcelles_results_fauna
    ON ecocompensation_results.parcelles USING GIN (fauna_distances);

ALTER TABLE ecocompensation.projects
    ADD COLUMN IF NOT EXISTS filter_config jsonb NOT NULL DEFAULT '{}';
"""

_SQL_ENRICH_VEG_BATCH = """
WITH veg_agg AS (
    SELECT
        p.idu,
        array_agg(DISTINCT v.libelle_prio ORDER BY v.libelle_prio)
            FILTER (WHERE v.libelle_prio IS NOT NULL) AS veg_libelles
    FROM ecocompensation_results.parcelles p
    JOIN ecocompensation.vegetation_sur_cesbio v
        ON p.geom_2154 && v.geom
       AND ST_Intersects(p.geom_2154, v.geom)
    WHERE p.project_id = :pid
      AND p.idu = ANY(:idus)
    GROUP BY p.idu
)
UPDATE ecocompensation_results.parcelles p
SET    veg_libelles = COALESCE(va.veg_libelles, '{}')
FROM   veg_agg va
WHERE  p.project_id = :pid
  AND  p.idu = va.idu
"""

_SQL_ENRICH_FAUNA_BATCH = """
WITH fauna_agg AS (
    SELECT
        p.idu,
        COALESCE(
            (
                SELECT ROUND(ST_Distance(p.geom_2154, f.geometry))::int
                FROM   ecocompensation.fauna f
                WHERE  f.nom_vernaculaire = :species
                ORDER  BY p.geom_2154 <-> f.geometry
                LIMIT  1
            ),
            -1
        ) AS dist_m
    FROM ecocompensation_results.parcelles p
    WHERE p.project_id = :pid
      AND p.idu = ANY(:idus)
)
UPDATE ecocompensation_results.parcelles p
SET    fauna_distances = fauna_distances || jsonb_build_object(:species, fa.dist_m)
FROM   fauna_agg fa
WHERE  p.project_id = :pid
  AND  p.idu = fa.idu
"""


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


_SQL_SOURCE_GEOM = """
    COALESCE(
        (
            SELECT f.geom_2154
            FROM ecocompensation.projects pr
            JOIN ecocompensation.foncier f ON f.id = pr.foncier_id
            WHERE pr.id = CAST(:project_id AS uuid)
            LIMIT 1
        ),
        (
            SELECT ST_Union(pp.geom_2154)
            FROM ecocompensation.project_parcelles pp
            WHERE pp.project_id = CAST(:project_id AS uuid)
        )
    )
"""

_CLAUSE_EXCLUDE_SOURCE = f"""
    NOT ST_Intersects(p.geom_2154, ({_SQL_SOURCE_GEOM}))
"""


def _has_source_geometry(engine, project_id: str) -> bool:
    """True si le projet a une géométrie source (foncier ou parcelle(s) initiale(s))."""
    with engine.connect() as conn:
        found = conn.execute(
            text(f"SELECT ({_SQL_SOURCE_GEOM}) IS NOT NULL AS ok"),
            {"project_id": project_id},
        ).scalar_one()
    return bool(found)


def _build_filter_clauses(
    config: FilterConfig,
    *,
    exclude_source_geom: bool = False,
) -> tuple[list[str], list[str]]:
    """Retourne (labels, clauses SQL cumulatives)."""
    labels = [f"Candidats (surface ≥ {config.min_area_ha} ha)"]
    clauses = ["p.project_id = :project_id"]

    if exclude_source_geom:
        labels.append("Hors emprise projet (source)")
        clauses.append(_CLAUSE_EXCLUDE_SOURCE)

    labels.append(f"Miller ≥ {config.miller_thresh}")
    clauses.append("""
        (4.0 * PI() * ST_Area(p.geom_2154))
        / NULLIF(ST_Perimeter(p.geom_2154)^2, 0)::double precision
        >= :miller_th
    """)

    if config.cesbio_libelles:
        labels.append("CESBIO (EXISTS/GiST)")
        clauses.append("""
            EXISTS (
                SELECT 1
                FROM ecocompensation.vegetation_sur_cesbio v
                WHERE v.libelle_prio = ANY(:cesbio_libelles)
                  AND p.geom_2154 && v.geom
                  AND ST_Intersects(p.geom_2154, v.geom)
            )
        """)

    if config.fauna_criteria:
        fauna_parts = []
        for i, fc in enumerate(config.fauna_criteria):
            fauna_parts.append(f"""
                EXISTS (
                    SELECT 1
                    FROM ecocompensation.fauna f
                    WHERE f.nom_vernaculaire = :fauna_species_{i}
                      AND ST_DWithin(p.geom_2154, f.geometry, :fauna_dist_m_{i})
                )
            """)
        labels.append(f"Faune ({len(config.fauna_criteria)} espèce(s))")
        clauses.append("(" + " OR ".join(f"({p})" for p in fauna_parts) + ")")

    return labels, clauses


def _filter_params(project_id: str, config: FilterConfig) -> dict:
    params: dict = {
        "project_id": project_id,
        "miller_th": config.miller_thresh,
        "cesbio_libelles": config.cesbio_libelles,
    }
    for i, fc in enumerate(config.fauna_criteria):
        params[f"fauna_species_{i}"] = fc.species
        params[f"fauna_dist_m_{i}"] = fc.dist_m
    return params


def _spatial_filter(
    engine, project_id: str, config: FilterConfig, log: Callable[[str], None]
) -> list[str]:
    exclude_source = _has_source_geometry(engine, project_id)
    if exclude_source:
        log("SOURCE_EXCLUSION:enabled")
    else:
        log("SOURCE_EXCLUSION:skipped_no_geom")

    labels, all_clauses = _build_filter_clauses(
        config, exclude_source_geom=exclude_source
    )
    params = _filter_params(project_id, config)

    with engine.begin() as conn:
        cumulative: list[str] = []
        for label, clause in zip(labels, all_clauses):
            cumulative.append(clause)
            where = " AND ".join(f"({c})" for c in cumulative)
            t0 = time.perf_counter()
            n = conn.execute(
                text(f"SELECT COUNT(*) FROM ecocompensation_results.parcelles p WHERE {where}"),
                params,
            ).scalar_one()
            dt = round(time.perf_counter() - t0, 2)
            log(f"FILTER_STEP:{label}:{int(n)}:{dt}s")
            if int(n) == 0 and len(cumulative) > 1:
                return []

        final_where = " AND ".join(f"({c})" for c in cumulative)
        idus = conn.execute(
            text(f"""
                SELECT p.idu
                FROM ecocompensation_results.parcelles p
                WHERE {final_where}
                ORDER BY ST_Area(p.geom_2154) DESC
            """),
            params,
        ).scalars().all()

    return [str(i) for i in idus]


def _purge_non_survivors(engine, project_id: str, surviving_idus: list[str]) -> int:
    with engine.begin() as conn:
        if not surviving_idus:
            deleted = conn.execute(
                text("DELETE FROM ecocompensation_results.parcelles WHERE project_id = :pid"),
                {"pid": project_id},
            ).rowcount
            return int(deleted or 0)
        deleted = conn.execute(
            text("""
                DELETE FROM ecocompensation_results.parcelles
                WHERE project_id = :pid
                  AND NOT (idu = ANY(:idus))
            """),
            {"pid": project_id, "idus": surviving_idus},
        ).rowcount
    return int(deleted or 0)


def _enrich_survivors(
    engine,
    project_id: str,
    idus: list[str],
    species_list: list[str],
    log: Callable[[str], None],
) -> None:
    if not idus:
        return

    batches = _chunks(idus, ENRICH_BATCH_SIZE)
    for i, batch in enumerate(batches, 1):
        t0 = time.perf_counter()
        with engine.begin() as conn:
            conn.execute(text(f"SET LOCAL statement_timeout = '{STMT_TIMEOUT}'"))
            conn.execute(text(_SQL_ENRICH_VEG_BATCH), {"pid": project_id, "idus": batch})
        log(
            f"ENRICH_BATCH:{i}/{len(batches)}:veg:{len(batch)}:"
            f"{round(time.perf_counter() - t0, 1)}s"
        )

        for species in species_list:
            t0 = time.perf_counter()
            with engine.begin() as conn:
                conn.execute(text(f"SET LOCAL statement_timeout = '{STMT_TIMEOUT}'"))
                conn.execute(
                    text(_SQL_ENRICH_FAUNA_BATCH),
                    {"pid": project_id, "idus": batch, "species": species},
                )
            log(
                f"ENRICH_BATCH:{i}/{len(batches)}:fauna:{species}:"
                f"{round(time.perf_counter() - t0, 1)}s"
            )


def _save_filter_config(engine, project_id: str, config: FilterConfig) -> None:
    import json

    payload = {
        "min_area_ha": config.min_area_ha,
        "miller_thresh": config.miller_thresh,
        "cesbio_libelles": config.cesbio_libelles,
        "fauna_criteria": [
            {"species": fc.species, "dist_m": fc.dist_m}
            for fc in config.fauna_criteria
        ],
    }
    with engine.begin() as conn:
        conn.execute(text(_ENSURE_DDL))
        conn.execute(
            text("""
                UPDATE ecocompensation.projects
                SET filter_config = CAST(:cfg AS jsonb), updated_at = now()
                WHERE id = :pid
            """),
            {"pid": project_id, "cfg": json.dumps(payload)},
        )


def run(
    engine,
    project_id: str,
    aoi_id: str,
    config: FilterConfig,
    cb: Cb = None,
) -> FilterPipelineResult:
    """
    Exécute le pipeline complet pour un projet/AOI existants.
    Les parcelles finales restent dans ecocompensation_results.parcelles.
    """
    log = _make_log(project_id, cb)
    t_global = time.perf_counter()
    log(
        f"PIPELINE:start min_area={config.min_area_ha}ha miller≥{config.miller_thresh} "
        f"cesbio={len(config.cesbio_libelles)} fauna={len(config.fauna_criteria)}"
    )
    result = FilterPipelineResult()

    try:
        _save_filter_config(engine, project_id, config)

        # ── 1. Tiling parcelles ─────────────────────────────────────────────
        log(f"PHASE:parcelles:start surface≥{config.min_area_ha}ha")
        n_tiled = run_parcelles(
            engine, project_id, aoi_id, cb=log, min_area_ha=config.min_area_ha
        )
        result.n_tiled = n_tiled
        log(f"PHASE:parcelles:done:{n_tiled}")

        if n_tiled == 0:
            result.duration_s = time.perf_counter() - t_global
            return result

        # ── 2. Filtrage spatial ─────────────────────────────────────────────
        log("PHASE:filter:start")
        surviving = _spatial_filter(engine, project_id, config, log)
        result.surviving_idus = surviving
        result.n_after_filter = len(surviving)
        log(f"PHASE:filter:done:{len(surviving)}")

        # ── 3. Purge non-survivantes ────────────────────────────────────────
        log("PHASE:purge:start")
        n_purged = _purge_non_survivors(engine, project_id, surviving)
        result.n_purged = n_purged
        log(f"PHASE:purge:done:{n_purged}")

        # ── 4. Enrichissement léger ─────────────────────────────────────────
        if surviving:
            species = [fc.species for fc in config.fauna_criteria]
            log(f"PHASE:enrich:start:{len(surviving)}")
            _enrich_survivors(engine, project_id, surviving, species, log)
            log(f"PHASE:enrich:done:{len(surviving)}")

        result.duration_s = round(time.perf_counter() - t_global, 1)
        log(f"PHASE:complete:{len(surviving)}:{result.duration_s}s")
        return result

    except Exception as e:
        logger.exception("Erreur filter_pipeline project_id=%s", project_id)
        result.error = str(e)
        result.duration_s = round(time.perf_counter() - t_global, 1)
        log(f"PHASE:error:{e}")
        return result
