"""
criteres.py — critères UF = mêmes clauses SQL que le filtre parcelles, avec leur type.

Source unique : filter_pipeline.build_filter_clauses (alias p, colonne p.geom_2154).
On n'y ajoute que :
  - le découpage faune / flore en une colonne par espèce (au lieu de la clause combinée),
  - la surface de zone humide en m² (critère additif) quand min_zone_humide_ha > 0.
"""
from __future__ import annotations

import logging
from typing import Callable

from api.recherche_de_foncier.filtrage.layers.faune import fauna_exists_sql
from api.recherche_de_foncier.filtrage.layers.filter_pipeline import (
    FilterConfig,
    build_filter_clauses,
    filter_params,
    flora_exists_sql,
    resolve_spatial_mode,
)
from api.recherche_de_foncier.filtrage.layers.national_exclusions import national_exclusion_steps

from .evaluation import ELIMINATOIRE, EXISTENCE, FAUNE, FLORE, ZH_SURFACE, Critere

logger = logging.getLogger(__name__)

# Clés gérées ailleurs : surface et Miller en Python sur le terrain, faune/flore éclatées
_HORS_SQL = {"min_area_ha", "miller_thresh", "fauna_criteria", "flora_criteria", "bio_combine"}

ZH_AREA_SQL = """
    COALESCE((
        SELECT SUM(ST_Area(ST_Intersection(p.geom_2154, zh.geom_2154)))
        FROM ecocompensation_results.zone_humide zh
        WHERE zh.project_id = CAST(:project_id AS uuid)
          AND zh.geom_2154 IS NOT NULL
          AND p.geom_2154 && zh.geom_2154
          AND ST_Intersects(p.geom_2154, zh.geom_2154)
    ), 0)
"""


def kind_for(key: str, config: FilterConfig, *, national_keys: set[str],
             cesbio_par_parcelle: bool) -> str | None:
    if key in _HORS_SQL:
        return None
    if key in national_keys:
        return ELIMINATOIRE
    if key == "search_mode":
        return ELIMINATOIRE if config.search_mode != "within_foncier" else EXISTENCE
    if key == "cesbio_libelles":
        return ELIMINATOIRE if cesbio_par_parcelle else EXISTENCE
    if key == "zone_humide_mode":
        if config.zone_humide_mode == "exclude":
            return ELIMINATOIRE
        return ZH_SURFACE if config.min_zone_humide_ha > 0 else EXISTENCE
    if key == "zones_humides_probables_mode":
        return ELIMINATOIRE if config.zones_humides_probables_mode == "exclude" else EXISTENCE
    # same_bassin_versant, troncons/surfaces hydro, arrachage_vignes : intersection / distance
    return EXISTENCE


def build_criteres(
    engine,
    project_id: str,
    config: FilterConfig,
    *,
    cesbio_par_parcelle: bool = True,
    log: Callable[[str], None] | None = None,
) -> tuple[list[Critere], dict]:
    """Retourne (critères ordonnés, paramètres SQL communs)."""
    log = log or logger.info
    spatial_mode = resolve_spatial_mode(engine, project_id, config, log)
    keys, labels, clauses = build_filter_clauses(config, spatial_mode=spatial_mode)
    national_keys = {k for k, _l, _c in national_exclusion_steps(config.excluded_layers)}

    out: list[Critere] = []
    for key, label, clause in zip(keys, labels, clauses):
        kind = kind_for(key, config, national_keys=national_keys,
                        cesbio_par_parcelle=cesbio_par_parcelle)
        if kind is None:
            continue
        out.append(Critere(key, label, kind, ZH_AREA_SQL if kind == ZH_SURFACE else clause))

    for i, fc in enumerate(config.fauna_criteria):
        out.append(Critere(f"faune_{i}", f"Faune : {fc.species} ≤ {fc.dist_m:g} m",
                           FAUNE, fauna_exists_sql("p.geom_2154", i)))
    for i, fc in enumerate(config.flora_criteria):
        out.append(Critere(f"flore_{i}", f"Flore : {fc.label or fc.taxon_key} ≤ {fc.dist_m:g} m",
                           FLORE, flora_exists_sql("p.geom_2154", i)))

    # éliminatoires d'abord : ils réduisent le nombre de parcelles à tester ensuite
    out.sort(key=lambda c: 0 if c.kind == ELIMINATOIRE else 1)
    return out, filter_params(project_id, config)
