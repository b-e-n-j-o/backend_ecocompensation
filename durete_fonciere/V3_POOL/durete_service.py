from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Iterable

from .batch_durete import traiter_siren_avec_retry, run_batch, lire_input


def _durete_verbose_enabled(verbose: bool | None) -> bool:
    """
    Détermine si les logs détaillés dureté doivent être actifs.
    Priorité:
      1) paramètre explicite `verbose`
      2) variable d'env DURETE_VERBOSE (1/true/yes/on)
      3) défaut = False
    """
    if verbose is not None:
        return bool(verbose)
    raw = str(os.getenv("DURETE_VERBOSE", "")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _configure_durete_logging(verbose: bool | None) -> None:
    """Règle le niveau de logs des modules dureté (silencieux par défaut)."""
    lvl = logging.INFO if _durete_verbose_enabled(verbose) else logging.WARNING
    for name in (
        "scoring",
        "batch_durete",
        "annuaire",
        "bodacc",
        "dvf",
        "rpg",
        "sirene",
    ):
        logging.getLogger(name).setLevel(lvl)


def run_durete_for_siren(
    siren: str,
    idus: list[str] | None = None,
    avec_rpg: bool = True,
    model: str = "gemini-3.1-flash-lite-preview",
    denomination: str = "",
    forme_juridique: str = "",
    verbose: bool | None = None,
) -> dict:
    """
    Facade simple pour lancer le pipeline dureté foncière sur une seule PM.

    - `siren` : SIREN de la personne morale
    - `idus` : liste d'IDU ; si vide / None, elles seront résolues via Supabase
    """
    _configure_durete_logging(verbose)
    return traiter_siren_avec_retry(
        siren=siren,
        idus=idus or [],
        avec_rpg=avec_rpg,
        model=model,
        denomination_input=denomination,
        forme_juridique_input=forme_juridique,
    )


def run_durete_batch_in_memory(
    items: Iterable[dict[str, Any]],
    *,
    avec_rpg: bool = True,
    model: str = "gemini-3.1-flash-lite-preview",
    workers: int = 1,
    verbose: bool | None = None,
) -> list[dict]:
    """
    Variante « in-memory » de run_batch : au lieu de lire/écrire des fichiers,
    on alimente le pipeline avec une liste de dicts au même format que `lire_input`
    et on renvoie la liste complète des résultats.
    """
    _configure_durete_logging(verbose)

    # Pour rester DRY, on réutilise la logique de `traiter_siren_avec_retry`.
    # On ne persiste rien en disque ici ; c'est la responsabilité de l'appelant.
    results: list[dict] = []

    # Mode séquentiel (simple et sûr vis-à-vis des quotas API)
    for item in items:
        res = traiter_siren_avec_retry(
            siren=item.get("siren", ""),
            idus=item.get("idus") or [],
            avec_rpg=avec_rpg,
            model=model,
            denomination_input=item.get("denomination", "") or "",
            forme_juridique_input=item.get("forme_juridique", "") or "",
        )
        results.append(res)

    return results

