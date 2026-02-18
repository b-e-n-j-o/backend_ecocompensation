#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
orchestrator.py
===============

Lance tous les fetches de couches pour un aoi_id donné, en séquence,
et pousse les mises à jour de progression via un callback.

Utilisé par :
  - l'API FastAPI (route POST /projects/{id}/fetch)  → callback = WebSocket push
  - la CLI locale                                    → callback = print

La table ecocompensation.projects est mise à jour à chaque étape.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Callable, Awaitable

from sqlalchemy import text

from layers.layer_runner import LAYER_REGISTRY, LayerResult

logger = logging.getLogger(__name__)

# Callback async : reçoit un dict de progression et l'envoie au client
ProgressPush = Callable[[dict], Awaitable[None]]


async def _noop_push(data: dict) -> None:
    pass


async def run_orchestration(
    engine,
    project_id: str,
    aoi_id: str,
    push: ProgressPush = _noop_push,
) -> dict[str, LayerResult]:
    """
    Exécute toutes les couches dans l'ordre du LAYER_REGISTRY.
    Pousse des événements WebSocket à chaque étape.

    Retourne un dict {layer_key: LayerResult}.
    """

    results: dict[str, LayerResult] = {}
    total = len(LAYER_REGISTRY)

    async def emit(event: str, layer_key: str, message: str, extra: dict | None = None):
        payload = {
            "event": event,
            "layer_key": layer_key,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **(extra or {}),
        }
        await push(payload)
        # Mise à jour de la colonne layers_status en base
        try:
            with engine.begin() as conn:
                conn.execute(
                    text("""
                        UPDATE ecocompensation.projects
                        SET layers_status = layers_status || jsonb_build_object(:key, :val),
                            updated_at = now()
                        WHERE id = :pid;
                    """),
                    {"key": layer_key, "val": event, "pid": project_id},
                )
        except Exception:
            pass  # Ne pas bloquer le fetch pour une erreur de status

    # Marquer le projet en cours
    try:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE ecocompensation.projects SET status='fetching', updated_at=now() WHERE id=:pid"),
                {"pid": project_id},
            )
    except Exception:
        pass

    await push({"event": "start", "total_layers": total,
                "message": f"Démarrage de l'orchestration pour {total} couches",
                "timestamp": datetime.now(timezone.utc).isoformat()})

    t_global = time.perf_counter()

    for i, layer_cfg in enumerate(LAYER_REGISTRY, 1):
        key = layer_cfg["key"]
        label = layer_cfg["label"]
        fn = layer_cfg["fn"]

        await emit("running", key, f"[{i}/{total}] {label} en cours…")

        # Le callback synchrone du layer_runner → on le convertit en push async
        def make_cb(k: str):
            def cb(msg: str):
                # On schedule l'envoi sans bloquer le thread sync
                asyncio.get_event_loop().call_soon_threadsafe(
                    asyncio.ensure_future,
                    push({"event": "progress", "layer_key": k, "message": msg,
                          "timestamp": datetime.now(timezone.utc).isoformat()})
                )
            return cb

        # Exécution dans un thread pool pour ne pas bloquer la boucle asyncio
        loop = asyncio.get_event_loop()
        result: LayerResult = await loop.run_in_executor(
            None, fn, engine, aoi_id, make_cb(key)
        )
        results[key] = result

        if result.error:
            await emit("error", key, f"❌ {label} : {result.error}",
                       {"duration_s": round(result.duration_s, 1)})
        elif result.skipped:
            await emit("skipped", key, f"⏭ {label} : aucune donnée (ignorée)",
                       {"duration_s": round(result.duration_s, 1), "n_inserted": 0})
        else:
            await emit("done", key, f"✓ {label} : {result.n_inserted:,} entités ({result.duration_s:.1f}s)",
                       {"duration_s": round(result.duration_s, 1), "n_inserted": result.n_inserted})

    total_s = time.perf_counter() - t_global
    n_ok = sum(1 for r in results.values() if r.success and not r.skipped)
    n_skip = sum(1 for r in results.values() if r.skipped)
    n_err = sum(1 for r in results.values() if r.error)

    summary = {
        "event": "complete",
        "message": f"Orchestration terminée en {total_s:.0f}s — {n_ok} OK, {n_skip} ignorées, {n_err} erreurs",
        "total_s": round(total_s, 1),
        "n_ok": n_ok, "n_skip": n_skip, "n_err": n_err,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await push(summary)

    # Marquer le projet comme prêt (ou en erreur partielle)
    final_status = "ready" if n_err == 0 else "ready_with_errors"
    try:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE ecocompensation.projects SET status=:s, updated_at=now() WHERE id=:pid"),
                {"s": final_status, "pid": project_id},
            )
    except Exception:
        pass

    logger.info(summary["message"])
    return results