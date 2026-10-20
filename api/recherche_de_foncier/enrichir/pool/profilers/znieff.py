"""
Alerte ZNIEFF de type 2 sur les parcelles retenues.

Le type 1 est une exclusion de filtrage : il n'arrive pas dans le pool.
Le type 2, laissé passer par défaut, est signalé ici avec la surface
d'intersection, sans écarter la parcelle.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from sqlalchemy import text

from .base import BasePoolProfiler

logger = logging.getLogger(__name__)


class ZnieffProfiler(BasePoolProfiler):
    metric_key = "znieff_2"

    def compute_for_run(self, conn, project_id: str, run_id: str) -> dict[str, dict]:
        params = {"project_id": project_id, "run_id": run_id}
        parcel_rows = conn.execute(
            text(
                """
                SELECT DISTINCT p.idu, ST_Area(p.geom_2154) AS parcel_area_m2
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
            """
            SELECT
                p.idu,
                z.cle,
                z.nom_site,
                z.id_mnhn,
                SUM(ST_Area(ST_Intersection(p.geom_2154, z.geom_2154))) AS inter_area
            FROM ecocompensation_results.parcelles_pool pp
            JOIN ecocompensation_results.parcelles p
              ON p.project_id = pp.project_id
             AND p.idu = pp.idu
            JOIN ecocompensation.znieff z
              ON z.type_znieff = '2'
             AND z.geom_2154 && p.geom_2154
             AND ST_Intersects(z.geom_2154, p.geom_2154)
            WHERE pp.project_id = CAST(:project_id AS uuid)
              AND pp.run_id = CAST(:run_id AS uuid)
              AND p.geom_2154 IS NOT NULL
              AND z.geom_2154 IS NOT NULL
            GROUP BY p.idu, z.cle, z.nom_site, z.id_mnhn
            """
        )
        safe_sql = text(
            """
            SELECT
                p.idu,
                z.cle,
                z.nom_site,
                z.id_mnhn,
                SUM(
                    ST_Area(
                        ST_Intersection(ST_MakeValid(p.geom_2154), ST_MakeValid(z.geom_2154))
                    )
                ) AS inter_area
            FROM ecocompensation_results.parcelles_pool pp
            JOIN ecocompensation_results.parcelles p
              ON p.project_id = pp.project_id
             AND p.idu = pp.idu
            JOIN ecocompensation.znieff z
              ON z.type_znieff = '2'
             AND z.geom_2154 && p.geom_2154
             AND ST_Intersects(ST_MakeValid(z.geom_2154), ST_MakeValid(p.geom_2154))
            WHERE pp.project_id = CAST(:project_id AS uuid)
              AND pp.run_id = CAST(:run_id AS uuid)
              AND p.geom_2154 IS NOT NULL
              AND z.geom_2154 IS NOT NULL
            GROUP BY p.idu, z.cle, z.nom_site, z.id_mnhn
            """
        )
        try:
            rows = conn.execute(fast_sql, params).mappings().all()
        except Exception:
            logger.exception(
                "ZNIEFF profiler fast path failed; retrying with ST_MakeValid "
                "(project_id=%s, run_id=%s)",
                project_id,
                run_id,
            )
            rows = conn.execute(safe_sql, params).mappings().all()

        sites_by_idu: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            area_m2 = float(r["inter_area"] or 0.0)
            if area_m2 <= 0:
                continue
            sites_by_idu[str(r["idu"])].append(
                {
                    "cle": r.get("cle"),
                    "nom_site": (r.get("nom_site") or "").strip() or None,
                    "id_mnhn": (r.get("id_mnhn") or "").strip() or None,
                    "area_m2": area_m2,
                }
            )

        payload: dict[str, dict] = {}
        for idu, sites in sites_by_idu.items():
            parcel_m2 = parcel_area_by_idu.get(idu, 0.0)
            if parcel_m2 <= 0:
                continue
            inter_m2 = min(sum(s["area_m2"] for s in sites), parcel_m2)
            listed = []
            for s in sorted(sites, key=lambda item: item["area_m2"], reverse=True):
                listed.append(
                    {
                        "cle": s["cle"],
                        "nom_site": s["nom_site"],
                        "id_mnhn": s["id_mnhn"],
                        "area_ha": round(s["area_m2"] / 10_000, 4),
                        "ratio": round(min(1.0, s["area_m2"] / parcel_m2), 6),
                    }
                )
            payload[idu] = {
                "parcel_area_ha": round(parcel_m2 / 10_000, 4),
                "znieff2_ha": round(inter_m2 / 10_000, 4),
                "znieff2_ratio": round(inter_m2 / parcel_m2, 6),
                "sites": listed,
            }
        return payload
