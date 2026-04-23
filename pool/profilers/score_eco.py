from __future__ import annotations

import logging
import os
import time
from sqlalchemy import text

from .base import BasePoolProfiler

log = logging.getLogger(__name__)

ECO_MAX = 6


class ScoreEcoProfiler(BasePoolProfiler):
    """
    Score écologique (0..6), uniquement à partir de :
    - la distance parcelle ↔ projet (``parcelles_pool.distance_km``) : même barème qu’avant (0..3) ;
    - la métrique ``especes_faune`` déjà calculée : intersection, puis distance à l’observation la plus proche
      par rapport au buffer du filtre (pas de rôle de l’adjacence entre parcelles).

    Barème espèces (0..3) :
    - 3 : intersection avec une observation ;
    - 2 : pas d’intersection et observation la plus proche à ≤ buffer_max_m / 2 ;
    - 1 : au-delà de la demi-buffer mais ≤ buffer_max_m ;
    - 0 : au-delà du buffer ou pas d’observation / pas de buffer dans le filtre.

    Nécessite ``EspecesProfiler`` en amont lorsque le filtre définit des critères faune ; sinon la partie espèces vaut 0.
    """

    metric_key = "score_eco"

    @staticmethod
    def _mem_logging_enabled() -> bool:
        raw = str(os.getenv("POOL_PROFILE_MEM", "")).strip().lower()
        return raw in {"1", "true", "yes", "on"}

    @staticmethod
    def _mem_mb() -> float | None:
        try:
            import psutil  # lazy import pour ne rien imposer si non utilisé

            return round(psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024), 2)
        except Exception:
            return None

    def _mem_checkpoint(self, label: str) -> None:
        if not self._mem_logging_enabled():
            return
        mb = self._mem_mb()
        if mb is None:
            log.info("[score_eco] %s | RAM: n/a", label)
            return
        log.info("[score_eco] %s | RAM RSS: %.2f MB", label, mb)

    def _step_log(self, step: str, start_ts: float, extra: str = "") -> None:
        elapsed = time.perf_counter() - start_ts
        suffix = f" | {extra}" if extra else ""
        log.info("[score_eco] STEP %s | duration_s=%.3f%s", step, elapsed, suffix)

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
    def _has_faune_criteria(opts: dict) -> bool:
        crit = opts.get("faune_criteria")
        if not isinstance(crit, list):
            return False
        for c in crit:
            if not isinstance(c, dict):
                continue
            if str(c.get("tax_nom_val", "") or "").strip():
                return True
        return False

    @staticmethod
    def _species_points_from_faune(j: dict) -> tuple[int, str, dict]:
        """
        Retourne (points, reason, extra) à partir du JSON ``especes_faune`` uniquement
        (aucune requête spatiale ici).
        """
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

    def compute_for_run(self, conn, project_id: str, run_id: str) -> dict[str, dict]:
        t0 = time.perf_counter()
        self._mem_checkpoint("start")
        opts = self._filter_options(conn, project_id, run_id)
        has_faune = self._has_faune_criteria(opts)

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
        self._mem_checkpoint(f"after base_rows ({len(base_rows)} rows)")

        fauna_by_idu: dict[str, dict] = {}
        if has_faune:
            ts_species = time.perf_counter()
            species_metric_rows = conn.execute(
                text(
                    """
                    SELECT idu, metric_value_jsonb
                    FROM ecocompensation_results.parcelles_pool_metrics
                    WHERE project_id = CAST(:project_id AS uuid)
                      AND run_id = CAST(:run_id AS uuid)
                      AND metric_key = 'especes_faune'
                    """
                ),
                {"project_id": project_id, "run_id": run_id},
            ).mappings().all()
            for r in species_metric_rows:
                idu = str(r["idu"])
                j = r.get("metric_value_jsonb")
                if isinstance(j, dict):
                    fauna_by_idu[idu] = j
            self._step_log(
                "species_metric_read",
                ts_species,
                f"rows={len(species_metric_rows)} idu_with_payload={len(fauna_by_idu)}",
            )
            if not species_metric_rows:
                raise RuntimeError(
                    "score_eco requires existing 'especes_faune' metrics when faune criteria are set. "
                    "Run EspecesProfiler before ScoreEcoProfiler."
                )
            self._mem_checkpoint(f"after fauna metrics ({len(fauna_by_idu)} idu)")

        payload: dict[str, dict] = {}
        ts_payload = time.perf_counter()
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
            else:
                j = fauna_by_idu.get(idu) or {}
                species_points, species_reason, species_extra = self._species_points_from_faune(j)

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
        self._step_log("payload_build", ts_payload, f"idu={len(payload)}")
        self._mem_checkpoint(f"end payload ({len(payload)} idu)")
        self._step_log("compute_total", t0, f"idu={len(payload)}")
        return payload
