"""study_type sur ecocompensation.projects : recherche vs analyse de foncier."""

from __future__ import annotations

KIND_RECHERCHE = "recherche"
KIND_ANALYSE = "analyse"

# Anciennes valeurs, désormais assimilées à une recherche.
_LEGACY_RECHERCHE = frozenset({
    "faune_buffer",
    "zones_humides_intra",
    "recherche_de_foncier",
    "",
})


def normalize_project_kind(raw: str | None) -> str:
    value = str(raw or "").strip()
    if value in {KIND_ANALYSE, "analyse_de_foncier"}:
        return KIND_ANALYSE
    return KIND_RECHERCHE


def ecological_study_type(raw: str | None) -> str:
    """Profilers pool : ZH seulement si l'ancien tag est encore stocké sur le run."""
    value = str(raw or "").strip()
    if value == "zones_humides_intra":
        return "zones_humides_intra"
    return "faune_buffer"


def project_kind_filter_values(raw: str | None) -> list[str] | None:
    """Valeurs SQL à matcher pour un filtre d'API (None = pas de filtre)."""
    if not raw:
        return None
    kind = normalize_project_kind(raw)
    if kind == KIND_ANALYSE:
        return [KIND_ANALYSE, "analyse_de_foncier"]
    return [KIND_RECHERCHE, "recherche_de_foncier", "faune_buffer", "zones_humides_intra"]
