#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
orchestrator.py
===============

Lance les fetches de couches pour un aoi_id donné, en séquence,
et pousse les mises à jour de progression via un callback.

Options :
  - ``layer_keys`` : sous-ensemble de couches (ordre = registre) ; ``None`` = toutes.
  - ``dry_run`` : exécute chaque couche puis supprime les lignes insérées pour ce projet
    (même logique que la CLI).

Utilisé par :
  - l'API FastAPI (POST /projects/{id}/fetch)  → WebSocket
  - la CLI ``run_orchestration_cli.py``

PATTERN PROGRESS :
  Le cb passé à chaque couche met les messages dans une queue thread-safe.
  Un drain_task asyncio vide la queue toutes les 100ms pendant que le thread
  SQL tourne via run_in_executor. Cela évite le deadlock où run_in_executor
  bloque la boucle asyncio qui ne peut plus traiter run_coroutine_threadsafe.
"""

from __future__ import annotations

import asyncio
import queue as queue_module
import logging
import time
from datetime import datetime, timezone
from typing import Callable, Awaitable

from sqlalchemy import text

from layers.layer_runner import LAYER_REGISTRY, LayerResult

logger = logging.getLogger(__name__)

ProgressPush = Callable[[dict], Awaitable[None]]


async def _noop_push(data: dict) -> None:
    pass


def select_layer_configs(layer_keys: list[str]) -> list[dict]:
    """
    Filtre et ordonne les entrées du registre selon ``layer_keys``
    (ordre = ordre du LAYER_REGISTRY).
    """
    if not layer_keys:
        raise ValueError("La liste de couches ne peut pas être vide")
    index = {cfg["key"]: cfg for cfg in LAYER_REGISTRY}
    missing = [k for k in layer_keys if k not in index]
    if missing:
        raise ValueError(f"Couches inconnues : {missing}")
    order = [cfg["key"] for cfg in LAYER_REGISTRY]
    selected = set(layer_keys)
    ordered_keys = [k for k in order if k in selected]
    return [index[k] for k in ordered_keys]


def _resolve_target_tables(engine, table_pattern: str) -> list[str]:
    if "*" not in table_pattern:
        return [table_pattern]
    if "." not in table_pattern:
        return []
    schema, table_like = table_pattern.split(".", 1)
    sql_like = table_like.replace("*", "%")
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT table_schema, table_name
                FROM information_schema.tables
                WHERE table_schema = :schema
                  AND table_name LIKE :table_like
                ORDER BY table_name
                """
            ),
            {"schema": schema, "table_like": sql_like},
        ).mappings().all()
    return [f"{r['table_schema']}.{r['table_name']}" for r in rows]


def cleanup_dry_run_writes(engine, table_pattern: str, project_id: str) -> None:
    """Supprime les lignes insérées pour ce projet après un dry-run."""
    tables = _resolve_target_tables(engine, table_pattern)
    if not tables:
        logger.debug("[dry-run] Aucune table pour le pattern %s", table_pattern)
        return
    with engine.begin() as conn:
        for table in tables:
            try:
                conn.execute(text(f"DELETE FROM {table} WHERE project_id = :pid"), {"pid": project_id})
            except Exception as e:
                logger.warning("[dry-run] Nettoyage ignoré sur %s : %s", table, e)


def _get_aoi_buffer_km(engine, aoi_id: str) -> float:
    try:
        with engine.begin() as conn:
            buffer_m = conn.execute(
                text(
                    """
                    SELECT buffer_m
                    FROM ecocompensation.aoi
                    WHERE id = :aid
                    """
                ),
                {"aid": aoi_id},
            ).scalar_one_or_none()
        if buffer_m is None:
            return 0.0
        return float(buffer_m) / 1000.0
    except Exception:
        return 0.0


