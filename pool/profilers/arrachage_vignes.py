from __future__ import annotations

from collections import defaultdict
import logging

from sqlalchemy import text

from .base import BasePoolProfiler

logger = logging.getLogger(__name__)


class ArrachageVignesProfiler(BasePoolProfiler):
    """
    Arrachage de vignes : surfaces d'intersection par catégorie `c_div_re_1`.

    Les ratios sont exprimés par rapport à la surface de la parcelle :
      ratio_k = aire(parcelle ∩ classe k) / aire(parcelle)
    """

    metric_key = "arrachage_vignes_ratio"

    def compute_for_run(self, conn, project_id: str, run_id: str) -> dict[str, dict]:
        parcel_rows = conn.execute(
            text(
                """
                SELECT DISTINCT
                    p.idu,
                    ST_Area(p.geom_2154) AS parcel_area_m2
                FROM ecocompensation_results.parcelles_pool pp
                JOIN ecocompensation_results.parcelles p
                  ON p.project_id = pp.project_id
                 AND p.idu = pp.idu
                WHERE pp.project_id = CAST(:project_id AS uuid)
                  AND pp.run_id = CAST(:run_id AS uuid)
                  AND p.geom_2154 IS NOT NULL
                """
            ),
            {"project_id": project_id, "run_id": run_id},
        ).mappings().all()
        parcel_area_by_idu: dict[str, float] = {
            str(r["idu"]): float(r["parcel_area_m2"] or 0.0) for r in parcel_rows
        }

        fast_sql = text(
            """
            SELECT
                p.idu,
                COALESCE(NULLIF(TRIM(a.c_div_re_1), ''), 'non_renseigné') AS classe,
                SUM(ST_Area(ST_Intersection(p.geom_2154, a.geom_2154))) AS inter_area,
                BOOL_OR(lower(btrim(COALESCE(a.c_div_rena, ''))) = 'renaturation') AS has_renaturation
            FROM ecocompensation_results.parcelles_pool pp
            JOIN ecocompensation_results.parcelles p
              ON p.project_id = pp.project_id
             AND p.idu = pp.idu
            JOIN ecocompensation.arrachage_vignes a
              ON a.geom_2154 && p.geom_2154
             AND ST_Intersects(a.geom_2154, p.geom_2154)
            WHERE pp.project_id = CAST(:project_id AS uuid)
              AND pp.run_id = CAST(:run_id AS uuid)
              AND p.geom_2154 IS NOT NULL
              AND a.geom_2154 IS NOT NULL
            GROUP BY p.idu, COALESCE(NULLIF(TRIM(a.c_div_re_1), ''), 'non_renseigné')
            """
        )
        safe_sql = text(
            """
            SELECT
                p.idu,
                COALESCE(NULLIF(TRIM(a.c_div_re_1), ''), 'non_renseigné') AS classe,
                SUM(
                    ST_Area(
                        ST_Intersection(ST_MakeValid(p.geom_2154), ST_MakeValid(a.geom_2154))
                    )
                ) AS inter_area,
                BOOL_OR(lower(btrim(COALESCE(a.c_div_rena, ''))) = 'renaturation') AS has_renaturation
            FROM ecocompensation_results.parcelles_pool pp
            JOIN ecocompensation_results.parcelles p
              ON p.project_id = pp.project_id
             AND p.idu = pp.idu
            JOIN ecocompensation.arrachage_vignes a
              ON a.geom_2154 && p.geom_2154
             AND ST_Intersects(ST_MakeValid(a.geom_2154), ST_MakeValid(p.geom_2154))
            WHERE pp.project_id = CAST(:project_id AS uuid)
              AND pp.run_id = CAST(:run_id AS uuid)
              AND p.geom_2154 IS NOT NULL
              AND a.geom_2154 IS NOT NULL
            GROUP BY p.idu, COALESCE(NULLIF(TRIM(a.c_div_re_1), ''), 'non_renseigné')
            """
        )
        try:
            rows = conn.execute(
                fast_sql,
                {"project_id": project_id, "run_id": run_id},
            ).mappings().all()
        except Exception:
            logger.exception(
                "Arrachage profiler fast path failed; retrying with ST_MakeValid fallback "
                "(project_id=%s, run_id=%s)",
                project_id,
                run_id,
            )
            rows = conn.execute(
                safe_sql,
                {"project_id": project_id, "run_id": run_id},
            ).mappings().all()

        by_idu: dict[str, dict[str, float]] = defaultdict(dict)
        has_renat_by_idu: dict[str, bool] = defaultdict(bool)
        for r in rows:
            idu = str(r["idu"])
            classe = str(r["classe"])
            area = float(r["inter_area"] or 0.0)
            if area <= 0:
                continue
            by_idu[idu][classe] = by_idu[idu].get(classe, 0.0) + area
            has_renat_by_idu[idu] = has_renat_by_idu[idu] or bool(r.get("has_renaturation"))

        payload: dict[str, dict] = {}
        for idu, classes in by_idu.items():
            parcel_area = parcel_area_by_idu.get(idu, 0.0)
            if parcel_area <= 0:
                continue

            ratios: dict[str, float] = {}
            impacted_area = 0.0
            for k, inter in classes.items():
                if inter <= 0:
                    continue
                ratios[k] = round(min(1.0, inter / parcel_area), 6)
                impacted_area += inter

            if not ratios:
                continue

            payload[idu] = {
                "ratios": ratios,
                "total_intersection_area_m2": round(parcel_area, 3),
                "impacted_area_m2": round(impacted_area, 3),
                "impacted_ratio": round(min(1.0, impacted_area / parcel_area), 6),
                "has_renaturation": bool(has_renat_by_idu.get(idu, False)),
            }
        return payload
