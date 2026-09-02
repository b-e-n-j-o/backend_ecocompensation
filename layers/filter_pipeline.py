#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
filter_pipeline.py
==================

Pipeline de filtrage écologique sans staging de couches.

Étapes :
  1. Tiling parcelles (aoi_to_parcelles_v2) avec filtre surface
  2. Filtrage spatial : hors emprise projet OU dans foncier (ZH) → Miller
     → exclusion GEOMCE (ecocompensation.geomce_surf/lin/pct, obligatoire)
     → exclusions nationales (GEOMCE, ENS, Natura 2000… selon excluded_layers)
     → CESBIO / Faune / ZH (selon méthode)
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

from layers.common.aoi_to_parcelles_v2 import run as run_parcelles
from layers.national_exclusions import DEFAULT_EXCLUDED_LAYERS, national_exclusion_steps

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
    """exclude_source_buffer = hors emprise + buffer AOI ; within_foncier = à l'intérieur de l'AOI projet."""
    search_mode: str = "exclude_source_buffer"
    zone_humide_mode: str = "ignore"
    zones_humides_probables_mode: str = "ignore"
    """Surface minimale (ha) de zone humide établie intersectant la parcelle (mode intersect). 0 = toute intersection."""
    min_zone_humide_ha: float = 0.0
    """Clés de couches nationales à exclure (geomce, preemption_ens, ens)."""
    excluded_layers: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDED_LAYERS))
    """Distance max (m) au tronçon hydro le plus proche ; None = critère ignoré."""
    troncons_hydros_max_dist_m: float | None = None
    """Distance max (m) à une surface hydro ; None = critère ignoré."""
    surfaces_hydros_max_dist_m: float | None = None


@dataclass
class FilterPipelineResult:
    n_tiled: int = 0
    n_after_filter: int = 0
    n_purged: int = 0
    surviving_idus: list[str] = field(default_factory=list)
    funnel: list[dict] = field(default_factory=list)
    duration_s: float = 0.0
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.error is None


# ── DDL colonnes enrichissement + filter_config projet ────────────────────────

_ENSURE_DDL = """
ALTER TABLE ecocompensation_results.parcelles
    ADD COLUMN IF NOT EXISTS veg_libelles    text[]  NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS fauna_distances jsonb   NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS zone_humide_ha  double precision NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS dist_hydro_m    double precision NULL,
    ADD COLUMN IF NOT EXISTS troncons_hydro_info jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS dist_surface_hydro_m double precision NULL,
    ADD COLUMN IF NOT EXISTS surface_hydro_ha double precision NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS surfaces_hydro_info jsonb NOT NULL DEFAULT '[]'::jsonb;

CREATE INDEX IF NOT EXISTS idx_parcelles_results_veg
    ON ecocompensation_results.parcelles USING GIN (veg_libelles);
CREATE INDEX IF NOT EXISTS idx_parcelles_results_fauna
    ON ecocompensation_results.parcelles USING GIN (fauna_distances);

ALTER TABLE ecocompensation.projects
    ADD COLUMN IF NOT EXISTS filter_config jsonb NOT NULL DEFAULT '{}';
"""


def ensure_columns(engine) -> None:
    """DDL enrichissement parcelles + filter_config. Appelé au démarrage FastAPI."""
    with engine.begin() as conn:
        conn.execute(text(_ENSURE_DDL))

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

_SQL_ENRICH_ZH_BATCH = """
WITH zh_agg AS (
    SELECT
        p.idu,
        ROUND((COALESCE(SUM(ST_Area(ST_Intersection(p.geom_2154, zh.geom_2154))), 0) / 10000.0)::numeric, 4)
            AS zone_humide_ha
    FROM ecocompensation_results.parcelles p
    JOIN ecocompensation_results.zone_humide zh
      ON zh.project_id = p.project_id
     AND zh.geom_2154 IS NOT NULL
     AND p.geom_2154 && zh.geom_2154
     AND ST_Intersects(p.geom_2154, zh.geom_2154)
    WHERE p.project_id = :pid
      AND p.idu = ANY(:idus)
    GROUP BY p.idu
)
UPDATE ecocompensation_results.parcelles p
SET    zone_humide_ha = COALESCE(za.zone_humide_ha, 0)
FROM   zh_agg za
WHERE  p.project_id = :pid
  AND  p.idu = za.idu
"""

