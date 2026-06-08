#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
profile_cesbio.py
=================

Profiling riche CESBIO (ST_Intersection + surfaces/pct par libelle_prio).
À lancer séparément sur un run pool (ex. ~50 parcelles), pas pendant le filtrage.

Retourne par parcelle : {libelle_prio: {area_m2, pct}} où pct = % de la surface parcelle.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from sqlalchemy import text

from .base import BasePoolProfiler

logger = logging.getLogger(__name__)

PROFILE_BATCH_SIZE = 50
STMT_TIMEOUT = "90s"

Cb = Callable[[str], None] | None

_SQL_PROFILE_VEG_BATCH = """
WITH hits AS (
    SELECT
        pp.idu,
        v.libelle_prio,
        ST_Area(ST_Intersection(p.geom_2154, v.geom)) AS inter_area_m2
    FROM ecocompensation_results.parcelles_pool pp
    JOIN ecocompensation_results.parcelles p
      ON p.project_id = pp.project_id
     AND p.idu = pp.idu
    JOIN ecocompensation.vegetation_sur_cesbio v
        ON p.geom_2154 && v.geom
       AND ST_Intersects(p.geom_2154, v.geom)
    WHERE pp.project_id = CAST(:pid AS uuid)
      AND pp.run_id = CAST(:rid AS uuid)
      AND pp.idu = ANY(:idus)
      AND v.libelle_prio IS NOT NULL
),
class_areas AS (
    SELECT idu, libelle_prio, SUM(inter_area_m2) AS area_m2
    FROM hits
    WHERE inter_area_m2 > 0
    GROUP BY idu, libelle_prio
),
parcel_areas AS (
    SELECT pp.idu, ST_Area(p.geom_2154) AS parcel_area_m2
    FROM ecocompensation_results.parcelles_pool pp
    JOIN ecocompensation_results.parcelles p
      ON p.project_id = pp.project_id
     AND p.idu = pp.idu
    WHERE pp.project_id = CAST(:pid AS uuid)
      AND pp.run_id = CAST(:rid AS uuid)
      AND pp.idu = ANY(:idus)
)
SELECT
    ca.idu,
    ca.libelle_prio,
    ROUND(ca.area_m2::numeric, 1) AS area_m2,
    ROUND((ca.area_m2 / NULLIF(pa.parcel_area_m2, 0) * 100)::numeric, 2) AS pct
FROM class_areas ca
JOIN parcel_areas pa ON pa.idu = ca.idu
ORDER BY ca.idu, ca.area_m2 DESC
"""


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _profile_idus(
    conn,
    project_id: str,
    run_id: str,
    idus: list[str],
    log: Callable[[str], None],
) -> dict[str, dict[str, dict[str, float]]]:
    if not idus:
        return {}

    result: dict[str, dict[str, dict[str, float]]] = {}
    batches = _chunks(idus, PROFILE_BATCH_SIZE)
    t_total = 0.0

    log(f"PROFILE_CESBIO:start:{len(idus)}:{len(batches)}batches")
    for i, batch in enumerate(batches, 1):
        t0 = time.perf_counter()
        conn.execute(text(f"SET LOCAL statement_timeout = '{STMT_TIMEOUT}'"))
        rows = conn.execute(
            text(_SQL_PROFILE_VEG_BATCH),
            {"pid": project_id, "rid": run_id, "idus": batch},
        ).mappings().all()
        dt = round(time.perf_counter() - t0, 2)
        t_total += dt
        log(f"PROFILE_CESBIO:batch:{i}/{len(batches)}:{len(batch)}:{dt}s")

        for r in rows:
            idu = str(r["idu"])
            lib = str(r["libelle_prio"])
            result.setdefault(idu, {})[lib] = {
                "area_m2": float(r["area_m2"]),
                "pct": float(r["pct"]),
            }

    log(f"PROFILE_CESBIO:done:{len(result)}:{round(t_total, 1)}s")
    return result


class ProfileCesbioProfiler(BasePoolProfiler):
    """Profiler pool — métrique `cesbio_profile` (surfaces/pct par libellé CESBIO)."""

    metric_key = "cesbio_profile"

    def compute_for_run(self, conn, project_id: str, run_id: str) -> dict[str, dict[str, Any]]:
        idus = [
            str(r)
            for r in conn.execute(
                text(
                    """
                    SELECT idu
                    FROM ecocompensation_results.parcelles_pool
                    WHERE project_id = CAST(:pid AS uuid)
                      AND run_id = CAST(:rid AS uuid)
                    ORDER BY rank NULLS LAST, idu
                    """
                ),
                {"pid": project_id, "rid": run_id},
            ).scalars().all()
        ]
        return _profile_idus(
            conn,
            project_id,
            run_id,
            idus,
            lambda msg: logger.info(
                "[cesbio_profile] project_id=%s run_id=%s %s",
                project_id,
                run_id,
                msg,
            ),
        )


def run(
    engine,
    project_id: str,
    run_id: str,
    idus: list[str] | None = None,
    cb: Cb = None,
) -> dict[str, dict[str, dict]]:
    """
    Profile les parcelles d'un run pool (usage CLI / job séparé).
    Si idus=None, profile toutes les parcelles du run.
    """
    log = cb or (
        lambda msg: logger.info(
            "[cesbio_profile] project_id=%s run_id=%s %s",
            project_id,
            run_id,
            msg,
        )
    )

    with engine.connect() as conn:
        if idus is None:
            idus = [
                str(r)
                for r in conn.execute(
                    text(
                        """
                        SELECT idu
                        FROM ecocompensation_results.parcelles_pool
                        WHERE project_id = CAST(:pid AS uuid)
                          AND run_id = CAST(:rid AS uuid)
                        ORDER BY rank NULLS LAST, idu
                        """
                    ),
                    {"pid": project_id, "rid": run_id},
                ).scalars().all()
            ]
        return _profile_idus(conn, project_id, run_id, idus, log)
