"""
Couverture EBC des parcelles déjà retenues dans le pool.

Croise ecocompensation.v_ebc. Ne filtre pas : la surface mobilisable hors EBC
contrainte et le signal ebc_a_creer sont exposés pour l'affichage (et, plus
tard, le score écologique).
"""

from __future__ import annotations

import logging
from collections import defaultdict

from sqlalchemy import text

from api.recherche_de_foncier.filtrage.layers.modules.espaces_boises_classe import (
    EBC_A_CREER_CLASSE,
    EBC_CONTRAINTE_CLASSES,
    EBC_VIEW,
)

from .base import BasePoolProfiler

logger = logging.getLogger(__name__)

_CONTRAINTE = set(EBC_CONTRAINTE_CLASSES)


class EspacesBoisesClassesProfiler(BasePoolProfiler):
    metric_key = "espaces_boises_classes"

    def compute_for_run(self, conn, project_id: str, run_id: str) -> dict[str, dict]:
        params = {"project_id": project_id, "run_id": run_id}
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
            params,
        ).mappings().all()
        parcel_area_by_idu = {
            str(r["idu"]): float(r["parcel_area_m2"] or 0.0) for r in parcel_rows
        }

        fast_sql = text(
            f"""
            SELECT
                p.idu,
                COALESCE(NULLIF(TRIM(e.classe), ''), 'non_renseigne') AS classe,
                SUM(ST_Area(ST_Intersection(p.geom_2154, e.geom_2154))) AS inter_area
            FROM ecocompensation_results.parcelles_pool pp
            JOIN ecocompensation_results.parcelles p
              ON p.project_id = pp.project_id
             AND p.idu = pp.idu
            JOIN {EBC_VIEW} e
              ON e.geom_2154 && p.geom_2154
             AND ST_Intersects(e.geom_2154, p.geom_2154)
            WHERE pp.project_id = CAST(:project_id AS uuid)
              AND pp.run_id = CAST(:run_id AS uuid)
              AND p.geom_2154 IS NOT NULL
              AND e.geom_2154 IS NOT NULL
            GROUP BY p.idu, COALESCE(NULLIF(TRIM(e.classe), ''), 'non_renseigne')
            """
        )
        safe_sql = text(
            f"""
            SELECT
                p.idu,
                COALESCE(NULLIF(TRIM(e.classe), ''), 'non_renseigne') AS classe,
                SUM(
                    ST_Area(
                        ST_Intersection(ST_MakeValid(p.geom_2154), ST_MakeValid(e.geom_2154))
                    )
                ) AS inter_area
            FROM ecocompensation_results.parcelles_pool pp
            JOIN ecocompensation_results.parcelles p
              ON p.project_id = pp.project_id
             AND p.idu = pp.idu
            JOIN {EBC_VIEW} e
              ON e.geom_2154 && p.geom_2154
             AND ST_Intersects(ST_MakeValid(e.geom_2154), ST_MakeValid(p.geom_2154))
            WHERE pp.project_id = CAST(:project_id AS uuid)
              AND pp.run_id = CAST(:run_id AS uuid)
              AND p.geom_2154 IS NOT NULL
              AND e.geom_2154 IS NOT NULL
            GROUP BY p.idu, COALESCE(NULLIF(TRIM(e.classe), ''), 'non_renseigne')
            """
        )
        try:
            rows = conn.execute(fast_sql, params).mappings().all()
        except Exception:
            logger.exception(
                "EBC profiler fast path failed; retrying with ST_MakeValid "
                "(project_id=%s, run_id=%s)",
                project_id,
                run_id,
            )
            rows = conn.execute(safe_sql, params).mappings().all()

        by_idu: dict[str, dict[str, float]] = defaultdict(dict)
        for r in rows:
            area = float(r["inter_area"] or 0.0)
            if area <= 0:
                continue
            by_idu[str(r["idu"])][str(r["classe"])] = area

        payload: dict[str, dict] = {}
        for idu, classes in by_idu.items():
            parcel_m2 = parcel_area_by_idu.get(idu, 0.0)
            if parcel_m2 <= 0:
                continue

            ratios: dict[str, float] = {}
            areas_ha: dict[str, float] = {}
            contrainte_m2 = 0.0
            a_creer_m2 = 0.0
            for classe, inter_m2 in classes.items():
                ratios[classe] = round(min(1.0, inter_m2 / parcel_m2), 6)
                areas_ha[classe] = round(inter_m2 / 10_000, 4)
                if classe == EBC_A_CREER_CLASSE:
                    a_creer_m2 += inter_m2
                elif classe in _CONTRAINTE:
                    contrainte_m2 += inter_m2

            contrainte_m2 = min(contrainte_m2, parcel_m2)
            a_creer_m2 = min(a_creer_m2, parcel_m2)
            mobilisable_m2 = max(0.0, parcel_m2 - contrainte_m2)
            payload[idu] = {
                "parcel_area_ha": round(parcel_m2 / 10_000, 4),
                "classes": ratios,
                "class_area_ha": areas_ha,
                "contrainte_ha": round(contrainte_m2 / 10_000, 4),
                "contrainte_ratio": round(contrainte_m2 / parcel_m2, 6),
                "mobilisable_ha": round(mobilisable_m2 / 10_000, 4),
                "mobilisable_ratio": round(mobilisable_m2 / parcel_m2, 6),
                "has_ebc_a_creer": a_creer_m2 > 0,
                "ebc_a_creer_ha": round(a_creer_m2 / 10_000, 4),
                "ebc_a_creer_ratio": round(a_creer_m2 / parcel_m2, 6),
            }
        return payload
