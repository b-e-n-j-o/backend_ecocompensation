from __future__ import annotations

from sqlalchemy import text

from .base import BasePoolProfiler


class ParcelScoreV1Profiler(BasePoolProfiler):
    """
    Score parcelle v1 (0..9) :
    - Espèces faune: intersection=3, adjacente à une parcelle intersectée=2, dans buffer=1, sinon 0
    - Distance centre: <3km=3, 3-7=2, 7-10=1, >10=0
    - Surface: +1 si surface_ha >= 2 * min_area_ha (filtre)
    - Arrachage: +1 si intersection arrachage ET au moins une classe `c_div_rena='renaturation'`
    - Personnes morales: +1 si `parcelles_personnes_morales.intersects_pm_database` (métrique déjà calculée dans le même run)
    """

    metric_key = "parcel_score_v1"

    def _filter_options(self, conn, project_id: str, run_id: str) -> dict:
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
            return {}
        opts = row.get("options_json") or {}
        return opts if isinstance(opts, dict) else {}

    def compute_for_run(self, conn, project_id: str, run_id: str) -> dict[str, dict]:
        opts = self._filter_options(conn, project_id, run_id)
        target_surface_ha = float(opts.get("min_area_ha") or 0.0)

        faune_criteria = opts.get("faune_criteria")
        if not isinstance(faune_criteria, list):
            faune_criteria = []
        selected_species = []
        radius_candidates = []
        for c in faune_criteria:
            if not isinstance(c, dict):
                continue
            tax = str(c.get("tax_nom_val", "") or "").strip()
            if tax:
                selected_species.append(tax)
            if str(c.get("mode", "") or "").strip().lower() == "within_radius":
                r = c.get("radius_m")
                if isinstance(r, (int, float)):
                    radius_candidates.append(max(0.0, float(r)))

        # dédup stable
        seen = set()
        selected_species = [s for s in selected_species if not (s.lower() in seen or seen.add(s.lower()))]
        species_norm = [s.lower().strip() for s in selected_species]
        buffer_radius_m = max(radius_candidates) if radius_candidates else None

        # Base parcelles du pool (distance/surface déjà calculées).
        base_rows = conn.execute(
            text(
                """
                SELECT
                    p.idu,
                    pp.distance_km,
                    pp.surface_ha
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

        # Espèces : set intersection
        intersect_ids: set[str] = set()
        if species_norm:
            rows = conn.execute(
                text(
                    """
                    SELECT DISTINCT p.idu
                    FROM ecocompensation_results.parcelles_pool pp
                    JOIN ecocompensation_results.parcelles p
                      ON p.project_id = pp.project_id
                     AND p.idu = pp.idu
                    JOIN ecocompensation_results.fauna f
                      ON f.project_id = pp.project_id
                     AND f.geom_2154 IS NOT NULL
                     AND lower(btrim(f.nom_vernaculaire::text)) = ANY(CAST(:species_norm AS text[]))
                     AND ST_Intersects(ST_MakeValid(p.geom_2154), ST_MakeValid(f.geom_2154))
                    WHERE pp.project_id = CAST(:project_id AS uuid)
                      AND pp.run_id = CAST(:run_id AS uuid)
                    """
                ),
                {"project_id": project_id, "run_id": run_id, "species_norm": species_norm},
            ).mappings().all()
            intersect_ids = {str(r["idu"]) for r in rows}

        # Espèces : set adjacence aux parcelles intersectées
        adjacent_ids: set[str] = set()
        if intersect_ids:
            rows = conn.execute(
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
                {"project_id": project_id, "run_id": run_id, "inter_ids": list(intersect_ids)},
            ).mappings().all()
            adjacent_ids = {str(r["idu"]) for r in rows}

        # Espèces : set dans buffer (si pas déjà intersection)
        within_buffer_ids: set[str] = set()
        if species_norm and buffer_radius_m is not None:
            rows = conn.execute(
                text(
                    """
                    SELECT DISTINCT p.idu
                    FROM ecocompensation_results.parcelles_pool pp
                    JOIN ecocompensation_results.parcelles p
                      ON p.project_id = pp.project_id
                     AND p.idu = pp.idu
                    JOIN ecocompensation_results.fauna f
                      ON f.project_id = pp.project_id
                     AND f.geom_2154 IS NOT NULL
                     AND lower(btrim(f.nom_vernaculaire::text)) = ANY(CAST(:species_norm AS text[]))
                     AND ST_DWithin(ST_MakeValid(p.geom_2154), ST_MakeValid(f.geom_2154), :radius_m)
                    WHERE pp.project_id = CAST(:project_id AS uuid)
                      AND pp.run_id = CAST(:run_id AS uuid)
                    """
                ),
                {
                    "project_id": project_id,
                    "run_id": run_id,
                    "species_norm": species_norm,
                    "radius_m": float(buffer_radius_m),
                },
            ).mappings().all()
            within_buffer_ids = {str(r["idu"]) for r in rows}

        # Arrachage renaturation
        renat_ids: set[str] = set()
        rows = conn.execute(
            text(
                """
                SELECT DISTINCT p.idu
                FROM ecocompensation_results.parcelles_pool pp
                JOIN ecocompensation_results.parcelles p
                  ON p.project_id = pp.project_id
                 AND p.idu = pp.idu
                JOIN ecocompensation.arrachage_vignes a
                  ON ST_Intersects(ST_MakeValid(p.geom_2154), ST_MakeValid(a.geom_2154))
                WHERE pp.project_id = CAST(:project_id AS uuid)
                  AND pp.run_id = CAST(:run_id AS uuid)
                  AND lower(btrim(COALESCE(a.c_div_rena, ''))) = 'renaturation'
                """
            ),
            {"project_id": project_id, "run_id": run_id},
        ).mappings().all()
        renat_ids = {str(r["idu"]) for r in rows}

        # Métrique PM déjà écrite par PersonnesMoralesProfiler (même transaction / run avant ce profiler).
        pm_rows = conn.execute(
            text(
                """
                SELECT idu, metric_value_jsonb
                FROM ecocompensation_results.parcelles_pool_metrics
                WHERE project_id = CAST(:project_id AS uuid)
                  AND run_id = CAST(:run_id AS uuid)
                  AND metric_key = 'parcelles_personnes_morales'
                """
            ),
            {"project_id": project_id, "run_id": run_id},
        ).mappings().all()
        pm_repertorie: dict[str, bool] = {}
        for r in pm_rows:
            idu_pm = str(r["idu"])
            j = r["metric_value_jsonb"]
            if isinstance(j, dict) and j.get("intersects_pm_database") is True:
                pm_repertorie[idu_pm] = True

        payload: dict[str, dict] = {}
        for r in base_rows:
            idu = str(r["idu"])
            distance_km = float(r["distance_km"] or 0.0)
            surface_ha = float(r["surface_ha"] or 0.0)

            # Espèces
            if idu in intersect_ids:
                species_points = 3
                species_reason = "intersection"
            elif idu in adjacent_ids:
                species_points = 2
                species_reason = "adjacent_to_intersection"
            elif idu in within_buffer_ids:
                species_points = 1
                species_reason = "within_buffer"
            else:
                species_points = 0
                species_reason = "outside_buffer"

            # Distance
            if distance_km < 3:
                distance_points = 3
                distance_bucket = "<3km"
            elif distance_km <= 7:
                distance_points = 2
                distance_bucket = "3-7km"
            elif distance_km <= 10:
                distance_points = 1
                distance_bucket = "7-10km"
            else:
                distance_points = 0
                distance_bucket = ">10km"

            # Surface
            surface_points = 1 if target_surface_ha > 0 and surface_ha >= 2 * target_surface_ha else 0

            # Arrachage renaturation
            arrachage_points = 1 if idu in renat_ids else 0

            # Personne morale (base PPM)
            pm_points = 1 if pm_repertorie.get(idu) else 0

            total_score = (
                species_points + distance_points + surface_points + arrachage_points + pm_points
            )
            payload[idu] = {
                "total_score": int(total_score),
                "max_score": 9,
                "breakdown": {
                    "especes": {
                        "points": species_points,
                        "reason": species_reason,
                        "buffer_radius_max_m": buffer_radius_m,
                    },
                    "distance": {
                        "points": distance_points,
                        "distance_km": round(distance_km, 3),
                        "bucket": distance_bucket,
                    },
                    "surface": {
                        "points": surface_points,
                        "surface_ha": round(surface_ha, 4),
                        "target_ha": round(target_surface_ha, 4),
                    },
                    "arrachage": {
                        "points": arrachage_points,
                        "reason": "renaturation" if arrachage_points else "not_concerned_or_not_renaturation",
                    },
                    "personnes_morales": {
                        "points": pm_points,
                        "reason": (
                            "repertoire_pm" if pm_points else "not_repertoire_pm"
                        ),
                    },
                },
            }

        return payload