_SQL_ENRICH_TRONCONS_HYDRO_BATCH = """
WITH hydro_info AS (
    SELECT
        p.idu,
        (
            SELECT ROUND(ST_Distance(p.geom_2154, th.geom_2154))::int
            FROM ecocompensation_results.troncons_hydros th
            WHERE th.project_id = p.project_id
              AND th.geom_2154 IS NOT NULL
            ORDER BY p.geom_2154 <-> th.geom_2154
            LIMIT 1
        ) AS dist_hydro_m,
        COALESCE((
            SELECT jsonb_agg(
                jsonb_build_object(
                    'cleabs', th.cleabs,
                    'nom', th.nom,
                    'nature', th.nature,
                    'classe_de_largeur', th.classe_de_largeur,
                    'numero_d_ordre', th.numero_d_ordre,
                    'dist_m', ROUND(ST_Distance(p.geom_2154, th.geom_2154))::int
                )
                ORDER BY ST_Distance(p.geom_2154, th.geom_2154)
            )
            FROM ecocompensation_results.troncons_hydros th
            WHERE th.project_id = p.project_id
              AND th.geom_2154 IS NOT NULL
              AND ST_DWithin(p.geom_2154, th.geom_2154, :max_dist_m)
        ), '[]'::jsonb) AS troncons_hydro_info
    FROM ecocompensation_results.parcelles p
    WHERE p.project_id = :pid
      AND p.idu = ANY(:idus)
)
UPDATE ecocompensation_results.parcelles p
SET    dist_hydro_m = hi.dist_hydro_m,
       troncons_hydro_info = hi.troncons_hydro_info
FROM   hydro_info hi
WHERE  p.project_id = :pid
  AND  p.idu = hi.idu
"""

_SQL_ENRICH_SURFACES_HYDRO_BATCH = """
WITH surface_info AS (
    SELECT
        p.idu,
        (
            SELECT ROUND(ST_Distance(p.geom_2154, sh.geom_2154))::int
            FROM ecocompensation_results.surfaces_hydros sh
            WHERE sh.project_id = p.project_id
              AND sh.geom_2154 IS NOT NULL
            ORDER BY p.geom_2154 <-> sh.geom_2154
            LIMIT 1
        ) AS dist_surface_hydro_m,
        ROUND((
            SELECT COALESCE(SUM(ST_Area(ST_Intersection(p.geom_2154, sh.geom_2154))), 0) / 10000.0
            FROM ecocompensation_results.surfaces_hydros sh
            WHERE sh.project_id = p.project_id
              AND sh.geom_2154 IS NOT NULL
              AND p.geom_2154 && sh.geom_2154
              AND ST_Intersects(p.geom_2154, sh.geom_2154)
              AND ST_DWithin(p.geom_2154, sh.geom_2154, :max_dist_m)
        )::numeric, 4) AS surface_hydro_ha,
        COALESCE((
            SELECT jsonb_agg(
                jsonb_build_object(
                    'cleabs', sh.cleabs,
                    'nom', sh.nom,
                    'nature', sh.nature,
                    'position_par_rapport_au_sol', sh.position_par_rapport_au_sol,
                    'statut', sh.statut,
                    'dist_m', ROUND(ST_Distance(p.geom_2154, sh.geom_2154))::int,
                    'intersect_ha', ROUND(
                        (COALESCE(ST_Area(ST_Intersection(p.geom_2154, sh.geom_2154)), 0) / 10000.0)::numeric,
                        4
                    )
                )
                ORDER BY ST_Distance(p.geom_2154, sh.geom_2154)
            )
            FROM ecocompensation_results.surfaces_hydros sh
            WHERE sh.project_id = p.project_id
              AND sh.geom_2154 IS NOT NULL
              AND ST_DWithin(p.geom_2154, sh.geom_2154, :max_dist_m)
        ), '[]'::jsonb) AS surfaces_hydro_info
    FROM ecocompensation_results.parcelles p
    WHERE p.project_id = :pid
      AND p.idu = ANY(:idus)
)
UPDATE ecocompensation_results.parcelles p
SET    dist_surface_hydro_m = si.dist_surface_hydro_m,
       surface_hydro_ha = COALESCE(si.surface_hydro_ha, 0),
       surfaces_hydro_info = si.surfaces_hydro_info
FROM   surface_info si
WHERE  p.project_id = :pid
  AND  p.idu = si.idu
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

_SQL_AOI_GEOM = """
    (
        SELECT a.geom_2154
        FROM ecocompensation.projects pr
        JOIN ecocompensation.aoi a ON a.id = pr.aoi_id
        WHERE pr.id = CAST(:project_id AS uuid)
        LIMIT 1
    )
