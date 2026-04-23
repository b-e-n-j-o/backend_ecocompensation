from __future__ import annotations

from collections import defaultdict
import logging

from sqlalchemy import text

from .base import BasePoolProfiler

log = logging.getLogger(__name__)

class EspecesProfiler(BasePoolProfiler):
    """
    Profiler faune par espèces sélectionnées dans le filtre (`faune_criteria` du run).

    Pour chaque parcelle du pool :
    - indique si elle intersecte au moins une observation de la liste d'espèces,
    - détaille les intersections par espèce (nb d'observations),
    - si aucune intersection, fournit la distance à l'observation la plus proche.
    """

    metric_key = "especes_faune"

    def _selected_criteria(self, conn, project_id: str, run_id: str) -> list[dict]:
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
        criteria = options.get("faune_criteria") if isinstance(options, dict) else None
        if not isinstance(criteria, list):
            return []

        cleaned: list[dict] = []
        for c in criteria:
            if not isinstance(c, dict):
                continue
            tax = str(c.get("tax_nom_val", "") or "").strip()
            if not tax:
                continue
            mode = str(c.get("mode", "") or "").strip().lower()
            radius = c.get("radius_m")
            radius_m = None
            if isinstance(radius, (int, float)):
                radius_m = max(0.0, float(radius))
            cleaned.append(
                {
                    "tax_nom_val": tax,
                    "mode": mode,
                    "radius_m": radius_m,
                }
            )
        return cleaned

    def compute_for_run(self, conn, project_id: str, run_id: str) -> dict[str, dict]:
        selected_criteria = self._selected_criteria(conn, project_id, run_id)
        if not selected_criteria:
            return {}

        # déduplication stable des espèces affichées
        seen_species = set()
        selected_species: list[str] = []
        for c in selected_criteria:
            s = str(c["tax_nom_val"]).strip()
            k = s.lower()
            if k in seen_species:
                continue
            seen_species.add(k)
            selected_species.append(s)

        species_norm = [s.lower().strip() for s in selected_species]
        within_radius = [
            float(c["radius_m"])
            for c in selected_criteria
            if c.get("mode") == "within_radius" and c.get("radius_m") is not None
        ]
        buffer_radius_max_m = max(within_radius) if within_radius else None

        # Intersections observées par parcelle et par espèce.
        inter_rows = conn.execute(
            text(
                """
                SELECT
                    p.idu,
                    COALESCE(NULLIF(TRIM(f.nom_vernaculaire), ''), 'non_renseigné') AS species_label,
                    COUNT(*)::int AS obs_count
                FROM ecocompensation_results.parcelles_pool pp
                JOIN ecocompensation_results.parcelles p
                  ON p.project_id = pp.project_id
                 AND p.idu = pp.idu
                JOIN ecocompensation_results.fauna f
                  ON f.project_id = pp.project_id
                 AND f.geom_2154 IS NOT NULL
                 AND lower(btrim(f.nom_vernaculaire::text)) = ANY(CAST(:species_norm AS text[]))
                 AND ST_Intersects(
                        ST_MakeValid(p.geom_2154),
                        ST_MakeValid(f.geom_2154)
                     )
                WHERE pp.project_id = CAST(:project_id AS uuid)
                  AND pp.run_id = CAST(:run_id AS uuid)
                GROUP BY p.idu, COALESCE(NULLIF(TRIM(f.nom_vernaculaire), ''), 'non_renseigné')
                """
            ),
            {"project_id": project_id, "run_id": run_id, "species_norm": species_norm},
        ).mappings().all()

        by_idu: dict[str, dict[str, int]] = defaultdict(dict)
        for r in inter_rows:
            idu = str(r["idu"])
            label = str(r["species_label"])
            by_idu[idu][label] = int(r["obs_count"] or 0)

        intersect_idus = {idu for idu, vals in by_idu.items() if vals}

        # Adjacence: parcelles du pool au contact d'une parcelle qui intersecte une espèce.
        adjacent_idus: set[str] = set()
        if intersect_idus:
            adjacent_rows = conn.execute(
                text(
                    """
                    SELECT DISTINCT p2.idu
                    FROM ecocompensation_results.parcelles_pool pp1
                    JOIN ecocompensation_results.parcelles p1
                      ON p1.project_id = pp1.project_id
                     AND p1.idu = pp1.idu
                    JOIN ecocompensation_results.parcelles_pool pp2
                      ON pp2.project_id = pp1.project_id
                     AND pp2.run_id = pp1.run_id
                    JOIN ecocompensation_results.parcelles p2
                      ON p2.project_id = pp2.project_id
                     AND p2.idu = pp2.idu
                    WHERE pp1.project_id = CAST(:project_id AS uuid)
                      AND pp1.run_id = CAST(:run_id AS uuid)
                      AND p1.idu = ANY(CAST(:inter_ids AS text[]))
                      AND p2.idu <> p1.idu
                      AND ST_Touches(ST_MakeValid(p2.geom_2154), ST_MakeValid(p1.geom_2154))
                    """
                ),
                {
                    "project_id": project_id,
                    "run_id": run_id,
                    "inter_ids": list(intersect_idus),
                },
            ).mappings().all()
            adjacent_idus = {str(r["idu"]) for r in adjacent_rows}
            log.info(
                "Especes profiler adjacency | project_id=%s run_id=%s inter_idus=%d adjacent_idus=%d",
                project_id,
                run_id,
                len(intersect_idus),
                len(adjacent_idus),
            )

        # Distance à l'observation la plus proche (dans la liste d'espèces).
        nearest_rows = conn.execute(
            text(
                """
                SELECT
                    p.idu,
                    nn.species_label,
                    nn.dist_m
                FROM ecocompensation_results.parcelles_pool pp
                JOIN ecocompensation_results.parcelles p
                  ON p.project_id = pp.project_id
                 AND p.idu = pp.idu
                LEFT JOIN LATERAL (
                    SELECT
                        COALESCE(NULLIF(TRIM(f.nom_vernaculaire), ''), 'non_renseigné') AS species_label,
                        ST_Distance(
                            ST_MakeValid(p.geom_2154),
                            ST_MakeValid(f.geom_2154)
                        ) AS dist_m
                    FROM ecocompensation_results.fauna f
                    WHERE f.project_id = pp.project_id
                      AND f.geom_2154 IS NOT NULL
                      AND lower(btrim(f.nom_vernaculaire::text)) = ANY(CAST(:species_norm AS text[]))
                    ORDER BY ST_MakeValid(p.geom_2154) <-> ST_MakeValid(f.geom_2154)
                    LIMIT 1
                ) nn ON TRUE
                WHERE pp.project_id = CAST(:project_id AS uuid)
                  AND pp.run_id = CAST(:run_id AS uuid)
                """
            ),
            {"project_id": project_id, "run_id": run_id, "species_norm": species_norm},
        ).mappings().all()

        payload: dict[str, dict] = {}
        for r in nearest_rows:
            idu = str(r["idu"])
            inter_by_species = by_idu.get(idu, {})
            intersects = bool(inter_by_species)
            total_obs = sum(inter_by_species.values()) if intersects else 0

            dist_m = r.get("dist_m")
            nearest_species = r.get("species_label")
            payload[idu] = {
                "selected_species": selected_species,
                "intersects_any": intersects,
                "adjacent_to_intersection": idu in adjacent_idus,
                "intersections_by_species": inter_by_species,
                "intersection_observation_count": total_obs,
                "nearest_observation_distance_m": None if dist_m is None else round(float(dist_m), 2),
                "nearest_species": None if nearest_species is None else str(nearest_species),
                "buffer_radius_max_m": buffer_radius_max_m,
                "within_buffer_any": (
                    False
                    if intersects or dist_m is None or buffer_radius_max_m is None
                    else float(dist_m) <= float(buffer_radius_max_m)
                ),
            }

        return payload

