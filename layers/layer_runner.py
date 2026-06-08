#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
layer_runner.py
===============

Registre des couches. Chaque module aoi_to_*.py expose :
    run(engine, project_id: str, aoi_id: str, cb=None) -> int

Deux couches spéciales utilisent deux bases distinctes (core + PPM) :
    - unites_foncieres  : lit public.parcelles_personnes_morales (base PPM)
    - sous_ensembles    : lit ecocompensation_results.unites_foncieres (base core)
                          → dépend de unites_foncieres, placé juste après
"""
from __future__ import annotations
import time
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ── Imports modules couches ──────────────────────────────────────────────────

from layers.aoi_to_unites_foncieres       import run as _run_unites_foncieres
from layers.aoi_to_sous_ensembles         import run as _run_sous_ensembles
from layers.enrich_uf                     import run as _run_enrich_uf
from layers.aoi_to_parcelles_v2           import run as _run_parcelles
from layers.enrich_candidates             import run as _run_enrich_candidates
from layers.aoi_to_geomce                 import run as _run_geomce
from layers.aoi_to_zone_de_vegetation     import run as _run_zone_de_vegetation
from layers.aoi_to_carhab              import run as _run_carhab
from layers.aoi_to_zone_humide            import run as _run_zone_humide
from layers.aoi_to_remontee_de_nappes     import run as _run_remontee_de_nappes
from layers.aoi_to_troncons_hydro         import run as _run_troncons_hydro
from layers.aoi_to_routes                 import run as _run_routes
from layers.aoi_to_voies_ferrees          import run as _run_voies_ferrees
from layers.aoi_to_fragmentation_polygone import run as _run_fragmentation_polygone
from layers.aoi_to_zones_humides_probables import run as _run_zones_humides_probables
from layers.aoi_to_surfaces_hydro         import run as _run_surfaces_hydro
from layers.aoi_to_ebc                    import run as _run_ebc
from layers.aoi_to_natura_2000            import run as _run_natura2000
from layers.aoi_to_znieff                 import run as _run_znieff
from layers.aoi_to_reserves_naturelles_et_biologiques import (
    run as _run_reserves_naturelles,
)
from layers.aoi_to_sites_classes            import run as _run_sites_classes
from layers.aoi_to_arrachage_vignes       import run as _run_arrachage_vignes


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


# ── Wrappers ─────────────────────────────────────────────────────────────────

def _wrap(key: str, table: str, fn, engine, project_id: str, aoi_id: str, cb) -> LayerResult:
    """Exécute fn et retourne un LayerResult uniforme. Ne plante jamais."""
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
    """Fabrique une fonction (engine, project_id, aoi_id, cb) -> LayerResult."""
    def wrapped(engine, project_id, aoi_id, cb=None, *, min_area_ha: float | None = None):
        return _wrap(key, table, fn, engine, project_id, aoi_id, cb)
    wrapped.__name__ = f"layer_{key}"
    return wrapped


def _make_enrich(key: str, table: str, fn):
    """Wrapper enrich_candidates : passe species_list depuis l'orchestrateur."""
    def wrapped(engine, project_id, aoi_id, cb=None, *, species_list=None):
        def inner(e, p, a, c):
            return fn(e, p, a, c, species_list=species_list)
        return _wrap(key, table, inner, engine, project_id, aoi_id, cb)
    wrapped.__name__ = f"layer_{key}"
    return wrapped


def _make_enrich_uf(key: str, table: str, fn):
    """Wrapper enrich_uf : passe species_list depuis l'orchestrateur."""
    def wrapped(engine, project_id, aoi_id, cb=None, *, species_list=None):
        def inner(e, p, a, c):
            return fn(e, p, a, c, species_list=species_list)
        return _wrap(key, table, inner, engine, project_id, aoi_id, cb)
    wrapped.__name__ = f"layer_{key}"
    return wrapped


