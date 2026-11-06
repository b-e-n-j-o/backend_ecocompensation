from __future__ import annotations

import logging

from sqlalchemy import text

from .base import BasePoolProfiler

log = logging.getLogger(__name__)


class FloreProfiler(BasePoolProfiler):
    """
    Distance de chaque parcelle du pool aux observations flore du filtre.

    Réutilisé à l'enrichissement du run, y compris pour une parcelle ajoutée
    manuellement : le calcul repart de ``ecocompensation.flore_observation``.
    """

    metric_key = "especes_flore"

    def _criteria(self, conn, project_id: str, run_id: str) -> list[dict]:
        row = conn.execute(
            text(
                """
                SELECT options_json
                FROM ecocompensation_results.parcelles_pool_runs
                WHERE project_id = CAST(:project_id AS uuid)
                  AND id = CAST(:run_id AS uuid)
                """
            ),
            {"project_id": project_id, "run_id": run_id},
        ).mappings().one_or_none()
        if not row:
            return []
        options = row.get("options_json") or {}
        raw = options.get("flora_criteria") if isinstance(options, dict) else None
        if not isinstance(raw, list):
            return []
        out: list[dict] = []
        for c in raw:
            if not isinstance(c, dict):
                continue
            key = str(c.get("taxon_key") or "").strip()
            label = str(c.get("label") or key).strip()
            if not key or not label:
                continue
            try:
                dist_m = float(c.get("dist_m") or 0)
            except (TypeError, ValueError):
                dist_m = 0.0
            out.append({"taxon_key": key, "label": label, "dist_m": max(0.0, dist_m)})
        return out

    def compute_for_run(self, conn, project_id: str, run_id: str) -> dict[str, dict]:
        criteria = self._criteria(conn, project_id, run_id)
        if not criteria:
            return {}

        idus = [
            str(r["idu"])
            for r in conn.execute(
                text(
                    """
                    SELECT idu
                    FROM ecocompensation_results.parcelles_pool
                    WHERE project_id = CAST(:project_id AS uuid)
                      AND run_id = CAST(:run_id AS uuid)
                    """
                ),
                {"project_id": project_id, "run_id": run_id},
            ).mappings().all()
        ]
        if not idus:
            return {}

        by_idu: dict[str, list[dict]] = {idu: [] for idu in idus}
        for crit in criteria:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        p.idu,
                        COALESCE(
                            (
                                SELECT ROUND(MIN(ST_Distance(p.geom_2154, o.geom)))::int
                                FROM ecocompensation.flore_observation o
                                WHERE o.taxon_key = :taxon_key
                                  AND o.geom IS NOT NULL
                                  AND ST_DWithin(p.geom_2154, o.geom, CAST(:dist_m AS float8))
                            ),
                            -1
                        ) AS dist_m
                    FROM ecocompensation_results.parcelles_pool pp
                    JOIN ecocompensation_results.parcelles p
                      ON p.project_id = pp.project_id
                     AND p.idu = pp.idu
                    WHERE pp.project_id = CAST(:project_id AS uuid)
                      AND pp.run_id = CAST(:run_id AS uuid)
                      AND p.geom_2154 IS NOT NULL
                    """
                ),
                {
                    "project_id": project_id,
                    "run_id": run_id,
                    "taxon_key": crit["taxon_key"],
                    "dist_m": crit["dist_m"],
                },
            ).mappings().all()
            seen = set()
            for r in rows:
                idu = str(r["idu"])
                dist = int(r["dist_m"])
                within = dist >= 0
                by_idu.setdefault(idu, []).append(
                    {
                        "taxon_key": crit["taxon_key"],
                        "label": crit["label"],
                        "dist_m": dist,
                        "radius_m": crit["dist_m"],
                        "within_radius": within,
                    }
                )
                seen.add(idu)
            for idu in idus:
                if idu not in seen:
                    by_idu[idu].append(
                        {
                            "taxon_key": crit["taxon_key"],
                            "label": crit["label"],
                            "dist_m": -1,
                            "radius_m": crit["dist_m"],
                            "within_radius": False,
                        }
                    )

        log.info(
            "[flore] compute_for_run project_id=%s run_id=%s idu=%d taxons=%d",
            project_id,
            run_id,
            len(by_idu),
            len(criteria),
        )
        return {
            idu: {
                "taxa": taxa,
                "intersects_any": any(t["within_radius"] for t in taxa),
            }
            for idu, taxa in by_idu.items()
        }
