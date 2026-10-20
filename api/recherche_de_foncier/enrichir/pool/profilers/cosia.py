from __future__ import annotations

import logging
import os
import time
from collections import defaultdict

from sqlalchemy import bindparam, text

from .base import BasePoolProfiler

logger = logging.getLogger(__name__)

# Table nationale ; filtre département aligné sur le code INSEE de la parcelle (2 premiers caractères).
DEFAULT_COSIA_TABLE = "geo.cosia"
# Réduit le risque de statement_timeout en découpant le JOIN lourd COSIA × parcelles.
DEFAULT_BATCH_SIZE = 10


class CosiaProfiler(BasePoolProfiler):
    """Zonages COSIA (IGN) : surfaces d’intersection par `classe`, relatives à l’union des intersections."""

    metric_key = "cosia_zonage_ratio"

    def _pool_idus(self, conn, project_id: str, run_id: str) -> list[str]:
        rows = conn.execute(
            text(
                """
                SELECT DISTINCT pp.idu
                FROM ecocompensation_results.parcelles_pool pp
                WHERE pp.project_id = CAST(:project_id AS uuid)
                  AND pp.run_id = CAST(:run_id AS uuid)
                ORDER BY pp.idu
                """
            ),
            {"project_id": project_id, "run_id": run_id},
        ).fetchall()
        return [str(r[0]) for r in rows if r[0]]

    def _surface_ha_by_idu(
        self, conn, project_id: str, run_id: str
    ) -> dict[str, float | None]:
        rows = conn.execute(
            text(
                """
                SELECT pp.idu, pp.surface_ha
                FROM ecocompensation_results.parcelles_pool pp
                WHERE pp.project_id = CAST(:project_id AS uuid)
                  AND pp.run_id = CAST(:run_id AS uuid)
                """
            ),
            {"project_id": project_id, "run_id": run_id},
        ).fetchall()
        out: dict[str, float | None] = {}
        for r in rows:
            idu = str(r[0]) if r[0] is not None else ""
            if not idu:
                continue
            sh = r[1]
            try:
                out[idu] = float(sh) if sh is not None else None
            except (TypeError, ValueError):
                out[idu] = None
        return out

    @staticmethod
    def _format_idu_batch(batch: list[str], max_show: int = 6) -> str:
        if len(batch) <= max_show:
            return ", ".join(batch)
        shown = batch[:max_show]
        return ", ".join(shown) + f" … (+{len(batch) - max_show})"

    def compute_for_run(self, conn, project_id: str, run_id: str) -> dict[str, dict]:
        ctable = os.getenv("POOL_COSIA_TABLE", DEFAULT_COSIA_TABLE)
        batch_size = max(1, int(os.getenv("POOL_COSIA_BATCH_SIZE", str(DEFAULT_BATCH_SIZE))))

        idus = self._pool_idus(conn, project_id, run_id)
        if not idus:
            return {}

        surfaces = self._surface_ha_by_idu(conn, project_id, run_id)
        n_batches = (len(idus) + batch_size - 1) // batch_size
        max_ha = max((surfaces.get(i) or 0.0) for i in idus) if idus else 0.0
        logger.info(
            "COSIA [%s] START run_id=%s table=%s parcelles=%d batch_size=%d -> %d lot(s) "
            "(surface max %.2f ha si connue)",
            project_id[:8],
            run_id[:8],
            ctable,
            len(idus),
            batch_size,
            n_batches,
            max_ha,
        )

        sql = (
            text(
                f"""
                SELECT
                    p.idu,
                    COALESCE(NULLIF(TRIM(c.classe), ''), 'non_renseigné') AS classe,
                    SUM(
                        ST_Area(
                            ST_Intersection(
                                ST_MakeValid(p.geom_2154),
                                ST_MakeValid(c.geom_2154)
                            )
                        )
                    ) AS inter_area
                FROM ecocompensation_results.parcelles_pool pp
                JOIN ecocompensation_results.parcelles p
                  ON p.project_id = pp.project_id
                 AND p.idu = pp.idu
                JOIN {ctable} c
                  ON c.geom_2154 && p.geom_2154
                 AND ST_Intersects(ST_MakeValid(p.geom_2154), ST_MakeValid(c.geom_2154))
                 AND p.code_insee IS NOT NULL
                 AND LENGTH(TRIM(p.code_insee)) >= 2
                 AND c.dpt = LEFT(TRIM(p.code_insee), 2)
                WHERE pp.project_id = CAST(:project_id AS uuid)
                  AND pp.run_id = CAST(:run_id AS uuid)
                  AND pp.idu IN :idus_batch
                GROUP BY p.idu, COALESCE(NULLIF(TRIM(c.classe), ''), 'non_renseigné')
                """
            ).bindparams(bindparam("idus_batch", expanding=True))
        )

        by_idu: dict[str, dict[str, float]] = defaultdict(dict)
        totals: dict[str, float] = defaultdict(float)

        batch_num = 0
        t_run0 = time.perf_counter()
        for i in range(0, len(idus), batch_size):
            batch = idus[i : i + batch_size]
            batch_num += 1
            ha_in_batch = [
                surfaces.get(x)
                for x in batch
                if surfaces.get(x) is not None
            ]
            max_batch_ha = max(ha_in_batch) if ha_in_batch else None
            if max_batch_ha is not None and max_batch_ha >= 50:
                logger.warning(
                    "COSIA [%s] lot %d/%d contient une parcelle >= 50 ha (max %.1f ha) — requête souvent lente",
                    project_id[:8],
                    batch_num,
                    n_batches,
                    max_batch_ha,
                )
            logger.info(
                "COSIA [%s] lot %d/%d EN COURS idus=[%s] — exécution SQL (JOIN %s × parcelles)…",
                project_id[:8],
                batch_num,
                n_batches,
                self._format_idu_batch(batch),
                ctable,
            )
            t0 = time.perf_counter()
            rows = conn.execute(
                sql,
                {"project_id": project_id, "run_id": run_id, "idus_batch": batch},
            ).mappings().all()
            dt = time.perf_counter() - t0
            n_classes = len(rows)
            logger.info(
                "COSIA [%s] lot %d/%d DONE duration_s=%.2f lignes_agrégées=%d (lignes classe × idu)",
                project_id[:8],
                batch_num,
                n_batches,
                dt,
                n_classes,
            )

            for r in rows:
                idu = str(r["idu"])
                classe = str(r["classe"])
                area = float(r["inter_area"] or 0.0)
                if area <= 0:
                    continue
                by_idu[idu][classe] = by_idu[idu].get(classe, 0.0) + area
                totals[idu] += area

        payload: dict[str, dict] = {}
        for idu, classes in by_idu.items():
            total = totals.get(idu, 0.0)
            if total <= 0:
                continue
            ratios = {k: round(v / total, 6) for k, v in classes.items() if v > 0}
            payload[idu] = {
                "ratios": ratios,
                "total_intersection_area_m2": round(total, 3),
            }
        total_s = time.perf_counter() - t_run0
        logger.info(
            "COSIA [%s] END run_id=%s parcelles_avec_ratio=%d total_profiler_s=%.2f",
            project_id[:8],
            run_id[:8],
            len(payload),
            total_s,
        )
        return payload