def _make_sous_ensembles(key: str, table: str, fn):
    """
    Comme _make, mais transmet ``max_uf_parcelles`` à ``run`` (sous-ensembles uniquement).
    Signature : (engine, project_id, aoi_id, cb=None, *, max_uf_parcelles=None) -> LayerResult
    """
    def wrapped(engine, project_id, aoi_id, cb=None, *, max_uf_parcelles: int | None = None):
        def inner(e, p, a, c):
            return fn(e, p, a, c, max_uf_parcelles=max_uf_parcelles)
        return _wrap(key, table, inner, engine, project_id, aoi_id, cb)
    wrapped.__name__ = f"layer_{key}"
    return wrapped


def _make_ppm(key, table, fn):
    """
    Wrapper pour les couches nécessitant engine_ppm (base PPM séparée).
    Instancie get_engine_ppm() à la volée — pas de changement sur les
    autres layers, pas d'import circulaire au module level.
    """
    def wrapped(engine, project_id, aoi_id, cb=None, *, min_area_ha: float | None = None):
        from db import get_engine_ppm
        engine_ppm = get_engine_ppm()
        t0 = time.perf_counter()
        try:
            kwargs = {"engine_ppm": engine_ppm}
            if min_area_ha is not None:
                kwargs["min_area_ha"] = min_area_ha
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


# ── Registre ─────────────────────────────────────────────────────────────────
# Ordre d'exécution :
#   1. unites_foncieres  — lit PPM (base séparée), indépendant des autres couches
#   2. sous_ensembles    — dépend de unites_foncieres, doit venir juste après
#   3-N. Couches SIG     — indépendantes entre elles
#
# fast=True  : SQL direct, quelques secondes
# fast=False : fetch WFS ou calcul lourd, peut durer plusieurs minutes

