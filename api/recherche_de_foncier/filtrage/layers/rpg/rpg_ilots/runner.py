"""
runner.py — phase « îlots agricoles RPG » lancée par filter_orchestrator après le filtre parcelles.

Événements WS : progress (layer_key="ilots_rpg"), phase:ilots_rpg_ready, error.
Résultat : ecocompensation.projects.last_results_ilots_rpg (jsonb).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

from sqlalchemy import text

from . import build, enrich, pool
from .params import IlotsRpgParams, params_from_config

logger = logging.getLogger(__name__)
LAYER_KEY = "ilots_rpg"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_results(engine, project_id: str, result: dict) -> None:
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE ecocompensation.projects "
                          "ADD COLUMN IF NOT EXISTS last_results_ilots_rpg jsonb"))
        conn.execute(text("""
            UPDATE ecocompensation.projects
            SET last_results_ilots_rpg = CAST(:r AS jsonb), updated_at = now()
            WHERE id = CAST(:pid AS uuid)"""),
            {"r": json.dumps(result, ensure_ascii=False, default=str), "pid": project_id})


def run_sync(engine, project_id: str, aoi_id: str, params: IlotsRpgParams,
             cb: Callable[[str], None] | None = None, *, profiling: bool = True) -> dict:
    """Chaîne complète synchrone : build -> enrich -> pool -> sauvegarde."""
    build.run(engine, project_id, aoi_id, params, cb)
    enrich.run(engine, project_id, params.fauna_species, cb)
    result = pool.build_response(engine, project_id, params, profiling=profiling)
    save_results(engine, project_id, result)
    return result


async def run_ilots_rpg_after_filter(engine, project_id: str, aoi_id: str, config,
                                     push: Callable[[dict], Awaitable[None]]) -> None:
    params = params_from_config(config)
    loop = asyncio.get_running_loop()
    logger.info("[ilots_rpg] START project_id=%s millesime=%s", project_id, params.millesime)

    def cb(msg: str) -> None:
        logger.info("[ilots_rpg] project_id=%s %s", project_id, msg)
        asyncio.run_coroutine_threadsafe(
            push({"event": "progress", "layer_key": LAYER_KEY, "message": msg, "timestamp": _now()}), loop)

    try:
        await push({"event": "progress", "layer_key": LAYER_KEY,
                    "message": "PHASE:ilots_rpg:start", "timestamp": _now()})
        result = await loop.run_in_executor(
            None, lambda: run_sync(engine, project_id, aoi_id, params, cb))
        await push({"event": "phase:ilots_rpg_ready", "layer_key": LAYER_KEY,
                    "message": f"Îlots agricoles disponibles ({result['total_ilots']})",
                    "timestamp": _now()})
        logger.info("[ilots_rpg] DONE project_id=%s total=%s", project_id, result["total_ilots"])
    except Exception:
        logger.exception("[ilots_rpg] FAILED project_id=%s", project_id)
        await push({"event": "error", "layer_key": LAYER_KEY,
                    "message": "Erreur calcul îlots agricoles (voir logs serveur)", "timestamp": _now()})
