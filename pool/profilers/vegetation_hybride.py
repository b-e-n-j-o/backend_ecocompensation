from __future__ import annotations

from collections import defaultdict

from sqlalchemy import text

from .base import BasePoolProfiler


class VegetationHybrideProfiler(BasePoolProfiler):
    metric_key = "vegetation_hybride_ratio"

    def compute_for_run(self, conn, project_id: str, run_id: str) -> dict[str, dict]:
        rows = conn.execute(
            text(
                """
                WITH intersections AS (
                    SELECT
                        p.idu,
                        COALESCE(v.source, 'inconnu') AS source,
                        COALESCE(v.libelle_prio, v.nature, v.libelle, 'inconnu') AS classe,
                        ST_Intersection(
                            ST_MakeValid(p.geom_2154),
                            ST_MakeValid(v.geom_2154)
                        ) AS inter_geom
                    FROM ecocompensation_results.parcelles_pool pp
                    JOIN ecocompensation_results.parcelles p
                      ON p.project_id = pp.project_id
                     AND p.idu = pp.idu
                    JOIN ecocompensation_results.bd_topo_et_cesbio v
                      ON v.project_id = pp.project_id
                     AND p.geom_2154 && v.geom_2154
                     AND ST_Intersects(ST_MakeValid(p.geom_2154), ST_MakeValid(v.geom_2154))
                    WHERE pp.project_id = CAST(:project_id AS uuid)
                      AND pp.run_id = CAST(:run_id AS uuid)
                ),
                cleaned AS (
                    SELECT idu, source, classe, inter_geom
                    FROM intersections
                    WHERE inter_geom IS NOT NULL
                      AND NOT ST_IsEmpty(inter_geom)
                ),
                bd_union AS (
                    SELECT
                        idu,
                        ST_UnaryUnion(ST_Collect(inter_geom)) AS g_bd
                    FROM cleaned
                    WHERE source = 'bdtopo'
                    GROUP BY idu
                ),
                prioritized AS (
                    SELECT idu, classe, inter_geom
                    FROM cleaned
                    WHERE source = 'bdtopo'

                    UNION ALL

                    SELECT
                        c.idu,
                        c.classe,
                        ST_Difference(
                            c.inter_geom,
                            COALESCE(b.g_bd, ST_GeomFromText('POLYGON EMPTY', 2154))
                        ) AS inter_geom
                    FROM cleaned c
                    LEFT JOIN bd_union b ON b.idu = c.idu
                    WHERE c.source = 'cesbio'
                ),
                prioritized_clean AS (
                    SELECT idu, classe, inter_geom
                    FROM prioritized
                    WHERE inter_geom IS NOT NULL
                      AND NOT ST_IsEmpty(inter_geom)
                ),
                class_areas AS (
                    SELECT
                        idu,
                        classe,
                        ST_Area(ST_UnaryUnion(ST_Collect(inter_geom))) AS inter_area
                    FROM prioritized_clean
                    GROUP BY idu, classe
                ),
                totals AS (
                    SELECT
                        idu,
                        SUM(inter_area) AS total_area
                    FROM class_areas
                    GROUP BY idu
                )
                SELECT
                    c.idu,
                    c.classe,
                    c.inter_area,
                    t.total_area
                FROM class_areas c
                JOIN totals t ON t.idu = c.idu
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
            total_area = float(r["total_area"] or 0.0)
            if area <= 0:
                continue
            by_idu[idu][classe] = by_idu[idu].get(classe, 0.0) + area
            if total_area > 0:
                totals[idu] = max(totals[idu], total_area)
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