LAYER_REGISTRY: list[dict] = [
    # ── Unités foncières (calcul dérivé PPM) ────────────────────────────
    {
        "key":   "unites_foncieres",
        "label": "Unités foncières (personnes morales)",
        "table": "ecocompensation_results.unites_foncieres",
        "fast":  False,
        # Utilise _make_ppm : engine_ppm injecté automatiquement
        "fn":    _make_ppm("unites_foncieres", "ecocompensation_results.unites_foncieres", _run_unites_foncieres),
    },
    {
        "key":   "sous_ensembles",
        "label": "Sous-ensembles UF (k=2..5)",
        "table": "ecocompensation_results.sous_ensembles",
        "fast":  False,
        # Dépend de unites_foncieres — placé juste après, vérifie lui-même la dépendance
        "fn":    _make_sous_ensembles("sous_ensembles", "ecocompensation_results.sous_ensembles", _run_sous_ensembles),
    },
    {
        "key":   "enrich_uf",
        "label": "Flags UF (végétation + faune)",
        "table": "ecocompensation_results.sous_ensembles",
        "fast":  False,
        "fn":    _make_enrich_uf(
            "enrich_uf",
            "ecocompensation_results.sous_ensembles",
            _run_enrich_uf,
        ),
    },

    # ── Couches SIG (ordre inchangé) ────────────────────────────────────
    {
        "key":   "parcelles",
        "label": "Parcelles cadastrales",
        "table": "ecocompensation_results.parcelles",
        "fast":  True,
        "fn":    _make("parcelles", "ecocompensation_results.parcelles", _run_parcelles),
    },
    {
        "key":   "enrich_candidates",
        "label": "Flags candidats (végétation + faune)",
        "table": "ecocompensation_results.parcelles",
        "fast":  True,
        "fn":    _make_enrich(
            "enrich_candidates",
            "ecocompensation_results.parcelles",
            _run_enrich_candidates,
        ),
    },
    {
        "key":   "geomce",
        "label": "Mesures compensatoires GEOMCE",
        "table": "ecocompensation_results.mesures_compensatoire_*",
        "fast":  True,
        "fn":    _make("geomce", "ecocompensation_results.mesures_compensatoire_*", _run_geomce),
    },
    {
        "key":   "zone_de_vegetation",
        "label": "Zone de végétation",
        "table": "ecocompensation_results.zone_de_vegetation",
        "fast":  True,
        "fn":    _make("zone_de_vegetation", "ecocompensation_results.zone_de_vegetation", _run_zone_de_vegetation),
    },
    {
        "key":   "carhab",
        "label": "Habitats Carhab (EUNIS / biotope / physio)",
        "table": "ecocompensation_results.carhab",
        "fast":  True,
        "fn":    _make("carhab", "ecocompensation_results.carhab", _run_carhab),
    },
    {
        "key":   "zone_humide",
        "label": "Zone humide",
        "table": "ecocompensation_results.zone_humide",
        "fast":  True,
        "fn":    _make("zone_humide", "ecocompensation_results.zone_humide", _run_zone_humide),
    },
    {
        "key":   "remontee_de_nappes",
        "label": "Remontée de nappes",
        "table": "ecocompensation_results.remontee_de_nappes",
        "fast":  True,
        "fn":    _make(
            "remontee_de_nappes",
            "ecocompensation_results.remontee_de_nappes",
            _run_remontee_de_nappes,
        ),
    },
    {
        "key":   "troncons_hydro",
        "label": "Tronçons hydrographiques",
        "table": "ecocompensation_results.troncons_hydro",
        "fast":  False,
        "fn":    _make("troncons_hydro", "ecocompensation_results.troncons_hydro", _run_troncons_hydro),
    },
    {
        "key":   "routes",
        "label": "Tronçons de route",
        "table": "ecocompensation_results.routes",
        "fast":  False,
        "fn":    _make("routes", "ecocompensation_results.routes", _run_routes),
    },
    {
        "key":   "voies_ferrees",
        "label": "Tronçons de voie ferrée",
        "table": "ecocompensation_results.voies_ferrees",
        "fast":  False,
        "fn":    _make("voies_ferrees", "ecocompensation_results.voies_ferrees", _run_voies_ferrees),
    },
    {
        "key":   "fragmentation",
        "label": "Fragmentation (polygones)",
        "table": "ecocompensation_results.fragmentation_polygons",
        "fast":  False,
        "fn":    _make("fragmentation", "ecocompensation_results.fragmentation_polygons", _run_fragmentation_polygone),
    },
    {
        "key":   "zones_humides_probables",
        "label": "Zones humides probables",
        "table": "ecocompensation_results.zones_humides_probables",
        "fast":  False,
        "fn":    _make("zones_humides_probables", "ecocompensation_results.zones_humides_probables", _run_zones_humides_probables),
    },
    {
        "key":   "surfaces_hydro",
        "label": "Surfaces hydrographiques",
        "table": "ecocompensation_results.surfaces_hydro",
        "fast":  False,
        "fn":    _make("surfaces_hydro", "ecocompensation_results.surfaces_hydro", _run_surfaces_hydro),
    },
    {
        "key":   "ebc",
        "label": "Espaces Boisés Classés",
        "table": "ecocompensation_results.ebc",
        "fast":  False,
        "fn":    _make("ebc", "ecocompensation_results.ebc", _run_ebc),
    },
    {
        "key":   "natura2000",
        "label": "Natura 2000 — SIC & ZPS (Patrinat)",
        "table": "ecocompensation_results.natura2000",
        "fast":  False,
        "fn":    _make("natura2000", "ecocompensation_results.natura2000", _run_natura2000),
    },
    {
        "key":   "znieff",
        "label": "ZNIEFF",
        "table": "ecocompensation_results.znieff",
        "fast":  False,
        "fn":    _make("znieff", "ecocompensation_results.znieff", _run_znieff),
    },
    {
        "key":   "reserves_naturelles",
        "label": "Réserves naturelles & biologiques (Patrinat)",
        "table": "ecocompensation_results.reserves_naturelles",
        "fast":  False,
        "fn":    _make(
            "reserves_naturelles",
            "ecocompensation_results.reserves_naturelles",
            _run_reserves_naturelles,
        ),
    },
    {
        "key":   "sites_classes",
        "label": "Sites classés (Patrinat)",
        "table": "ecocompensation_results.sites_classes",
        "fast":  False,
        "fn":    _make(
            "sites_classes",
            "ecocompensation_results.sites_classes",
            _run_sites_classes,
        ),
    },
    {
        "key":   "arrachage_vignes",
        "label": "Arrachage de vignes",
        "table": "ecocompensation_results.arrachage_vignes",
        "fast":  False,
        "fn":    _make("arrachage_vignes", "ecocompensation_results.arrachage_vignes", _run_arrachage_vignes),
    },
]