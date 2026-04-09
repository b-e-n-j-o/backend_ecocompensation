from __future__ import annotations

import logging

from pool import pool_service
from pool.profilers.carhab import CarhabProfiler
from pool.profilers.cosia import CosiaProfiler
from pool.profilers.vegetation_hybride import VegetationHybrideProfiler
from pool.profilers.zone_humide import ZoneHumideProfiler

logger = logging.getLogger(__name__)


PROFILERS = [
    VegetationHybrideProfiler(),
    CosiaProfiler(),
    CarhabProfiler(),
    ZoneHumideProfiler(),  # placeholder (retourne vide)
]


def compute_metrics_for_run(conn, project_id: str, run_id: str) -> None:
    pool_service.ensure_tables(conn)
    for profiler in PROFILERS:
        try:
            # Isole chaque profiler dans un SAVEPOINT:
            # en cas d'erreur SQL (timeout, etc.), la transaction globale reste saine.
            with conn.begin_nested():
                payload_by_idu = profiler.compute_for_run(conn, project_id, run_id)
                for idu, payload in payload_by_idu.items():
                    pool_service.upsert_metric(
                        conn,
                        project_id=project_id,
                        run_id=run_id,
                        idu=idu,
                        metric_key=profiler.metric_key,
                        metric_value=payload,
                    )
        except Exception:
            logger.exception(
                "Profiling pool échoué pour metric_key=%s (project_id=%s, run_id=%s)",
                profiler.metric_key,
                project_id,
                run_id,
            )
            continue
