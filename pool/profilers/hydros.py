"""
Proximité hydrographique des parcelles du pool (méthode zones humides).

S'appuie sur ecocompensation_results.troncons_hydros et surfaces_hydros
(clipées sur l'AOI projet). Réutilise les colonnes enrichies sur parcelles
quand disponibles, sinon recalcule la distance au plus proche cours d'eau /
point d'eau.
"""

from __future__ import annotations

from sqlalchemy import text

from .base import BasePoolProfiler


def _pick_nearest(items: object) -> dict | None:
    if not isinstance(items, list) or not items:
        return None
    first = items[0]
    if not isinstance(first, dict):
        return None
    return {
        "cleabs": first.get("cleabs"),
        "nom": first.get("nom"),
        "nature": first.get("nature"),
        "dist_m": first.get("dist_m"),
    }


class HydrosProfiler(BasePoolProfiler):
    metric_key = "hydros_proximite"

    def compute_for_run(self, conn, project_id: str, run_id: str) -> dict[str, dict]:
        rows = conn.execute(
            text(
                """
                SELECT
                    p.idu,
                    p.dist_hydro_m,
                    p.troncons_hydro_info,
                    p.dist_surface_hydro_m,
                    p.surface_hydro_ha,
                    p.surfaces_hydro_info,
                    (
                        SELECT ROUND(ST_Distance(p.geom_2154, th.geom_2154))::int
                        FROM ecocompensation_results.troncons_hydros th
                        WHERE th.project_id = p.project_id
                          AND th.geom_2154 IS NOT NULL
                        ORDER BY p.geom_2154 <-> th.geom_2154
                        LIMIT 1
                    ) AS fallback_troncon_m,
                    (
                        SELECT ROUND(ST_Distance(p.geom_2154, sh.geom_2154))::int
                        FROM ecocompensation_results.surfaces_hydros sh
                        WHERE sh.project_id = p.project_id
                          AND sh.geom_2154 IS NOT NULL
                        ORDER BY p.geom_2154 <-> sh.geom_2154
                        LIMIT 1
                    ) AS fallback_surface_m
                FROM ecocompensation_results.parcelles_pool pp
                JOIN ecocompensation_results.parcelles p
                  ON p.project_id = pp.project_id
                 AND p.idu = pp.idu
                WHERE pp.project_id = CAST(:project_id AS uuid)
                  AND pp.run_id = CAST(:run_id AS uuid)
                """
            ),
            {"project_id": project_id, "run_id": run_id},
        ).mappings().all()

        payload: dict[str, dict] = {}
        for r in rows:
            idu = str(r["idu"])
            troncon_m = r.get("dist_hydro_m")
            if troncon_m is None:
                troncon_m = r.get("fallback_troncon_m")
            surface_m = r.get("dist_surface_hydro_m")
            if surface_m is None:
                surface_m = r.get("fallback_surface_m")

            troncon_info = _pick_nearest(r.get("troncons_hydro_info"))
            surface_info = _pick_nearest(r.get("surfaces_hydro_info"))

            payload[idu] = {
                "nearest_troncon_m": int(troncon_m) if troncon_m is not None else None,
                "nearest_surface_m": int(surface_m) if surface_m is not None else None,
                "surface_hydro_ha": float(r.get("surface_hydro_ha") or 0),
                "nearest_troncon": troncon_info,
                "nearest_surface": surface_info,
            }
        return payload
