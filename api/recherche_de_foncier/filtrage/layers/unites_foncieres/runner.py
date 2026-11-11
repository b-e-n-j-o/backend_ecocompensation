"""
runner.py — phase « unités foncières PM » lancée après le filtre parcelles.

Même forme que rpg_ilots/runner.py. Ne touche PAS à projects.status (le filtre a déjà
mis 'ready') : il n'envoie que des événements WS.

Événements WS : progress (layer_key="unites_foncieres"), phase:uf_ready, error.
Résultat : ecocompensation.projects.last_results_uf (jsonb, version 2).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

from . import build, enrich, pool
from .params import UfParams, params_from_config

logger = logging.getLogger(__name__)
LAYER_KEY = "unites_foncieres"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_sync(engine, project_id: str, aoi_id: str, params: UfParams,
             cb: Callable[[str], None] | None = None, *, engine_ppm=None, save: bool = True) -> dict:
    """Chaîne complète synchrone : build -> enrich -> réponse -> sauvegarde."""
    diag = build.run(engine, project_id, aoi_id, params, cb, engine_ppm=engine_ppm)
    enrich.run(engine, project_id, params, cb)
    result = pool.build_response(engine, project_id, params, diagnostic=diag)
    if save:
        pool.save_response(engine, project_id, result)
    return result


async def run_uf_after_filter(engine, project_id: str, aoi_id: str, config,
                              push: Callable[[dict], Awaitable[None]], *, engine_ppm=None) -> None:
    params = params_from_config(config)
    loop = asyncio.get_running_loop()
    logger.info("[uf] START project_id=%s", project_id)

    def cb(msg: str) -> None:
        asyncio.run_coroutine_threadsafe(
            push({"event": "progress", "layer_key": LAYER_KEY, "message": msg, "timestamp": _now()}), loop)

    try:
        await push({"event": "uf_start", "layer_key": LAYER_KEY,
                    "message": "Démarrage calcul unités foncières", "timestamp": _now()})
        result = await loop.run_in_executor(
            None, lambda: run_sync(engine, project_id, aoi_id, params, cb, engine_ppm=engine_ppm))
        await push({"event": "phase:uf_ready", "layer_key": LAYER_KEY,
                    "message": f"Unités foncières disponibles ({result['total_uf']})", "timestamp": _now()})
        logger.info("[uf] DONE project_id=%s total=%s", project_id, result["total_uf"])
    except Exception:
        logger.exception("[uf] FAILED project_id=%s", project_id)
        await push({"event": "error", "layer_key": LAYER_KEY,
                    "message": "Erreur calcul unités foncières (voir logs serveur)", "timestamp": _now()})
