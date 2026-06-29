#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Couches d'exclusion nationales (tables ecocompensation.*, sans project_id).

Clés alignées sur excluded_layers côté API / frontend.
"""

from __future__ import annotations

DEFAULT_EXCLUDED_LAYERS: tuple[str, ...] = (
    "geomce",
    "preemption_ens",
    "ens",
    "natura_2000",
)

_EXCLUSION_ORDER: tuple[str, ...] = DEFAULT_EXCLUDED_LAYERS


def _clause_exclude_geomce(geom_alias: str) -> str:
    return f"""
        NOT EXISTS (
            SELECT 1
            FROM ecocompensation.geomce_surf gcs
            WHERE gcs.geom_2154 IS NOT NULL
              AND {geom_alias}.geom_2154 && gcs.geom_2154
              AND ST_Intersects({geom_alias}.geom_2154, gcs.geom_2154)
        )
        AND NOT EXISTS (
            SELECT 1
            FROM ecocompensation.geomce_lin gcl
            WHERE gcl.geom_2154 IS NOT NULL
              AND {geom_alias}.geom_2154 && gcl.geom_2154
              AND ST_Intersects({geom_alias}.geom_2154, gcl.geom_2154)
        )
        AND NOT EXISTS (
            SELECT 1
            FROM ecocompensation.geomce_pct gcp
            WHERE gcp.geom_2154 IS NOT NULL
              AND {geom_alias}.geom_2154 && gcp.geom_2154
              AND ST_Intersects({geom_alias}.geom_2154, gcp.geom_2154)
        )
    """


def _clause_exclude_preemption_ens(geom_alias: str) -> str:
    return f"""
        NOT EXISTS (
            SELECT 1
            FROM ecocompensation.preemption_espaces_naturels_sensibles pens
            WHERE pens.geom_2154 IS NOT NULL
              AND {geom_alias}.geom_2154 && pens.geom_2154
              AND ST_Intersects({geom_alias}.geom_2154, pens.geom_2154)
        )
    """


def _clause_exclude_ens(geom_alias: str) -> str:
    return f"""
        NOT EXISTS (
            SELECT 1
            FROM ecocompensation.espaces_naturels_sensibles_ens ens
            WHERE ens.geom_2154 IS NOT NULL
              AND {geom_alias}.geom_2154 && ens.geom_2154
              AND ST_Intersects({geom_alias}.geom_2154, ens.geom_2154)
        )
    """


def _clause_exclude_natura_2000(geom_alias: str) -> str:
    return f"""
        NOT EXISTS (
            SELECT 1
            FROM ecocompensation.natura_2000 n2
            WHERE n2.geom_2154 IS NOT NULL
              AND {geom_alias}.geom_2154 && n2.geom_2154
              AND ST_Intersects({geom_alias}.geom_2154, n2.geom_2154)
        )
    """


_LABELS: dict[str, str] = {
    "geomce": "Hors mesures compensatoires (GEOMCE)",
    "preemption_ens": "Hors préemption espaces naturels sensibles (ENS)",
    "ens": "Hors espaces naturels sensibles (ENS)",
    "natura_2000": "Hors Natura 2000 (ecocompensation.natura_2000)",
}


def national_exclusion_steps(
    excluded_layers: list[str] | set[str],
    *,
    geom_alias: str = "p",
) -> list[tuple[str, str]]:
    """Retourne [(label entonnoir, clause SQL), …] dans un ordre fixe."""
    active = {str(x).strip() for x in excluded_layers if str(x).strip()}
    builders = {
        "geomce": _clause_exclude_geomce,
        "preemption_ens": _clause_exclude_preemption_ens,
        "ens": _clause_exclude_ens,
        "natura_2000": _clause_exclude_natura_2000,
    }
    steps: list[tuple[str, str]] = []
    for key in _EXCLUSION_ORDER:
        if key not in active:
            continue
        builder = builders.get(key)
        if not builder:
            continue
        steps.append((_LABELS[key], builder(geom_alias)))
    return steps
