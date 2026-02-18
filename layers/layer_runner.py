#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
layer_runner.py
===============

Registre des couches. Chaque module aoi_to_*.py expose :
    run(engine, aoi_id: str, cb=None) -> int

Ce fichier ne contient AUCUNE logique métier.
"""
from __future__ import annotations
import time
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ── Imports modules couches ──────────────────────────────────────────────────

from layers.aoi_to_parcelles             import run as _run_parcelles
from layers.aoi_to_geomce                import run as _run_geomce
from layers.aoi_to_zone_de_vegetation    import run as _run_zone_de_vegetation
from layers.aoi_to_zone_humide           import run as _run_zone_humide
from layers.aoi_to_troncons_hydro        import run as _run_troncons_hydro
from layers.aoi_to_surfaces_hydro        import run as _run_surfaces_hydro
from layers.aoi_to_surfaces_elementaires import run as _run_surfaces_elementaires
from layers.aoi_to_ebc                   import run as _run_ebc
from layers.aoi_to_patrimoine_naturel    import run as _run_patrimoine_naturel
from layers.aoi_to_arrachage_vignes    import run as _run_arrachage_vignes


# ── LayerResult ──────────────────────────────────────────────────────────────

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


def _wrap(key: str, table: str, fn, engine, aoi_id: str, cb) -> LayerResult:
    """Exécute fn et retourne un LayerResult uniforme. Ne plante jamais."""
    t0 = time.perf_counter()
    try:
        n = fn(engine, aoi_id, cb) or 0
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
    """Fabrique une fonction (engine, aoi_id, cb) -> LayerResult."""
    def wrapped(engine, aoi_id, cb=None):
        return _wrap(key, table, fn, engine, aoi_id, cb)
    wrapped.__name__ = f"layer_{key}"
    return wrapped


# ── Registre ─────────────────────────────────────────────────────────────────
# fast=True  : SQL direct, quelques secondes
# fast=False : fetch WFS, peut durer plusieurs minutes

LAYER_REGISTRY: list[dict] = [
    {"key": "parcelles",             "label": "Parcelles cadastrales",          "table": "ecocompensation_results.parcelles",              "fast": True,  "fn": _make("parcelles",             "ecocompensation_results.parcelles",              _run_parcelles)},
    {"key": "geomce",                "label": "Mesures compensatoires GEOMCE",  "table": "ecocompensation_results.mesures_compensatoire_*", "fast": True,  "fn": _make("geomce",                "ecocompensation_results.mesures_compensatoire_*", _run_geomce)},
    {"key": "zone_de_vegetation",    "label": "Zone de végétation",             "table": "ecocompensation_results.zone_de_vegetation",      "fast": True,  "fn": _make("zone_de_vegetation",    "ecocompensation_results.zone_de_vegetation",      _run_zone_de_vegetation)},
    {"key": "zone_humide",           "label": "Zone humide",                    "table": "ecocompensation_results.zone_humide",             "fast": True,  "fn": _make("zone_humide",           "ecocompensation_results.zone_humide",             _run_zone_humide)},
    {"key": "troncons_hydro",        "label": "Tronçons hydrographiques",       "table": "ecocompensation_results.troncons_hydro",          "fast": False, "fn": _make("troncons_hydro",        "ecocompensation_results.troncons_hydro",          _run_troncons_hydro)},
    {"key": "surfaces_hydro",        "label": "Surfaces hydrographiques",       "table": "ecocompensation_results.surfaces_hydro",          "fast": False, "fn": _make("surfaces_hydro",        "ecocompensation_results.surfaces_hydro",          _run_surfaces_hydro)},
    {"key": "surfaces_elementaires", "label": "Surfaces élémentaires Sandre",   "table": "ecocompensation_results.surfaces_elementaires",   "fast": False, "fn": _make("surfaces_elementaires", "ecocompensation_results.surfaces_elementaires",   _run_surfaces_elementaires)},
    {"key": "ebc",                   "label": "Espaces Boisés Classés",         "table": "ecocompensation_results.ebc",                     "fast": False, "fn": _make("ebc",                   "ecocompensation_results.ebc",                     _run_ebc)},
    {"key": "patrimoine_naturel",    "label": "Patrimoine naturel","table": "ecocompensation_results.patrimoine_naturel",      "fast": False, "fn": _make("patrimoine_naturel",    "ecocompensation_results.patrimoine_naturel",      _run_patrimoine_naturel)},
    {"key": "arrachage_vignes",    "label": "Arrachage de vignes",    "table": "ecocompensation_results.arrachage_vignes",      "fast": False, "fn": _make("arrachage_vignes",    "ecocompensation_results.arrachage_vignes",      _run_arrachage_vignes)},
]