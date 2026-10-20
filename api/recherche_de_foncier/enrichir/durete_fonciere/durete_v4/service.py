# -*- coding: utf-8 -*-
"""
Façade backend — mêmes signatures que `durete_service.py` V3 (drop-in) +
options V4 en arguments nommés.

    from durete_v4.service import run_durete_for_siren
    res = run_durete_for_siren("952824076", ["33213000BP0018"])            # comme avant
    res = run_durete_for_siren("952824076", avec_recherche_web=True)       # + web sourcé
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Iterable, Optional

from . import config
from .pipeline import analyser_pm

LOGGERS = ("durete",)


def _verbose(verbose: Optional[bool]) -> bool:
    if verbose is not None:
        return bool(verbose)
    return str(os.getenv("DURETE_VERBOSE", "")).strip().lower() in {"1", "true", "yes", "on"}


def configure_logging(verbose: Optional[bool]) -> None:
    lvl = logging.INFO if _verbose(verbose) else logging.WARNING
    for name in LOGGERS:
        logging.getLogger(name).setLevel(lvl)
    # La recherche web et les étapes du pipeline restent visibles même sans DURETE_VERBOSE.
    logging.getLogger("durete.pipeline").setLevel(logging.INFO)
    logging.getLogger("durete.web").setLevel(logging.INFO)
    logging.getLogger("durete.service").setLevel(logging.INFO)


def resolve_durete_gemini_model(model: Optional[str] = None) -> str:
    return (model or "").strip() or config.GEMINI_MODEL


def run_durete_for_siren(
    siren: str,
    idus: Optional[list[str]] = None,
    avec_rpg: bool = True,
    model: Optional[str] = None,
    denomination: str = "",
    forme_juridique: str = "",
    verbose: Optional[bool] = None,
    **options_v4: Any,
) -> dict:
    """
    options_v4 : avec_gpu, avec_recherche_web, avec_narration, avec_sirene,
                 llm_provider ('gemini'|'mistral'|'none'), domaine_public_confirme, sources, llm.
    Ne lève jamais : renvoie {"statut": "erreur", ...} en cas d'échec.
    """
    configure_logging(verbose)
    options_v4.setdefault("avec_recherche_web", True)
    logging.getLogger("durete.service").info(
        "DURETE V4 APPEL | siren=%s idus=%s web=%s narration=%s provider=%s",
        siren,
        len(idus or []),
        options_v4.get("avec_recherche_web"),
        options_v4.get("avec_narration", True),
        options_v4.get("llm_provider") or config.LLM_PROVIDER,
    )
    try:
        return analyser_pm(siren, idus or None, avec_rpg=avec_rpg, model=model,
                           denomination_input=denomination, forme_juridique_input=forme_juridique,
                           **options_v4)
    except Exception as e:  # noqa: BLE001
        logging.getLogger("durete.service").exception("Échec dureté %s", siren)
        return {"siren": siren, "idus": idus or [], "statut": "erreur", "erreur": str(e),
                "ts": datetime.now().isoformat(), "version": "4"}


def run_durete_batch_in_memory(
    items: Iterable[dict[str, Any]],
    *,
    avec_rpg: bool = True,
    model: Optional[str] = None,
    workers: int = 1,
    verbose: Optional[bool] = None,
    **options_v4: Any,
) -> list[dict]:
    """Même contrat que V3 ; `workers` est maintenant réellement utilisé (ordre d'entrée conservé)."""
    configure_logging(verbose)

    def one(item: dict) -> dict:
        return run_durete_for_siren(
            str(item.get("siren", "")), item.get("idus") or ([item["idu"]] if item.get("idu") else None),
            avec_rpg=avec_rpg, model=model,
            denomination=item.get("denomination", "") or "",
            forme_juridique=item.get("forme_juridique", "") or "",
            verbose=verbose, **options_v4,
        )

    items = list(items)
    if workers <= 1:
        return [one(i) for i in items]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(one, items))
