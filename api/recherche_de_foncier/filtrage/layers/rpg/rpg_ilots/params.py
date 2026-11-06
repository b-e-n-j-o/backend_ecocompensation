"""Paramètres du module îlots RPG (indépendants de FilterConfig pour pouvoir lancer en CLI)."""
from __future__ import annotations

from dataclasses import dataclass, field

SRC_SCHEMA = "ecocompensation"
RES_SCHEMA = "ecocompensation_results"
RES_TABLE = f"{RES_SCHEMA}.ilots_rpg"


@dataclass
class IlotsRpgParams:
    millesime: str = "2025"
    min_area_ha: float = 0.0
    min_parcelles: int = 2              # un îlot d'une seule parcelle est déjà dans le pool parcelles
    miller_thresh: float = 0.0
    cesbio_libelles: list[str] = field(default_factory=list)
    fauna_criteria: list[dict] = field(default_factory=list)   # [{"species": ..., "dist_m": ...}]
    study_type: str = "faune_buffer"    # ou "zones_humides_intra"
    min_zone_humide_ha: float = 0.0
    limit: int = 100

    @property
    def ilots_table(self) -> str:
        return f"{SRC_SCHEMA}.rpg{self.millesime}_ilots"

    @property
    def membre_table(self) -> str:
        return f"{SRC_SCHEMA}.rpg{self.millesime}_ilot_membre"

    @property
    def fauna_species(self) -> list[str]:
        return [c["species"] for c in self.fauna_criteria if str(c.get("species") or "").strip()]


def params_from_config(config, millesime: str = "2025") -> IlotsRpgParams:
    """Construit les paramètres depuis le FilterConfig du filtre parcelles (mêmes seuils que les UF PM)."""
    return IlotsRpgParams(
        millesime=millesime,
        min_area_ha=float(config.min_area_ha or 0),
        miller_thresh=float(config.miller_thresh) if config.miller_thresh is not None else 0.0,
        cesbio_libelles=list(config.cesbio_libelles or []),
        fauna_criteria=[
            {"species": fc.species, "dist_m": fc.dist_m}
            for fc in config.fauna_criteria if fc.species.strip()
        ],
        study_type="zones_humides_intra" if config.search_mode == "within_foncier" else "faune_buffer",
        min_zone_humide_ha=float(config.min_zone_humide_ha or 0),
    )
