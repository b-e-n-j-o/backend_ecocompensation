#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
map_layers.py
=============

GeoJSON pour la carte résultats (couches nationales clippées à l'AOI) :
  - cesbio        : ecocompensation.vegetation_sur_cesbio
  - fauna         : ecocompensation.fauna (espèces du filtre)
  - fauna_buffer  : buffers ST_Buffer par observation / espèce
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)

STMT_TIMEOUT = "120s"


@dataclass
class FaunaMapCriterion:
    species: str
    dist_m: float


@dataclass
class FilterMapConfig:
    cesbio_libelles: list[str] = field(default_factory=list)
    fauna_criteria: list[FaunaMapCriterion] = field(default_factory=list)


def _coerce_json_dict(val: Any) -> dict:
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    if isinstance(val, str) and val.strip():
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def load_filter_map_config(
    conn,
    project_id: str,
    run_id: str | None = None,
) -> FilterMapConfig:
    """Lit cesbio_libelles + fauna_criteria (filter_v2) depuis run pool ou projet."""
    cfg = FilterMapConfig()

    if run_id:
        row = conn.execute(
            text("""
                SELECT options_json
                FROM ecocompensation_results.parcelles_pool_runs
                WHERE id = CAST(:rid AS uuid)
                  AND project_id = CAST(:pid AS uuid)
                LIMIT 1
            """),
            {"rid": run_id, "pid": project_id},
        ).mappings().one_or_none()
        if row:
            opts = _coerce_json_dict(row.get("options_json"))
            return _parse_filter_map_config(opts)

    row = conn.execute(
        text("""
            SELECT filter_config, last_filter
            FROM ecocompensation.projects
            WHERE id = CAST(:pid AS uuid)
        """),
        {"pid": project_id},
    ).mappings().one_or_none()
    if not row:
        return cfg

    for key in ("filter_config", "last_filter"):
        parsed = _parse_filter_map_config(_coerce_json_dict(row.get(key)))
        if parsed.cesbio_libelles or parsed.fauna_criteria:
            return parsed
    return cfg


def _parse_filter_map_config(opts: dict) -> FilterMapConfig:
    if not opts:
        return FilterMapConfig()

    # Ancien format last_filter enveloppé
    inner = opts.get("options") if isinstance(opts.get("options"), dict) else opts

    cesbio: list[str] = []
    if isinstance(inner.get("cesbio_libelles"), list):
        cesbio = [str(x).strip() for x in inner["cesbio_libelles"] if str(x).strip()]
    elif isinstance(inner.get("vegetation_hybride"), dict):
        ces = inner["vegetation_hybride"].get("cesbio_libelles") or []
        cesbio = [str(x).strip() for x in ces if str(x).strip()]

    fauna: list[FaunaMapCriterion] = []
    raw_fauna = inner.get("fauna_criteria")
    if isinstance(raw_fauna, list):
        for item in raw_fauna:
            if not isinstance(item, dict):
                continue
            species = str(item.get("species") or "").strip()
            if not species:
                continue
            try:
                dist_m = float(item.get("dist_m") or 0)
            except (TypeError, ValueError):
                dist_m = 0.0
            fauna.append(FaunaMapCriterion(species=species, dist_m=dist_m))
    elif inner.get("fauna_species"):
        # legacy orchestrator
        species_list = inner.get("fauna_species") or []
        for s in species_list:
            sp = str(s).strip()
            if sp:
                fauna.append(FaunaMapCriterion(species=sp, dist_m=1000.0))

    return FilterMapConfig(
        cesbio_libelles=list(dict.fromkeys(cesbio)),
        fauna_criteria=fauna,
    )


def _aoi_subquery() -> str:
    return """
        SELECT a.geom_2154 AS geom
        FROM ecocompensation.projects p
        JOIN ecocompensation.aoi a ON a.id = p.aoi_id
        WHERE p.id = CAST(:pid AS uuid)
        LIMIT 1
    """


def get_cesbio_aoi_geojson(conn, project_id: str, libelles: list[str]) -> dict:
    """CESBIO national clippé à l'emprise AOI, filtré par libellés du pool."""
    if not libelles:
        return {"type": "FeatureCollection", "features": []}

    conn.execute(text(f"SET LOCAL statement_timeout = '{STMT_TIMEOUT}'"))
    rows = conn.execute(
        text(f"""
            WITH aoi AS ({_aoi_subquery()})
            SELECT
                COALESCE(v.libelle_prio, v.nature, v.libelle) AS libelle_prio,
                v.libelle,
                v.nature,
                v.source,
                ST_AsGeoJSON(
                    ST_Transform(
                        ST_Intersection(v.geom, aoi.geom),
                        4326
                    ),
                    6
                )::json AS geometry
            FROM ecocompensation.vegetation_sur_cesbio v
            CROSS JOIN aoi
            WHERE v.geom && aoi.geom
              AND ST_Intersects(v.geom, aoi.geom)
              AND v.libelle_prio = ANY(:libelles)
        """),
        {"pid": project_id, "libelles": libelles},
    ).mappings().all()

    features = []
    for r in rows:
        geom = r.get("geometry")
        if not geom:
            continue
        features.append({
            "type": "Feature",
            "geometry": dict(geom),
            "properties": {
                "libelle_prio": r.get("libelle_prio"),
                "libelle": r.get("libelle"),
                "nature": r.get("nature"),
                "source": r.get("source"),
            },
        })
    return {"type": "FeatureCollection", "features": features}


