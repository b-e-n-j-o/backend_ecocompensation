from __future__ import annotations

import logging
import time

from sqlalchemy import text

from pool import pool_service
from pool.profiler_registry import (
    get_profilers_for_study_type,
    profiling_summary_label,
    resolve_study_type,
)
from pool.profilers.composite_score_v1 import CompositeScoreV1Profiler
from pool.profilers.durete_fonciere import DureteFonciereProfiler
from pool.profilers.personnes_morales import PersonnesMoralesProfiler
from pool.profilers.score_eco import ScoreEcoProfiler

logger = logging.getLogger(__name__)


def compute_metrics_for_run(conn, project_id: str, run_id: str) -> None:
    t0 = time.perf_counter()
    pool_service.ensure_tables(conn)
    study_type = resolve_study_type(conn, project_id, run_id)
    profilers = get_profilers_for_study_type(study_type)
    try:
        idu_total = conn.execute(
            text(
                """
                SELECT COUNT(*)
                FROM ecocompensation_results.parcelles_pool
                WHERE project_id = CAST(:project_id AS uuid)
                  AND run_id = CAST(:run_id AS uuid)
                """
            ),
            {"project_id": project_id, "run_id": run_id},
        ).scalar_one()
    except Exception:
        idu_total = None

    logger.info(
        "POOL RUN START | project_id=%s run_id=%s study_type=%s profilers=%s parcelles_pool=%s",
        project_id,
        run_id,
        study_type,
        [p.metric_key for p in profilers],
        idu_total if idu_total is not None else "unknown",
    )
    try:
        conn.execute(text("SET LOCAL statement_timeout = '15min'"))
    except Exception:
        pass
    n_ok = 0
    n_err = 0
    total_upserts = 0
    for idx, profiler in enumerate(profilers, 1):
        p0 = time.perf_counter()
        logger.info(
            "POOL PHASE [%d/%d] START | metric=%s project_id=%s run_id=%s",
            idx,
            len(profilers),
            profiler.metric_key,
            project_id,
            run_id,
        )
        try:
            with conn.begin_nested():
                payload_by_idu = profiler.compute_for_run(conn, project_id, run_id)
                upserts = 0
                for idu, payload in payload_by_idu.items():
                    pool_service.upsert_metric(
                        conn,
                        project_id=project_id,
                        run_id=run_id,
                        idu=idu,
                        metric_key=profiler.metric_key,
                        metric_value=payload,
                    )
                    upserts += 1
                total_upserts += upserts
                n_ok += 1
                elapsed = time.perf_counter() - p0
                logger.info(
                    "POOL PHASE [%d/%d] DONE | metric=%s upserts=%d duration_s=%.2f",
                    idx,
                    len(profilers),
                    profiler.metric_key,
                    upserts,
                    elapsed,
                )
        except Exception:
            n_err += 1
            elapsed = time.perf_counter() - p0
            logger.exception(
                "POOL PHASE [%d/%d] ERROR | metric=%s duration_s=%.2f project_id=%s run_id=%s",
                idx,
                len(profilers),
                profiler.metric_key,
                elapsed,
                project_id,
                run_id,
            )
            continue
    total_s = time.perf_counter() - t0
    logger.info(
        "POOL RUN COMPLETE | project_id=%s run_id=%s phases_ok=%d phases_err=%d total_upserts=%d total_s=%.2f",
        project_id,
        run_id,
        n_ok,
        n_err,
        total_upserts,
        total_s,
    )


def _upsert_profiler_payload(
    conn,
    project_id: str,
    run_id: str,
    metric_key: str,
    payload_by_idu: dict,
) -> int:
    count = 0
    for idu, payload in payload_by_idu.items():
        pool_service.upsert_metric(
            conn,
            project_id=project_id,
            run_id=run_id,
            idu=idu,
            metric_key=metric_key,
            metric_value=payload,
        )
        count += 1
    return count


