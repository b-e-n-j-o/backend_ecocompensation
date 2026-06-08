from __future__ import annotations

import logging
import time

from sqlalchemy import text

from .base import BasePoolProfiler

log = logging.getLogger(__name__)

ECO_MAX = 6


class ScoreEcoProfiler(BasePoolProfiler):
    """
    Score écologique (0..6) :
    - distance parcelle ↔ projet (``parcelles_pool.distance_km``) : 0..3 ;
    - espèces faune : 0..3 depuis ``especes_faune`` (legacy) ou ``filter_enrich.fauna_distances`` (filter_v2).
    """

    metric_key = "score_eco"

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

    @staticmethod
    def _fauna_criteria(opts: dict) -> list[dict]:
        crit = opts.get("fauna_criteria")
        if not isinstance(crit, list):
            return []
        out: list[dict] = []
        for c in crit:
            if not isinstance(c, dict):
                continue
            species = str(c.get("species") or c.get("tax_nom_val") or "").strip()
            if not species:
                continue
            try:
                dist_m = float(c.get("dist_m") or 0)
            except (TypeError, ValueError):
                dist_m = 0.0
            out.append({"species": species, "dist_m": dist_m})
        return out

    @staticmethod
    def _species_points_from_faune(j: dict) -> tuple[int, str, dict]:
        if j.get("intersects_any") is True:
            return 3, "intersection", {}

        dist_raw = j.get("nearest_observation_distance_m")
        buf_raw = j.get("buffer_radius_max_m")

        if buf_raw is None:
            return 0, "no_buffer_in_filter", {
                "nearest_observation_distance_m": dist_raw,
                "buffer_radius_max_m": None,
            }
        try:
            buffer_m = float(buf_raw)
        except (TypeError, ValueError):
            return 0, "no_buffer_in_filter", {
                "nearest_observation_distance_m": dist_raw,
                "buffer_radius_max_m": buf_raw,
            }
        if buffer_m <= 0:
            return 0, "no_buffer_in_filter", {
                "nearest_observation_distance_m": dist_raw,
                "buffer_radius_max_m": buffer_m,
            }

        if dist_raw is None:
            return 0, "no_observation", {
                "nearest_observation_distance_m": None,
                "buffer_radius_max_m": buffer_m,
                "buffer_half_m": buffer_m / 2.0,
            }
        try:
            dist_m = float(dist_raw)
        except (TypeError, ValueError):
            return 0, "no_observation", {
                "nearest_observation_distance_m": dist_raw,
                "buffer_radius_max_m": buffer_m,
                "buffer_half_m": buffer_m / 2.0,
            }

        half = buffer_m / 2.0
        extra = {
            "nearest_observation_distance_m": round(dist_m, 2),
            "buffer_radius_max_m": buffer_m,
            "buffer_half_m": half,
        }
        if dist_m <= half:
            return 2, "within_half_buffer", extra
        if dist_m <= buffer_m:
            return 1, "within_buffer", extra
        return 0, "beyond_buffer", extra

    @staticmethod
    def _species_points_from_filter_enrich(
        fauna_distances: dict,
        criteria: list[dict],
    ) -> tuple[int, str, dict]:
        if not criteria:
            return 0, "no_faune_criteria", {}

        best_points = 0
        best_reason = "no_observation"
        best_extra: dict = {}
        best_species: str | None = None
        best_dist: float | None = None
        best_buffer: float | None = None

        for crit in criteria:
            species = crit["species"]
            try:
                buffer_m = float(crit.get("dist_m") or 0)
            except (TypeError, ValueError):
                buffer_m = 0.0

            raw = fauna_distances.get(species)
            if raw is None:
                continue
            try:
                dist_m = float(raw)
            except (TypeError, ValueError):
                continue
            if dist_m < 0:
                continue

            if dist_m <= 0:
                pts, reason = 3, "intersection"
            elif buffer_m <= 0:
                pts, reason = 0, "no_buffer_in_filter"
            elif dist_m <= buffer_m / 2.0:
                pts, reason = 2, "within_half_buffer"
            elif dist_m <= buffer_m:
                pts, reason = 1, "within_buffer"
            else:
                pts, reason = 0, "beyond_buffer"

            if pts > best_points or (pts == best_points and (best_dist is None or dist_m < best_dist)):
                best_points = pts
                best_reason = reason
                best_species = species
                best_dist = dist_m
                best_buffer = buffer_m if buffer_m > 0 else None
                best_extra = {
                    "nearest_observation_distance_m": round(dist_m, 2),
                    "nearest_species": species,
                    "buffer_radius_max_m": buffer_m if buffer_m > 0 else None,
                    "buffer_half_m": buffer_m / 2.0 if buffer_m > 0 else None,
                }

        if best_species is None:
            return 0, "no_observation", {}
        return best_points, best_reason, best_extra

    def compute_for_run(self, conn, project_id: str, run_id: str) -> dict[str, dict]:
        t0 = time.perf_counter()
        opts = self._filter_options(conn, project_id, run_id)
        fauna_criteria = self._fauna_criteria(opts)
        has_faune = len(fauna_criteria) > 0

        base_rows = conn.execute(
            text(
                """
                SELECT
                    p.idu,
                    pp.distance_km
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

        fauna_legacy_by_idu: dict[str, dict] = {}
        filter_enrich_by_idu: dict[str, dict] = {}

        if has_faune:
            metric_rows = conn.execute(
                text(
                    """
                    SELECT idu, metric_key, metric_value_jsonb
                    FROM ecocompensation_results.parcelles_pool_metrics
                    WHERE project_id = CAST(:project_id AS uuid)
                      AND run_id = CAST(:run_id AS uuid)
                      AND metric_key IN ('especes_faune', 'filter_enrich')
                    """
                ),
                {"project_id": project_id, "run_id": run_id},
            ).mappings().all()

            for r in metric_rows:
                idu = str(r["idu"])
                j = r.get("metric_value_jsonb")
                if not isinstance(j, dict):
                    continue
                key = str(r.get("metric_key") or "")
                if key == "especes_faune":
                    fauna_legacy_by_idu[idu] = j
                elif key == "filter_enrich":
                    filter_enrich_by_idu[idu] = j

            if not fauna_legacy_by_idu and not filter_enrich_by_idu:
                log.warning(
                    "score_eco: faune criteria set but no especes_faune nor filter_enrich metrics "
                    "(project_id=%s run_id=%s)",
                    project_id,
                    run_id,
                )

        payload: dict[str, dict] = {}
        for r in base_rows:
            idu = str(r["idu"])
            distance_km = float(r["distance_km"] or 0.0)

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

            if not has_faune:
                species_points = 0
                species_reason = "no_faune_criteria"
                species_extra: dict = {}
            elif idu in fauna_legacy_by_idu:
                species_points, species_reason, species_extra = self._species_points_from_faune(
                    fauna_legacy_by_idu[idu]
                )
            else:
                enrich = filter_enrich_by_idu.get(idu) or {}
                fd = enrich.get("fauna_distances") if isinstance(enrich.get("fauna_distances"), dict) else {}
                species_points, species_reason, species_extra = self._species_points_from_filter_enrich(
                    fd,
                    fauna_criteria,
                )

            total_score = int(species_points + distance_points)
            payload[idu] = {
                "total_score": total_score,
                "max_score": ECO_MAX,
                "breakdown": {
                    "especes": {
                        "points": species_points,
                        "reason": species_reason,
                        **species_extra,
                    },
                    "distance": {
                        "points": distance_points,
                        "distance_km": round(distance_km, 3),
                        "bucket": distance_bucket,
                    },
                },
            }

        log.info(
            "[score_eco] compute_for_run project_id=%s run_id=%s idu=%d duration_s=%.2f",
            project_id,
            run_id,
            len(payload),
            time.perf_counter() - t0,
        )
        return payload
