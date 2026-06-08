from __future__ import annotations

import logging
import time

from sqlalchemy import text

from pool import pool_service
from pool.profilers.personnes_morales import PersonnesMoralesProfiler
from pool.profilers.score_eco import ScoreEcoProfiler

logger = logging.getLogger(__name__)

# filter_v2 : profilage léger sur tout le pool (PM + prospects + score éco).
# Profilers lourds (zonage hybride, CARHAB, dureté…) retirés — réactiver au besoin pour runs legacy.
PROFILERS = [
    PersonnesMoralesProfiler(),
    ScoreEcoProfiler(),
]


def compute_metrics_for_run(conn, project_id: str, run_id: str) -> None:
    t0 = time.perf_counter()
    pool_service.ensure_tables(conn)
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
        "POOL RUN START | project_id=%s run_id=%s profilers=%d parcelles_pool=%s",
        project_id,
        run_id,
        len(PROFILERS),
        idu_total if idu_total is not None else "unknown",
    )
    try:
        conn.execute(text("SET LOCAL statement_timeout = '15min'"))
    except Exception:
        pass
    n_ok = 0
    n_err = 0
    total_upserts = 0
    for idx, profiler in enumerate(PROFILERS, 1):
        p0 = time.perf_counter()
        logger.info(
            "POOL PHASE [%d/%d] START | metric=%s project_id=%s run_id=%s",
            idx,
            len(PROFILERS),
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
                    len(PROFILERS),
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
                len(PROFILERS),
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


def compute_parcel_score_for_run(conn, project_id: str, run_id: str) -> int:
    """Recalcule uniquement la métrique `score_eco` pour un run."""
    pool_service.ensure_tables(conn)
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