def compute_durete_for_run(
    conn,
    project_id: str,
    run_id: str,
    *,
    exclude_indesirables: bool = True,
) -> dict:
    """
    Calcule la dureté foncière (attractivité) pour les parcelles du run pool.

    - Pré-requis PM : rafraîchit `parcelles_personnes_morales` sur tout le run.
    - Dureté : uniquement sur les IDU actifs (hors pool indésirables projet si demandé).
    - Met à jour `composite_score_v1` après la dureté.
    """
    t0 = time.perf_counter()
    pool_service.ensure_tables(conn)
    try:
        conn.execute(text("SET LOCAL statement_timeout = '30min'"))
    except Exception:
        pass

    pool_idus = {
        str(r["idu"])
        for r in pool_service.get_pool(conn, project_id=project_id, run_id=run_id)
        if r.get("idu")
    }
    skipped_indesirables = 0
    if exclude_indesirables:
        excluded = set(pool_service.list_project_indesirable_idus(conn, project_id))
        active_idus = pool_idus - excluded
        skipped_indesirables = len(pool_idus & excluded)
    else:
        active_idus = pool_idus

    if not active_idus:
        return {
            "updated_count": 0,
            "active_idus": 0,
            "skipped_indesirables": skipped_indesirables,
            "eligible_pm": 0,
            "composite_updated": 0,
            "duration_s": round(time.perf_counter() - t0, 2),
        }

    pm_profiler = PersonnesMoralesProfiler()
    pm_payload = pm_profiler.compute_for_run(conn, project_id, run_id)
    pm_upserts = _upsert_profiler_payload(
        conn, project_id, run_id, pm_profiler.metric_key, pm_payload
    )

    durete_profiler = DureteFonciereProfiler()
    durete_payload = durete_profiler.compute_for_run(
        conn, project_id, run_id, only_idus=active_idus
    )
    durete_upserts = _upsert_profiler_payload(
        conn, project_id, run_id, durete_profiler.metric_key, durete_payload
    )

    eligible_pm = sum(
        1
        for idu in active_idus
        if (durete_payload.get(idu) or {}).get("eligible") is True
    )

    composite_profiler = CompositeScoreV1Profiler()
    composite_payload = composite_profiler.compute_for_run(conn, project_id, run_id)
    composite_upserts = _upsert_profiler_payload(
        conn, project_id, run_id, composite_profiler.metric_key, composite_payload
    )

    duration_s = round(time.perf_counter() - t0, 2)
    logger.info(
        "DURETE POOL DONE | project_id=%s run_id=%s active_idus=%d skipped_indesirables=%d "
        "durete_upserts=%d eligible_pm=%d composite_upserts=%d duration_s=%.2f",
        project_id,
        run_id,
        len(active_idus),
        skipped_indesirables,
        durete_upserts,
        eligible_pm,
        composite_upserts,
        duration_s,
    )
    return {
        "updated_count": durete_upserts,
        "active_idus": len(active_idus),
        "skipped_indesirables": skipped_indesirables,
        "eligible_pm": eligible_pm,
        "pm_upserts": pm_upserts,
        "composite_updated": composite_upserts,
        "duration_s": duration_s,
    }


def compute_parcel_score_for_run(conn, project_id: str, run_id: str) -> int:
    """Recalcule uniquement la métrique `score_eco` pour un run (faune_buffer)."""
    pool_service.ensure_tables(conn)
    study_type = resolve_study_type(conn, project_id, run_id)
    profiler_keys = {p.metric_key for p in get_profilers_for_study_type(study_type)}
    if "score_eco" not in profiler_keys:
        logger.info(
            "score_eco ignoré pour study_type=%s (project_id=%s run_id=%s)",
            study_type,
            project_id,
            run_id,
        )
        return 0
    profiler = ScoreEcoProfiler()
    payload_by_idu = profiler.compute_for_run(conn, project_id, run_id)
    count = 0
    for idu, payload in payload_by_idu.items():
        pool_service.upsert_metric(
            conn,
            project_id=project_id,
            run_id=run_id,
            idu=idu,
            metric_key=profiler.metric_key,
            metric_value=payload,
        )
        count += 1
    return count