async def _run_layer_with_progress(
    fn,
    engine,
    project_id: str,
    aoi_id: str,
    key: str,
    push: ProgressPush,
    loop: asyncio.AbstractEventLoop,
    **kwargs,
) -> LayerResult:
    """
    Exécute fn dans un thread (run_in_executor) tout en drainant
    les messages de progression via une queue thread-safe.

    Le cb passé à fn met les messages dans msg_queue (non-bloquant).
    drain_task vide la queue toutes les 100ms dans la boucle asyncio.
    Les deux tournent en parallèle → pas de deadlock.
    """
    msg_queue: queue_module.Queue = queue_module.Queue()

    def cb(msg: str):
        msg_queue.put({
            "event": "progress",
            "layer_key": key,
            "message": msg,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    async def drain_queue():
        """Vide la queue et push chaque message vers le WS."""
        while True:
            # Vider tout ce qui est disponible d'un coup
            drained = 0
            while True:
                try:
                    payload = msg_queue.get_nowait()
                    await push(payload)
                    drained += 1
                except queue_module.Empty:
                    break
            await asyncio.sleep(0.1)

    # Lance le drain en parallèle du thread SQL
    drain_task = asyncio.create_task(drain_queue())

    try:
        if kwargs:
            def run_sync():
                return fn(engine, project_id, aoi_id, cb, **kwargs)
            result = await loop.run_in_executor(None, run_sync)
        else:
            result = await loop.run_in_executor(None, fn, engine, project_id, aoi_id, cb)
    finally:
        drain_task.cancel()
        try:
            await drain_task
        except asyncio.CancelledError:
            pass
        # Vider ce qui reste dans la queue après la fin du thread
        while True:
            try:
                payload = msg_queue.get_nowait()
                await push(payload)
            except queue_module.Empty:
                break

    return result


async def run_orchestration(
    engine,
    project_id: str,
    aoi_id: str,
    push: ProgressPush = _noop_push,
    *,
    layer_keys: list[str] | None = None,
    dry_run: bool = False,
    uf_max_parcelles: int = 5,
    uf_min_area_ha: float = 7.0,
    fauna_species: list[str] | None = None,
) -> dict[str, LayerResult]:
    """
    Exécute les couches (toutes si ``layer_keys`` est None, sinon sous-ensemble).

    ``dry_run`` : après chaque couche réussie, suppression des lignes ``project_id``
    dans la table de résultat (test sans encombrer durablement la base).
    """
    if layer_keys is not None and len(layer_keys) == 0:
        raise ValueError("layer_keys ne peut pas être une liste vide")

    if layer_keys is None:
        configs = list(LAYER_REGISTRY)
    else:
        configs = select_layer_configs(layer_keys)

    results: dict[str, LayerResult] = {}
    total = len(configs)
    buffer_km = _get_aoi_buffer_km(engine, aoi_id)
    skipped_for_large_buffer = {"unites_foncieres", "sous_ensembles"} if buffer_km > 5.0 else set()

    async def emit(event: str, layer_key: str, message: str, extra: dict | None = None):
        payload = {
            "event": event,
            "layer_key": layer_key,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **(extra or {}),
        }
        await push(payload)
        if dry_run:
            return
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
            pass

    try:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE ecocompensation.projects SET status='fetching', updated_at=now() WHERE id=:pid"),
                {"pid": project_id},
            )
    except Exception:
        pass

    mode = " (dry-run, données non conservées)" if dry_run else ""
    await push({
        "event": "start",
        "total_layers": total,
        "dry_run": dry_run,
        "message": f"Démarrage de l'orchestration pour {total} couche(s){mode}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    t_global = time.perf_counter()
    loop = asyncio.get_running_loop()

    for i, layer_cfg in enumerate(configs, 1):
        key = layer_cfg["key"]
        label = layer_cfg["label"]
        fn = layer_cfg["fn"]

        # ── Couches ignorées pour buffer > 5km ───────────────────────────────
        if key in skipped_for_large_buffer:
            result = LayerResult(
                layer_key=key,
                table=layer_cfg["table"],
                n_inserted=0,
                duration_s=0.0,
                skipped=True,
            )
            results[key] = result
            await emit(
                "skipped", key,
                f"⏭ {label} : ignorée automatiquement "
                f"(buffer AOI {buffer_km:.1f} km > 5 km, calcul trop coûteux)",
                {"duration_s": 0.0, "n_inserted": 0},
            )
            continue

        await emit("running", key, f"[{i}/{total}] {label} en cours…")

        # ── Exécution avec drain de progression ──────────────────────────────
        try:
            if key == "unites_foncieres":
                result = await _run_layer_with_progress(
                    fn, engine, project_id, aoi_id, key, push, loop,
                    min_area_ha=uf_min_area_ha,
                )
            elif key == "sous_ensembles":
                result = await _run_layer_with_progress(
                    fn, engine, project_id, aoi_id, key, push, loop,
                    max_uf_parcelles=uf_max_parcelles,
                )
            elif key == "fauna":
                result = await _run_layer_with_progress(
                    fn, engine, project_id, aoi_id, key, push, loop,
                    species_list=fauna_species,
                )
            else:
                result = await _run_layer_with_progress(
                    fn, engine, project_id, aoi_id, key, push, loop,
                )
        except Exception as e:
            logger.exception("Erreur couche %s", key)
            result = LayerResult(
                layer_key=key,
                table=layer_cfg["table"],
                duration_s=0.0,
                error=str(e),
            )

        if dry_run and not result.error:
            cleanup_dry_run_writes(engine, layer_cfg["table"], project_id)

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
    n_ok   = sum(1 for r in results.values() if r.success and not r.skipped)
    n_skip = sum(1 for r in results.values() if r.skipped)
    n_err  = sum(1 for r in results.values() if r.error)

    summary = {
        "event": "complete",
        "message": f"Orchestration terminée en {total_s:.0f}s — {n_ok} OK, {n_skip} ignorées, {n_err} erreurs",
        "total_s": round(total_s, 1),
        "n_ok": n_ok,
        "n_skip": n_skip,
        "n_err": n_err,
        "dry_run": dry_run,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await push(summary)

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