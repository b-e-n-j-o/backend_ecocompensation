"""
params.py — paramètres de la phase UF, dérivés de la FilterConfig du filtre parcelles.

Toute la config du filtre est transmise (exclusions, toutes les espèces, flore, ZH,
hydro…) : plus de sous-ensemble de paramètres recopié à la main.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import text

from api.recherche_de_foncier.filtrage.layers.filter_pipeline import (
    FaunaCriterion,
    FilterConfig,
    FloraCriterion,
)
from api.recherche_de_foncier.filtrage.layers.national_exclusions import DEFAULT_EXCLUDED_LAYERS

from .evaluation import Regles


@dataclass
class UfParams:
    config: FilterConfig
    # CESBIO : True = chaque parcelle doit porter l'occupation (éliminatoire, recommandé)
    #          False = au moins une parcelle du terrain (ancien comportement sur l'union)
    cesbio_par_parcelle: bool = True
    # Miller sur l'union du terrain : affiché toujours, bloquant seulement si True
    miller_sur_terrain: bool = False
    min_parcelles: int = 2
    limit_uf: int = 100
    siren: str | None = None          # debug CLI : une seule personne morale

    @property
    def study_type(self) -> str:
        return "zones_humides_intra" if self.config.search_mode == "within_foncier" else "faune_buffer"

    @property
    def min_area_m2(self) -> float:
        return float(self.config.min_area_ha) * 10_000.0

    @property
    def need_zone_humide(self) -> bool:
        return (self.config.zone_humide_mode in ("intersect", "exclude")
                or self.study_type == "zones_humides_intra")

    @property
    def fauna_criteria_dicts(self) -> list[dict]:
        return [{"species": fc.species, "dist_m": fc.dist_m} for fc in self.config.fauna_criteria]

    def regles(self) -> Regles:
        c = self.config
        return Regles(
            min_area_m2=self.min_area_m2,
            fauna_combine=c.fauna_combine, fauna_joins=list(c.fauna_joins),
            flora_combine=c.flora_combine, flora_joins=list(c.flora_joins),
            bio_combine=c.bio_combine,
            min_zh_m2=float(c.min_zone_humide_ha or 0) * 10_000.0,
            miller_thresh=c.miller_thresh,
            miller_sur_terrain=self.miller_sur_terrain,
        )


def params_from_config(config: FilterConfig, **overrides) -> UfParams:
    return UfParams(config=config, **overrides)


def _combine(raw) -> str:
    return "and" if str(raw or "").lower() == "and" else "or"


def config_from_dict(raw: dict | None) -> FilterConfig:
    """Inverse de filter_pipeline.filter_config_dict (projects.filter_config)."""
    raw = raw or {}
    fauna = [
        FaunaCriterion(
            species=str(c.get("species") or "").strip(),
            dist_m=float(c.get("dist_m") or 0),
            aliases=[str(a) for a in (c.get("aliases") or []) if str(a).strip()],
        )
        for c in raw.get("fauna_criteria") or []
        if isinstance(c, dict) and str(c.get("species") or "").strip()
    ]
    flora = [
        FloraCriterion(
            taxon_key=str(c["taxon_key"]),
            label=str(c.get("label") or c["taxon_key"]),
            dist_m=float(c.get("dist_m") or 0),
        )
        for c in raw.get("flora_criteria") or []
        if isinstance(c, dict) and c.get("taxon_key")
    ]
    return FilterConfig(
        min_area_ha=float(raw.get("min_area_ha") if raw.get("min_area_ha") is not None else 7.0),
        miller_thresh=raw.get("miller_thresh"),
        cesbio_libelles=list(raw.get("cesbio_libelles") or []),
        fauna_criteria=fauna,
        fauna_combine=_combine(raw.get("fauna_combine")),
        fauna_joins=list(raw.get("fauna_joins") or []),
        flora_criteria=flora,
        flora_combine=_combine(raw.get("flora_combine")),
        flora_joins=list(raw.get("flora_joins") or []),
        bio_combine=_combine(raw.get("bio_combine")),
        search_mode=str(raw.get("search_mode") or "exclude_source_buffer"),
        zone_humide_mode=str(raw.get("zone_humide_mode") or "ignore"),
        zones_humides_probables_mode=str(raw.get("zones_humides_probables_mode") or "ignore"),
        min_zone_humide_ha=float(raw.get("min_zone_humide_ha") or 0),
        # liste vide = aucune exclusion (choix utilisateur) ; clé absente = défaut
        excluded_layers=(list(raw["excluded_layers"]) if "excluded_layers" in raw
                         else list(DEFAULT_EXCLUDED_LAYERS)),
        troncons_hydros_max_dist_m=raw.get("troncons_hydros_max_dist_m"),
        surfaces_hydros_max_dist_m=raw.get("surfaces_hydros_max_dist_m"),
        same_bassin_versant=bool(raw.get("same_bassin_versant")),
        arrachage_vignes=bool(raw.get("arrachage_vignes")),
        arrachage_vignes_types=list(raw.get("arrachage_vignes_types") or []),
        include_unites_foncieres=bool(raw.get("include_unites_foncieres", True)),
        include_ilots_rpg=bool(raw.get("include_ilots_rpg", False)),
    )


def load_project_config(engine, project_id: str) -> FilterConfig:
    """FilterConfig du dernier filtre lancé (filter_config, sinon last_filter)."""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT filter_config, last_filter FROM ecocompensation.projects "
                 "WHERE id = CAST(:pid AS uuid)"),
            {"pid": project_id},
        ).mappings().one_or_none()
    if not row:
        raise RuntimeError(f"Projet introuvable : {project_id}")
    raw = row["filter_config"] or row["last_filter"] or {}
    if isinstance(raw, str):
        raw = json.loads(raw)
    return config_from_dict(raw)
