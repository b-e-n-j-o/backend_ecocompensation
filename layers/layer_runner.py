#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
layer_runner.py
===============

Registre filter_v2. Chaque module aoi_to_*.py expose :
    run(engine, project_id: str, aoi_id: str, cb=None) -> int

Couches spéciales :
    - sous_ensembles : aoi_to_sub_uf — PPM → clustering SIREN → k-sous-ensembles
    - enrich_uf      : flags végétation / faune sur sous_ensembles
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

from layers.common.aoi_to_sub_uf import run as _run_sub_uf
from layers.common.aoi_to_parcelles_v2 import run as _run_parcelles
from layers.common.aoi_to_espaces_naturels_sensibles_ens import run as _run_ens
from layers.common.aoi_to_preemption_ens import run as _run_preemption_ens
from layers.compensation_zone_humide.aoi_to_zone_humide import run as _run_zone_humide
from layers.compensation_zone_humide.aoi_to_troncons_hydros import run as _run_troncons_hydros
from layers.compensation_zone_humide.aoi_to_surfaces_hydros import run as _run_surfaces_hydros
from layers.compensation_zone_humide.aoi_to_zones_humides_probables import (
    run as _run_zones_humides_probables,
)
from layers.compensation_zone_humide.aoi_to_bd_topo_et_cesbio import run as _run_bd_topo
from layers.enrich_uf import run as _run_enrich_uf


@dataclass
class LayerResult:
    layer_key: str
    table: str
    n_inserted: int = 0
    duration_s: float = 0.0
    skipped: bool = False
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.error is None


def _wrap(key: str, table: str, fn, engine, project_id: str, aoi_id: str, cb) -> LayerResult:
    t0 = time.perf_counter()
    try:
        n = fn(engine, project_id, aoi_id, cb) or 0
        return LayerResult(
            layer_key=key, table=table,
            n_inserted=n, duration_s=time.perf_counter() - t0,
            skipped=(n == 0),
        )
    except Exception as e:
        logger.exception("Erreur couche %s", key)
        return LayerResult(
            layer_key=key, table=table,
            duration_s=time.perf_counter() - t0, error=str(e),
        )


def _make(key, table, fn):
    def wrapped(engine, project_id, aoi_id, cb=None, *, min_area_ha: float | None = None):
        return _wrap(key, table, fn, engine, project_id, aoi_id, cb)
    wrapped.__name__ = f"layer_{key}"
    return wrapped


def _make_enrich_uf(key: str, table: str, fn):
    def wrapped(engine, project_id, aoi_id, cb=None, *, species_list=None):
        def inner(e, p, a, c):
            return fn(e, p, a, c, species_list=species_list)
        return _wrap(key, table, inner, engine, project_id, aoi_id, cb)
    wrapped.__name__ = f"layer_{key}"
    return wrapped


def _make_sub_uf(key: str, table: str, fn):
    def wrapped(
        engine,
        project_id,
        aoi_id,
        cb=None,
        *,
        min_area_ha: float | None = None,
        max_uf_parcelles: int | None = None,
    ):
        from db import get_engine_ppm
        engine_ppm = get_engine_ppm()
        t0 = time.perf_counter()
        try:
            kwargs: dict = {"engine_ppm": engine_ppm}
            if min_area_ha is not None:
                kwargs["min_area_ha"] = min_area_ha
            if max_uf_parcelles is not None:
                kwargs["max_uf_parcelles"] = max_uf_parcelles
            n = fn(engine, project_id, aoi_id, cb, **kwargs) or 0
            return LayerResult(
                layer_key=key, table=table,
                n_inserted=n, duration_s=time.perf_counter() - t0,
                skipped=(n == 0),
            )
        except Exception as e:
            logger.exception("Erreur couche %s", key)
            return LayerResult(
                layer_key=key, table=table,
                duration_s=time.perf_counter() - t0, error=str(e),
            )
    wrapped.__name__ = f"layer_{key}"
    return wrapped


LAYER_REGISTRY: list[dict] = [
    {
        "key": "sous_ensembles",
        "label": "Unités foncières (PPM → sous-ensembles)",
        "table": "ecocompensation_results.sous_ensembles",
        "fast": False,
        "fn": _make_sub_uf(
            "sous_ensembles",
            "ecocompensation_results.sous_ensembles",
            _run_sub_uf,
        ),
    },
    {
        "key": "enrich_uf",
        "label": "Flags UF (végétation + faune)",
        "table": "ecocompensation_results.sous_ensembles",
        "fast": False,
        "fn": _make_enrich_uf(
            "enrich_uf",
            "ecocompensation_results.sous_ensembles",
            _run_enrich_uf,
        ),
    },
    {
        "key": "parcelles",
        "label": "Parcelles cadastrales",
        "table": "ecocompensation_results.parcelles",
        "fast": True,
        "fn": _make("parcelles", "ecocompensation_results.parcelles", _run_parcelles),
    },
    {
        "key": "zone_humide",
        "label": "Zone humide",
        "table": "ecocompensation_results.zone_humide",
        "fast": True,
        "fn": _make("zone_humide", "ecocompensation_results.zone_humide", _run_zone_humide),
    },
    {
        "key": "espaces_naturels_sensibles_ens",
        "label": "Espaces naturels sensibles (ENS)",
        "table": "ecocompensation_results.espaces_naturels_sensibles_ens",
        "fast": True,
        "fn": _make(
            "espaces_naturels_sensibles_ens",
            "ecocompensation_results.espaces_naturels_sensibles_ens",
            _run_ens,
        ),
    },
    {
        "key": "preemption_ens",
        "label": "Préemption espaces naturels sensibles",
        "table": "ecocompensation_results.preemption_espaces_naturels_sensibles",
        "fast": True,
        "fn": _make(
            "preemption_ens",
            "ecocompensation_results.preemption_espaces_naturels_sensibles",
            _run_preemption_ens,
        ),
    },
    {
        "key": "troncons_hydros",
        "label": "Tronçons hydrographiques (BD TOPO national)",
        "table": "ecocompensation_results.troncons_hydros",
        "fast": True,
        "fn": _make(
            "troncons_hydros",
            "ecocompensation_results.troncons_hydros",
            _run_troncons_hydros,
        ),
    },
    {
        "key": "surfaces_hydros",
        "label": "Surfaces hydrographiques (BD TOPO national)",
        "table": "ecocompensation_results.surfaces_hydros",
        "fast": True,
        "fn": _make(
            "surfaces_hydros",
            "ecocompensation_results.surfaces_hydros",
            _run_surfaces_hydros,
        ),
    },
    {
        "key": "zones_humides_probables",
        "label": "Zones humides probables",
        "table": "ecocompensation_results.zones_humides_probables",
        "fast": False,
        "fn": _make(
            "zones_humides_probables",
            "ecocompensation_results.zones_humides_probables",
            _run_zones_humides_probables,
        ),
    },
    {
        "key": "bd_topo_et_cesbio",
        "label": "Occupation du sol (BD TOPO / CESBIO)",
        "table": "ecocompensation_results.bd_topo_et_cesbio",
        "fast": False,
        "fn": _make(
            "bd_topo_et_cesbio",
            "ecocompensation_results.bd_topo_et_cesbio",
            _run_bd_topo,
        ),
    },
]
