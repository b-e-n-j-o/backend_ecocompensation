#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
filter_orchestrator.py
======================

Orchestre le pipeline de filtrage écologique (sans fetch de couches SIG)
et pousse la progression via WebSocket (même pattern que orchestrator.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue as queue_module
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable

from sqlalchemy import text

from layers.filter_pipeline import FilterConfig, FilterPipelineResult, run as run_filter_pipeline
from orchestrator import UF_PHASE_KEYS, run_orchestration
from layers.uf_profiling import build_uf_pool_with_profiling
from pool import pool_service
from pool.pool_service import persist_parcelles_pool_run
from pool.profiling_service import compute_metrics_for_run

logger = logging.getLogger(__name__)

ProgressPush = Callable[[dict], Awaitable[None]]

FILTER_PHASES = [
    {"key": "parcelles", "label": "Parcelles candidates (tiling)"},
    {"key": "filter",    "label": "Filtrage écologique"},
    {"key": "purge",     "label": "Purge parcelles éliminées"},
    {"key": "enrich",    "label": "Enrichissement léger"},
    {"key": "profiling", "label": "Profilage pool (PM + score éco)"},
]

UF_FILTER_PHASES = [
    {"key": "unites_foncieres", "label": "Unités foncières (PPM + clustering)"},
    {"key": "sous_ensembles", "label": "Sous-ensembles contigus"},
    {"key": "enrich_uf", "label": "Enrichissement UF (végétation / faune)"},
]

FULL_PIPELINE_PHASES = FILTER_PHASES + UF_FILTER_PHASES


async def _noop_push(data: dict) -> None:
    pass


def _filter_options_json(config: FilterConfig) -> dict:
    return {
        "pipeline": "filter_v2",
        "min_area_ha": config.min_area_ha,
        "miller_thresh": config.miller_thresh,
        "cesbio_libelles": config.cesbio_libelles,
        "fauna_criteria": [
            {"species": fc.species, "dist_m": fc.dist_m}
            for fc in config.fauna_criteria
        ],
    }


def _persist_pipeline_results(
    engine,
    project_id: str,
    aoi_id: str,
    config: FilterConfig,
    result: FilterPipelineResult,
) -> str | None:
    """Écrit parcelles_pool_runs + last_results / last_filter pour affichage."""
    with engine.begin() as conn:
        rows = conn.execute(
            text("""
                SELECT
                    p.idu,
                    COALESCE(p.code_insee, '') AS code_insee,
                    COALESCE(p.section, '') AS section,
                    COALESCE(p.numero, '') AS numero,
                    ROUND((ST_Area(p.geom_2154) / 10000.0)::numeric, 4) AS surface_ha,
                    ROUND((
                        (4.0 * PI() * ST_Area(p.geom_2154))
                        / NULLIF(ST_Perimeter(p.geom_2154)^2, 0)
                    )::numeric, 4) AS miller,
                    ROUND((
                        ST_Distance(ST_Centroid(p.geom_2154), ST_Centroid(a.geom_2154)) / 1000.0
                    )::numeric, 3) AS distance_km,
                    p.veg_libelles,
                    p.fauna_distances
                FROM ecocompensation_results.parcelles p
                CROSS JOIN ecocompensation.aoi a
                WHERE p.project_id = CAST(:pid AS uuid)
                  AND a.id = CAST(:aid AS uuid)
                ORDER BY ST_Area(p.geom_2154) DESC
            """),
            {"pid": project_id, "aid": aoi_id},
        ).mappings().all()

    parcelles = []
    for i, row in enumerate(rows, 1):
        parcelles.append({
            "rank": i,
            "idu": row["idu"],
            "code_insee": row["code_insee"],
            "section": row["section"],
            "numero": row["numero"],
            "surface_ha": float(row["surface_ha"] or 0),
            "miller": float(row["miller"] or 0),
            "distance_km": float(row["distance_km"] or 0),
            "dist_hydro_m": None,
            "veg_libelles": list(row["veg_libelles"] or []),
            "fauna_distances": dict(row["fauna_distances"] or {}),
        })

    funnel = [
        {"step": 1, "label": f"Candidats (≥{config.min_area_ha} ha)", "count": result.n_tiled},
        {"step": 2, "label": "Après filtrage écologique", "count": result.n_after_filter},
    ]
    options_json = _filter_options_json(config)
    result_summary = {
        "pipeline": "filter_v2",
        "n_tiled": result.n_tiled,
        "n_after_filter": result.n_after_filter,
        "n_purged": result.n_purged,
        "duration_s": result.duration_s,
        "funnel": funnel,
    }

    pool_parcelles = [
        {
            "idu": p["idu"],
            "rank": p["rank"],
            "surface_ha": p["surface_ha"],
            "miller": p["miller"],
            "distance_km": p["distance_km"],
            "dist_hydro_m": p.get("dist_hydro_m"),
        }
        for p in parcelles
    ]

    run_id = persist_parcelles_pool_run(
        engine,
        project_id=project_id,
        options_json=options_json,
        parcelles=pool_parcelles,
        scope="parcelles",
        keep_last=20,
        result_summary=result_summary,
    )
    logger.info(
        "[filter_orchestrator] POOL RUN persisted project_id=%s run_id=%s parcelles=%d",
        project_id,
        run_id,
        len(pool_parcelles),
    )

    # Enrichissement léger → métriques pool (veg_libelles, fauna_distances)
    with engine.begin() as conn:
        pool_service.ensure_tables(conn)
        for p in parcelles:
            enrich: dict = {}
            if p.get("veg_libelles"):
                enrich["veg_libelles"] = p["veg_libelles"]
            if p.get("fauna_distances"):
                enrich["fauna_distances"] = p["fauna_distances"]
            if enrich:
                pool_service.upsert_metric(
                    conn,
                    project_id=project_id,
                    run_id=run_id,
                    idu=p["idu"],
                    metric_key="filter_enrich",
                    metric_value=enrich,
                )

    last_results = {
        "total": len(parcelles),
        "final_radius_km": 0.0,
        "parcelles": [
            {k: v for k, v in p.items() if k not in ("veg_libelles", "fauna_distances")}
            for p in parcelles
        ],
        "funnel": funnel,
        "pool_run_id": run_id,
        "pipeline": "filter_v2",
    }

    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE ecocompensation.projects
                SET last_filter = CAST(:f AS jsonb),
                    last_results = CAST(:r AS jsonb),
                    updated_at = now()
                WHERE id = CAST(:pid AS uuid)
            """),
            {
                "f": json.dumps(options_json, ensure_ascii=False),
                "r": json.dumps(last_results, ensure_ascii=False),
                "pid": project_id,
            },
        )
    return run_id


def _get_aoi_centre(engine, aoi_id: str) -> tuple[float, float]:
    with engine.begin() as conn:
        row = conn.execute(
            text("""
                SELECT ST_X(ST_Centroid(geom_2154)) AS cx,
                       ST_Y(ST_Centroid(geom_2154)) AS cy
                FROM ecocompensation.aoi WHERE id = CAST(:aid AS uuid)
            """),
            {"aid": aoi_id},
        ).mappings().one_or_none()
    if not row:
        raise RuntimeError(f"AOI introuvable: {aoi_id}")
    return float(row["cx"]), float(row["cy"])


async def _run_uf_after_filter(
    engine,
    project_id: str,
    aoi_id: str,
    config: FilterConfig,
    push: ProgressPush,
) -> None:
    """Phase UF en arrière-plan : PPM → sous-ensembles → enrich_uf → last_results_uf."""
    fauna_species = [fc.species for fc in config.fauna_criteria if fc.species.strip()]
    logger.info("[filter_orchestrator] UF phase START project_id=%s", project_id)

    async def uf_push(data: dict) -> None:
        if data.get("event") == "start":
            data = {
                **data,
                "event": "uf_start",
                "message": "Démarrage calcul unités foncières",
            }
        elif data.get("event") == "complete":
            data = {**data, "event": "uf_complete"}
        ev = data.get("event", "")
        lk = data.get("layer_key", "")
        msg = data.get("message", "")
        if ev in ("running", "done", "error", "skipped", "progress", "uf_start") or ev.startswith("phase:"):
            logger.info(
                "[filter_orchestrator] UF project_id=%s event=%s layer=%s %s",
                project_id,
                ev,
                lk,
                (msg[:240] if msg else ""),
            )
        await push(data)

    try:
        await uf_push({
            "event": "progress",
            "layer_key": "unites_foncieres",
            "message": "PHASE:uf:start",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        await run_orchestration(
            engine,
            project_id,
            aoi_id,
            uf_push,
            layer_keys=list(UF_PHASE_KEYS),
            fauna_species=fauna_species or None,
            uf_min_area_ha=config.min_area_ha,
        )
        cx, cy = _get_aoi_centre(engine, aoi_id)
        fauna_criteria = [
            {"species": fc.species, "dist_m": fc.dist_m}
            for fc in config.fauna_criteria
            if fc.species.strip()
        ]
        uf_result = build_uf_pool_with_profiling(
            engine,
            project_id,
            cx,
            cy,
            cesbio_libelles=config.cesbio_libelles,
            fauna_species=fauna_species[0] if fauna_species else None,
            fauna_dist_m=config.fauna_criteria[0].dist_m if config.fauna_criteria else 1000.0,
            fauna_criteria=fauna_criteria or None,
            miller_thresh=config.miller_thresh,
        )
        with engine.begin() as conn:
            conn.execute(
                text("""
                    UPDATE ecocompensation.projects
                    SET last_results_uf = CAST(:r AS jsonb),
                        updated_at = now()
                    WHERE id = CAST(:pid AS uuid)
                """),
                {
                    "r": json.dumps(uf_result, ensure_ascii=False),
                    "pid": project_id,
                },
            )
        await uf_push({
            "event": "phase:uf_ready",
            "message": "Unités foncières disponibles",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        logger.info(
            "[filter_orchestrator] UF phase DONE project_id=%s total_uf=%s",
            project_id,
            uf_result.get("total_uf"),
        )
    except Exception:
        logger.exception("[filter_orchestrator] UF phase FAILED project_id=%s", project_id)
        await uf_push({
            "event": "error",
            "layer_key": "unites_foncieres",
            "message": "Erreur calcul unités foncières (voir logs serveur)",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })


def _run_pool_profiling(engine, project_id: str, run_id: str) -> None:
    with engine.begin() as conn:
        compute_metrics_for_run(conn, project_id, run_id)


async def _run_pipeline_with_progress(
    engine,
    project_id: str,
    aoi_id: str,
    config: FilterConfig,
    push: ProgressPush,
    loop: asyncio.AbstractEventLoop,
) -> FilterPipelineResult:
    msg_queue: queue_module.Queue = queue_module.Queue()
    current_phase = {"key": "parcelles"}

    def cb(msg: str) -> None:
        logger.info("[filter_orchestrator] project_id=%s %s", project_id, msg)
        if msg.startswith("PHASE:"):
            parts = msg.split(":")
            if len(parts) >= 2:
                current_phase["key"] = parts[1]
        elif msg.startswith("TILE_PROGRESS:"):
            current_phase["key"] = "parcelles"
        elif msg.startswith("FILTER_STEP:"):
            current_phase["key"] = "filter"
        elif msg.startswith("ENRICH_BATCH:"):
            current_phase["key"] = "enrich"

        msg_queue.put({
            "event": "progress",
            "layer_key": current_phase["key"],
            "message": msg,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    async def drain_queue() -> None:
        while True:
            while True:
                try:
                    payload = msg_queue.get_nowait()
                    await push(payload)
                except queue_module.Empty:
                    break
            await asyncio.sleep(0.1)

    drain_task = asyncio.create_task(drain_queue())
    try:
        result = await loop.run_in_executor(
            None,
            lambda: run_filter_pipeline(engine, project_id, aoi_id, config, cb=cb),
        )
    finally:
        drain_task.cancel()
        try:
            await drain_task
        except asyncio.CancelledError:
            pass
        while True:
            try:
                payload = msg_queue.get_nowait()
                await push(payload)
            except queue_module.Empty:
                break

    return result


async def run_filter_orchestration(
    engine,
    project_id: str,
    aoi_id: str,
    config: FilterConfig,
    push: ProgressPush = _noop_push,
) -> FilterPipelineResult:
    """Lance le pipeline de filtrage avec événements WS."""
    total = len(FILTER_PHASES)
    t_global = time.perf_counter()
    loop = asyncio.get_running_loop()

    async def emit(event: str, phase_key: str, message: str, extra: dict | None = None) -> None:
        payload = {
            "event": event,
            "layer_key": phase_key,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **(extra or {}),
        }
        await push(payload)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text("""
                        UPDATE ecocompensation.projects
                        SET layers_status = layers_status
                            || jsonb_build_object(CAST(:key AS text), CAST(:val AS text)),
                            updated_at = now()
                        WHERE id = :pid
                    """),
                    {"key": phase_key, "val": event, "pid": project_id},
                )
        except Exception:
            logger.exception("layers_status update failed project_id=%s", project_id)

    logger.info(
        "[filter_orchestrator] START project_id=%s aoi_id=%s "
        "min_area=%.1fha miller≥%.2f cesbio=%d fauna=%d",
        project_id,
        aoi_id,
        config.min_area_ha,
        config.miller_thresh,
        len(config.cesbio_libelles),
        len(config.fauna_criteria),
    )

    await push({
        "event": "start",
        "total_layers": total,
        "message": "Démarrage du filtrage écologique",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    try:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE ecocompensation.projects SET status='filtering', updated_at=now() WHERE id=:pid"),
                {"pid": project_id},
            )
    except Exception:
        logger.exception("status=filtering failed project_id=%s", project_id)

    await emit("running", "parcelles", "Parcelles candidates en cours…")

    result = await _run_pipeline_with_progress(
        engine, project_id, aoi_id, config, push, loop
    )

    if result.error:
        await emit("error", "filter", f"❌ Erreur : {result.error}",
                   {"duration_s": result.duration_s})
        final_status = "error"
    else:
        run_id: str | None = None
        try:
            run_id = _persist_pipeline_results(engine, project_id, aoi_id, config, result)
            if run_id:
                logger.info(
                    "[filter_orchestrator] DONE project_id=%s run_id=%s "
                    "%d candidates → %d retenues (%.1fs)",
                    project_id,
                    run_id,
                    result.n_tiled,
                    result.n_after_filter,
                    result.duration_s,
                )
                await emit("running", "profiling", "Profilage pool (PM + score éco)…")
                try:
                    await loop.run_in_executor(
                        None,
                        lambda: _run_pool_profiling(engine, project_id, run_id),
                    )
                    await emit(
                        "done",
                        "profiling",
                        "Profilage pool terminé (personnes morales + score éco)",
                    )
                    await push({
                        "event": "phase:parcelles_ready",
                        "message": "Parcelles disponibles — analyse UF en cours…",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                except Exception:
                    logger.exception(
                        "Profilage pool échoué project_id=%s run_id=%s",
                        project_id,
                        run_id,
                    )
                    await emit("error", "profiling", "Profilage pool partiellement en échec (voir logs serveur)")
        except Exception:
            logger.exception("Persistance pool/last_results échouée project_id=%s", project_id)
        await emit(
            "done", "enrich",
            f"✓ {result.n_after_filter:,} parcelles retenues ({result.duration_s:.1f}s)",
            {"n_inserted": result.n_after_filter, "duration_s": result.duration_s},
        )
        final_status = "ready"

    total_s = round(time.perf_counter() - t_global, 1)
    await push({
        "event": "complete",
        "message": (
            f"Filtrage terminé en {total_s}s — "
            f"{result.n_tiled:,} candidates → {result.n_after_filter:,} retenues"
        ),
        "total_s": total_s,
        "n_ok": 0 if result.error else 1,
        "n_skip": 0,
        "n_err": 1 if result.error else 0,
        "n_final": result.n_after_filter,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    try:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE ecocompensation.projects SET status=:s, updated_at=now() WHERE id=:pid"),
                {"s": final_status, "pid": project_id},
            )
    except Exception:
        pass

    if result.error:
        logger.error(
            "[filter_orchestrator] ERROR project_id=%s %s (%.1fs)",
            project_id,
            result.error,
            result.duration_s,
        )
    else:
        logger.info(
            "[filter_orchestrator] COMPLETE project_id=%s total_s=%.1f",
            project_id,
            total_s,
        )
        # UF personnes morales en arrière-plan (peut durer plusieurs minutes)
        asyncio.create_task(
            _run_uf_after_filter(engine, project_id, aoi_id, config, push)
        )

    return result
