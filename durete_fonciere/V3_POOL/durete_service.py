from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .batch_durete import traiter_siren_avec_retry, run_batch, lire_input


def run_durete_for_siren(
    siren: str,
    idus: list[str] | None = None,
    avec_rpg: bool = True,
    model: str = "gemini-3.1-flash-lite-preview",
    denomination: str = "",
    forme_juridique: str = "",
) -> dict:
    """
    Facade simple pour lancer le pipeline dureté foncière sur une seule PM.

    - `siren` : SIREN de la personne morale
    - `idus` : liste d'IDU ; si vide / None, elles seront résolues via Supabase
    """
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
) -> list[dict]:
    """
    Variante « in-memory » de run_batch : au lieu de lire/écrire des fichiers,
    on alimente le pipeline avec une liste de dicts au même format que `lire_input`
    et on renvoie la liste complète des résultats.
    """
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

