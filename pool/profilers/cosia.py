from __future__ import annotations

import os
from collections import defaultdict

from sqlalchemy import text

from .base import BasePoolProfiler

# Table nationale ; filtre département aligné sur le code INSEE de la parcelle (2 premiers caractères).
DEFAULT_COSIA_TABLE = "geo.cosia"


class CosiaProfiler(BasePoolProfiler):
    """Zonages COSIA (IGN) : surfaces d’intersection par `classe`, relatives à l’union des intersections."""

    metric_key = "cosia_zonage_ratio"

    def compute_for_run(self, conn, project_id: str, run_id: str) -> dict[str, dict]:
        ctable = os.getenv("POOL_COSIA_TABLE", DEFAULT_COSIA_TABLE)
        rows = conn.execute(
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
                GROUP BY p.idu, COALESCE(NULLIF(TRIM(c.classe), ''), 'non_renseigné')
                """
            ),
            {"project_id": project_id, "run_id": run_id},
        ).mappings().all()
        by_idu: dict[str, dict[str, float]] = defaultdict(dict)
        totals: dict[str, float] = defaultdict(float)
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
        return payload