def get_fauna_aoi_geojson(
    conn, project_id: str, criteria: list[FaunaMapCriterion]
) -> dict:
    """Observations faune (points) à portée du filtre (ST_DWithin par espèce / dist_m)."""
    if not criteria:
        return {"type": "FeatureCollection", "features": []}

    species = [c.species for c in criteria]
    dists = [c.dist_m for c in criteria]

    conn.execute(text(f"SET LOCAL statement_timeout = '{STMT_TIMEOUT}'"))
    rows = conn.execute(
        text(f"""
            WITH aoi AS ({_aoi_subquery()}),
            crit AS (
                SELECT *
                FROM unnest(CAST(:species AS text[]), CAST(:dists AS float8[])) AS t(species, dist_m)
            )
            SELECT DISTINCT ON (f.id_obs)
                f.id_obs::text AS id_obs,
                f.nom_vernaculaire,
                f.nom_taxref,
                f.cd_ref,
                COALESCE(NULLIF(btrim(tr.niveau_patrimonialite::text), ''), 'Inconnu') AS niveau_patrimonialite,
                COALESCE(NULLIF(btrim(tr.protection_nationale::text), ''), 'Inconnu') AS protection_nationale,
                f.geom_type,
                f.date_debut,
                f.date_fin,
                ST_AsGeoJSON(
                    ST_Transform(ST_PointOnSurface(ST_MakeValid(f.geometry)), 4326),
                    6
                )::json AS geometry
            FROM ecocompensation.fauna f
            CROSS JOIN aoi
            JOIN crit c ON f.nom_vernaculaire = c.species
            LEFT JOIN ecocompensation.fauna_taxa_ref tr
              ON lower(btrim(f.nom_vernaculaire::text)) = lower(btrim(tr.tax::text))
            WHERE ST_DWithin(f.geometry, aoi.geom, c.dist_m)
            ORDER BY f.id_obs
            LIMIT 15000
        """),
        {"pid": project_id, "species": species, "dists": dists},
    ).mappings().all()

    features = []
    for r in rows:
        geom = r.get("geometry")
        if not geom:
            continue
        props = {k: v for k, v in dict(r).items() if k != "geometry"}
        for k, v in props.items():
            if hasattr(v, "isoformat"):
                props[k] = v.isoformat()
        features.append({"type": "Feature", "geometry": dict(geom), "properties": props})
    return {"type": "FeatureCollection", "features": features}


def get_fauna_buffer_aoi_geojson(
    conn, project_id: str, criteria: list[FaunaMapCriterion]
) -> dict:
    """Polygones buffer autour de chaque observation (dist_m par espèce du filtre)."""
    if not criteria:
        return {"type": "FeatureCollection", "features": []}

    features: list[dict] = []
    conn.execute(text(f"SET LOCAL statement_timeout = '{STMT_TIMEOUT}'"))

    for crit in criteria:
        if crit.dist_m <= 0:
            continue
        rows = conn.execute(
            text(f"""
                WITH aoi AS ({_aoi_subquery()})
                SELECT
                    f.id_obs::text AS id_obs,
                    f.nom_vernaculaire,
                    :dist_m AS buffer_m,
                    ST_AsGeoJSON(
                        ST_Transform(ST_Buffer(f.geometry, :dist_m), 4326),
                        6
                    )::json AS geometry
                FROM ecocompensation.fauna f
                CROSS JOIN aoi
                WHERE f.nom_vernaculaire = :species
                  AND ST_DWithin(f.geometry, aoi.geom, :dist_m)
                LIMIT 5000
            """),
            {
                "pid": project_id,
                "species": crit.species,
                "dist_m": crit.dist_m,
            },
        ).mappings().all()

        for r in rows:
            geom = r.get("geometry")
            if not geom:
                continue
            features.append({
                "type": "Feature",
                "geometry": dict(geom),
                "properties": {
                    "id_obs": r.get("id_obs"),
                    "nom_vernaculaire": r.get("nom_vernaculaire"),
                    "buffer_m": crit.dist_m,
                },
            })

    return {"type": "FeatureCollection", "features": features}