"""

_CLAUSE_EXCLUDE_SOURCE = f"""
    NOT ST_Intersects(p.geom_2154, ({_SQL_SOURCE_GEOM}))
"""

_CLAUSE_WITHIN_SOURCE = f"""
    ST_Intersects(p.geom_2154, ({_SQL_AOI_GEOM}))
"""

def _layer_intersect_clause(table: str, alias: str, geom_column: str = "geom_2154") -> str:
    return f"""
        EXISTS (
            SELECT 1 FROM {table} {alias}
            WHERE {alias}.project_id = CAST(:project_id AS uuid)
              AND {alias}.{geom_column} IS NOT NULL
              AND p.geom_2154 && {alias}.{geom_column}
              AND ST_Intersects(p.geom_2154, {alias}.{geom_column})
        )
    """


def _layer_exclude_clause(table: str, alias: str, geom_column: str = "geom_2154") -> str:
    return f"""
        NOT EXISTS (
            SELECT 1 FROM {table} {alias}
            WHERE {alias}.project_id = CAST(:project_id AS uuid)
              AND {alias}.{geom_column} IS NOT NULL
              AND p.geom_2154 && {alias}.{geom_column}
              AND ST_Intersects(p.geom_2154, {alias}.{geom_column})
        )
    """


def _clause_min_project_layer_intersection_area_m2(
    table: str,
    alias: str,
    geom_column: str,
) -> str:
    """Somme des surfaces d'intersection parcelle ∩ couche projet ≥ seuil (m²)."""
    return f"""
        COALESCE((
            SELECT SUM(ST_Area(ST_Intersection(p.geom_2154, {alias}.{geom_column})))
            FROM {table} {alias}
            WHERE {alias}.project_id = CAST(:project_id AS uuid)
              AND {alias}.{geom_column} IS NOT NULL
              AND p.geom_2154 && {alias}.{geom_column}
              AND ST_Intersects(p.geom_2154, {alias}.{geom_column})
        ), 0) >= :min_zone_humide_m2
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
    spatial_mode: str | None = None,
) -> tuple[list[str], list[str]]:
    """Retourne (labels, clauses SQL cumulatives)."""
    labels = [f"Candidats (surface ≥ {config.min_area_ha} ha)"]
    clauses = ["p.project_id = :project_id"]

    if spatial_mode == "within_foncier":
        labels.append("Dans le périmètre d'étude (AOI)")
        clauses.append(_CLAUSE_WITHIN_SOURCE)
    elif spatial_mode == "exclude_source":
        labels.append("Hors emprise projet (source)")
        clauses.append(_CLAUSE_EXCLUDE_SOURCE)

    labels.append(f"Miller ≥ {config.miller_thresh}")
    clauses.append("""
        (4.0 * PI() * ST_Area(p.geom_2154))
        / NULLIF(ST_Perimeter(p.geom_2154)^2, 0)::double precision
        >= :miller_th
    """)

    for label, clause in national_exclusion_steps(config.excluded_layers, geom_alias="p"):
        labels.append(label)
        clauses.append(clause)

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

    if config.zone_humide_mode == "intersect":
        if config.min_zone_humide_ha > 0:
            labels.append(
                f"Zones humides établies — ≥ {config.min_zone_humide_ha:g} ha intersectés"
            )
            clauses.append(
                _clause_min_project_layer_intersection_area_m2(
                    "ecocompensation_results.zone_humide",
                    "zh",
                    "geom_2154",
                )
            )
        else:
            labels.append("Zones humides établies — intersecte")
            clauses.append(_layer_intersect_clause("ecocompensation_results.zone_humide", "zh"))
    elif config.zone_humide_mode == "exclude":
        labels.append("Zones humides établies — n'intersecte pas")
        clauses.append(_layer_exclude_clause("ecocompensation_results.zone_humide", "zh"))

    if config.zones_humides_probables_mode == "intersect":
        labels.append("Zones humides probables — intersecte")
        clauses.append(
            _layer_intersect_clause(
                "ecocompensation_results.zones_humides_probables",
                "zhp",
                geom_column="geom",
            )
        )
    elif config.zones_humides_probables_mode == "exclude":
        labels.append("Zones humides probables — n'intersecte pas")
        clauses.append(
            _layer_exclude_clause(
                "ecocompensation_results.zones_humides_probables",
                "zhp",
                geom_column="geom",
            )
        )

    if config.troncons_hydros_max_dist_m is not None:
        if config.troncons_hydros_max_dist_m <= 0:
            labels.append("Tronçons hydro — intersecte")
            clauses.append(
                _layer_intersect_clause(
                    "ecocompensation_results.troncons_hydros",
                    "th",
                )
            )
        else:
            labels.append(
                f"Tronçons hydro — ≤ {config.troncons_hydros_max_dist_m:g} m"
            )
            clauses.append("""
                EXISTS (
                    SELECT 1
                    FROM ecocompensation_results.troncons_hydros th
                    WHERE th.project_id = CAST(:project_id AS uuid)
                      AND th.geom_2154 IS NOT NULL
                      AND ST_DWithin(
                          p.geom_2154,
                          th.geom_2154,
                          :troncons_hydros_max_dist_m
                      )
                )
            """)

    if config.surfaces_hydros_max_dist_m is not None:
        if config.surfaces_hydros_max_dist_m <= 0:
            labels.append("Surfaces hydro — intersecte")
            clauses.append(
                _layer_intersect_clause(
                    "ecocompensation_results.surfaces_hydros",
                    "sh",
                )
            )
        else:
            labels.append(
                f"Surfaces hydro — ≤ {config.surfaces_hydros_max_dist_m:g} m"
            )
            clauses.append("""
                EXISTS (
                    SELECT 1
                    FROM ecocompensation_results.surfaces_hydros sh
                    WHERE sh.project_id = CAST(:project_id AS uuid)
                      AND sh.geom_2154 IS NOT NULL
                      AND ST_DWithin(
                          p.geom_2154,
                          sh.geom_2154,
                          :surfaces_hydros_max_dist_m
                      )
                )
            """)

    return labels, clauses


def _filter_params(project_id: str, config: FilterConfig) -> dict:
    params: dict = {
        "project_id": project_id,
        "miller_th": config.miller_thresh,
        "cesbio_libelles": config.cesbio_libelles,
        "min_zone_humide_m2": config.min_zone_humide_ha * 10_000.0,
    }
    for i, fc in enumerate(config.fauna_criteria):
        params[f"fauna_species_{i}"] = fc.species
        params[f"fauna_dist_m_{i}"] = fc.dist_m
    if config.troncons_hydros_max_dist_m is not None:
        params["troncons_hydros_max_dist_m"] = config.troncons_hydros_max_dist_m
    if config.surfaces_hydros_max_dist_m is not None:
        params["surfaces_hydros_max_dist_m"] = config.surfaces_hydros_max_dist_m
    return params


def _spatial_filter(
    engine, project_id: str, config: FilterConfig, log: Callable[[str], None]
) -> tuple[list[str], list[dict]]:
    has_source = _has_source_geometry(engine, project_id)
    if config.search_mode == "within_foncier":
        if not has_source:
            log("WITHIN_FONCIER:error_no_geom")
            return [], []
        spatial_mode = "within_foncier"
        log("WITHIN_FONCIER:enabled")
    elif has_source:
        spatial_mode = "exclude_source"
        log("SOURCE_EXCLUSION:enabled")
    else:
        spatial_mode = None
        log("SOURCE_EXCLUSION:skipped_no_geom")

    labels, all_clauses = _build_filter_clauses(config, spatial_mode=spatial_mode)
    params = _filter_params(project_id, config)
    funnel_steps: list[dict] = []

    with engine.begin() as conn:
        cumulative: list[str] = []
        for step_idx, (label, clause) in enumerate(zip(labels, all_clauses)):
            cumulative.append(clause)
            where = " AND ".join(f"({c})" for c in cumulative)
            t0 = time.perf_counter()
            n = conn.execute(
                text(f"SELECT COUNT(*) FROM ecocompensation_results.parcelles p WHERE {where}"),
                params,
            ).scalar_one()
            dt = round(time.perf_counter() - t0, 2)
            count = int(n)
            log(f"FILTER_STEP:{label}:{count}:{dt}s")
            funnel_steps.append({"step": step_idx, "label": label, "count": count})
            if count == 0 and len(cumulative) > 1:
                return [], funnel_steps

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

    return [str(i) for i in idus], funnel_steps


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
    *,
    enrich_zone_humide: bool = False,
    enrich_troncons_hydro: bool = False,
    troncons_max_dist_m: float = 100.0,
    enrich_surfaces_hydro: bool = False,
    surfaces_max_dist_m: float = 100.0,
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

        if enrich_zone_humide:
            t0 = time.perf_counter()
            with engine.begin() as conn:
                conn.execute(text(f"SET LOCAL statement_timeout = '{STMT_TIMEOUT}'"))
                conn.execute(text(_SQL_ENRICH_ZH_BATCH), {"pid": project_id, "idus": batch})
            log(
                f"ENRICH_BATCH:{i}/{len(batches)}:zh:{len(batch)}:"
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

        if enrich_troncons_hydro:
            t0 = time.perf_counter()
            with engine.begin() as conn:
                conn.execute(text(f"SET LOCAL statement_timeout = '{STMT_TIMEOUT}'"))
                conn.execute(
                    text(_SQL_ENRICH_TRONCONS_HYDRO_BATCH),
                    {"pid": project_id, "idus": batch, "max_dist_m": troncons_max_dist_m},
                )
            log(
                f"ENRICH_BATCH:{i}/{len(batches)}:troncons_hydro:{len(batch)}:"
                f"{round(time.perf_counter() - t0, 1)}s"
            )

        if enrich_surfaces_hydro:
            t0 = time.perf_counter()
            with engine.begin() as conn:
                conn.execute(text(f"SET LOCAL statement_timeout = '{STMT_TIMEOUT}'"))
                conn.execute(
                    text(_SQL_ENRICH_SURFACES_HYDRO_BATCH),
                    {"pid": project_id, "idus": batch, "max_dist_m": surfaces_max_dist_m},
                )
            log(
                f"ENRICH_BATCH:{i}/{len(batches)}:surfaces_hydro:{len(batch)}:"
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
        "zone_humide_mode": config.zone_humide_mode,
        "zones_humides_probables_mode": config.zones_humides_probables_mode,
        "min_zone_humide_ha": config.min_zone_humide_ha,
        "troncons_hydros_max_dist_m": config.troncons_hydros_max_dist_m,
        "surfaces_hydros_max_dist_m": config.surfaces_hydros_max_dist_m,
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
        surviving, filter_funnel = _spatial_filter(engine, project_id, config, log)
        result.surviving_idus = surviving
        result.n_after_filter = len(surviving)
        result.funnel = filter_funnel
        log(f"PHASE:filter:done:{len(surviving)}")

        # ── 3. Purge non-survivantes ────────────────────────────────────────
        log("PHASE:purge:start")
        n_purged = _purge_non_survivors(engine, project_id, surviving)
        result.n_purged = n_purged
        log(f"PHASE:purge:done:{n_purged}")

        # ── 4. Enrichissement léger ─────────────────────────────────────────
        if surviving:
            species = [fc.species for fc in config.fauna_criteria]
            enrich_zh = config.zone_humide_mode in ("intersect", "exclude")
            enrich_troncons = config.troncons_hydros_max_dist_m is not None
            enrich_surfaces = config.surfaces_hydros_max_dist_m is not None
            log(f"PHASE:enrich:start:{len(surviving)}")
            _enrich_survivors(
                engine,
                project_id,
                surviving,
                species,
                log,
                enrich_zone_humide=enrich_zh,
                enrich_troncons_hydro=enrich_troncons,
                troncons_max_dist_m=max(config.troncons_hydros_max_dist_m or 0.0, 0.0),
                enrich_surfaces_hydro=enrich_surfaces,
                surfaces_max_dist_m=max(config.surfaces_hydros_max_dist_m or 0.0, 0.0),
            )
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
