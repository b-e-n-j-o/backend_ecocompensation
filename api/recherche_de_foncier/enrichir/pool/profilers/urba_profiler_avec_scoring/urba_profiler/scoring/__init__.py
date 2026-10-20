"""
scoring — Score urbanistique d'une parcelle / UF pour la compensation écologique.

    from urba_profiler.scoring import score_cible
    score = score_cible(cible, profil="zones_humides")

Règles : regles_compensation.yaml (éditable). Documentation générée :
    python -m urba_profiler.scoring.doc
"""

from .loader import get_rules, load_rules


def __getattr__(name):  # import paresseux : évite l'import circulaire avec ..schemas
    if name in ("score_cible", "interpret", "resolve_zone_type"):
        from . import engine

        return getattr(engine, name)
    raise AttributeError(name)


__all__ = ["score_cible", "interpret", "resolve_zone_type", "get_rules", "load_rules"]
