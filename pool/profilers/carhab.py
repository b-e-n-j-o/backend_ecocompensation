from __future__ import annotations

from collections import defaultdict

from sqlalchemy import text

from .base import BasePoolProfiler


class CarhabProfiler(BasePoolProfiler):
    """
    Zonages CARHAB : surfaces d'intersection par `nom_eunis`.

    Les classes CARHAB peuvent se recouvrir sur la même parcelle. Les ratios ne sont
    pas normalisés par la somme des intersections (ce qui diluait les parts), mais par
    la surface de la parcelle : ratio_k = aire(parcelle ∩ classe k) / aire(parcelle)
    (plafonné à 1). Plusieurs classes peuvent ainsi être à 100 % si chacune couvre
    toute la parcelle.
    """

    metric_key = "carhab_eunis_ratio"

    def compute_for_run(self, conn, project_id: str, run_id: str) -> dict[str, dict]:
        parcel_rows = conn.execute(
            text("""
                SELECT DISTINCT
                    p.idu,
                    ST_Area(ST_MakeValid(p.geom_2154)) AS parcel_area_m2
                FROM ecocompensation_results.parcelles_pool pp
                JOIN ecocompensation_results.parcelles p
                  ON p.project_id = pp.project_id
                 AND p.idu = pp.idu
                WHERE pp.project_id = CAST(:project_id AS uuid)
                  AND pp.run_id = CAST(:run_id AS uuid)
            """),
            {"project_id": project_id, "run_id": run_id},
        ).mappings().all()
        parcel_area_by_idu: dict[str, float] = {
            str(r["idu"]): float(r["parcel_area_m2"] or 0.0) for r in parcel_rows
        }

        # Jointure en EPSG:4326 : parcelle transformée pour l’index sur c.geom.
        rows = conn.execute(
            text("""
                SELECT
                    p.idu,
                    COALESCE(NULLIF(TRIM(c.nom_eunis), ''), 'non_renseigné') AS classe,
                    SUM(
                        ST_Area(
                            ST_Transform(
                                ST_Intersection(
                                    ST_Transform(ST_MakeValid(p.geom_2154), 4326),
                                    ST_MakeValid(c.geom)
                                ),
                                2154
                            )
                        )
                    ) AS inter_area
                FROM ecocompensation_results.parcelles_pool pp
                JOIN ecocompensation_results.parcelles p
                  ON p.project_id = pp.project_id
                 AND p.idu = pp.idu
                JOIN ecocompensation.carhab_clean c
                  ON ST_Intersects(
                        c.geom,
                        ST_Transform(ST_MakeValid(p.geom_2154), 4326)
                     )
                WHERE pp.project_id = CAST(:project_id AS uuid)
                  AND pp.run_id = CAST(:run_id AS uuid)
                GROUP BY p.idu, COALESCE(NULLIF(TRIM(c.nom_eunis), ''), 'non_renseigné')
            """),
            {"project_id": project_id, "run_id": run_id},
        ).mappings().all()

        by_idu: dict[str, dict[str, float]] = defaultdict(dict)
        for r in rows:
            idu = str(r["idu"])
            classe = str(r["classe"])
            area = float(r["inter_area"] or 0.0)
            if area <= 0:
                continue
            by_idu[idu][classe] = by_idu[idu].get(classe, 0.0) + area

        payload: dict[str, dict] = {}
        for idu, classes in by_idu.items():
            parcel_area = parcel_area_by_idu.get(idu, 0.0)
            if parcel_area <= 0:
                continue
            ratios: dict[str, float] = {}
            for k, inter in classes.items():
                if inter <= 0:
                    continue
                ratios[k] = round(min(1.0, inter / parcel_area), 6)
            if not ratios:
                continue
            payload[idu] = {
                "ratios": ratios,
                # Référence pour l’UI : surface parcelle (dénominateur des %), pas la somme des intersections.
                "total_intersection_area_m2": round(parcel_area, 3),
            }
        return payload
