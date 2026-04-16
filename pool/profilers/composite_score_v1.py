from __future__ import annotations

from sqlalchemy import text

from .base import BasePoolProfiler


class CompositeScoreV1Profiler(BasePoolProfiler):
    """
    Score composite parcelle (0..100), à partir de :
      - score écologique v1 (0..9) normalisé sur 100
      - score foncier de dureté (0..100) transformé en attractivité (100 - dureté)

    Formule :
      composite = 0.6 * eco_norm + 0.4 * attractivite_fonciere

    Garde-fou :
      - attractivite_fonciere < 20 => foncier_redhibitoire = True
    """

    metric_key = "composite_score_v1"
    ECO_MAX = 9.0
    W_ECO = 0.6
    W_FONCIER = 0.4
    REDHIBITOIRE_ATTRACTIVITE_THRESHOLD = 20.0

    def _all_idus(self, conn, project_id: str, run_id: str) -> list[str]:
        rows = conn.execute(
            text(
                """
                SELECT DISTINCT pp.idu
                FROM ecocompensation_results.parcelles_pool pp
                WHERE pp.project_id = CAST(:project_id AS uuid)
                  AND pp.run_id = CAST(:run_id AS uuid)
                """
            ),
            {"project_id": project_id, "run_id": run_id},
        ).fetchall()
        return [str(r[0]) for r in rows if r[0]]

    def _metric_rows(self, conn, project_id: str, run_id: str) -> list[dict]:
        return conn.execute(
            text(
                """
                SELECT idu, metric_key, metric_value_jsonb
                FROM ecocompensation_results.parcelles_pool_metrics
                WHERE project_id = CAST(:project_id AS uuid)
                  AND run_id = CAST(:run_id AS uuid)
                  AND metric_key IN ('parcel_score_v1', 'durete_fonciere')
                """
            ),
            {"project_id": project_id, "run_id": run_id},
        ).mappings().all()

    def compute_for_run(self, conn, project_id: str, run_id: str) -> dict[str, dict]:
        all_idus = self._all_idus(conn, project_id, run_id)
        if not all_idus:
            return {}

        eco_by_idu: dict[str, float] = {}
        durete_by_idu: dict[str, float] = {}

        for r in self._metric_rows(conn, project_id, run_id):
            idu = str(r["idu"])
            key = str(r["metric_key"])
            val = r.get("metric_value_jsonb")
            if not isinstance(val, dict):
                continue
            if key == "parcel_score_v1":
                raw = val.get("total_score")
                if isinstance(raw, (int, float)):
                    eco_by_idu[idu] = float(raw)
            elif key == "durete_fonciere":
                raw = val.get("score_final")
                if isinstance(raw, (int, float)):
                    durete_by_idu[idu] = float(raw)

        payload: dict[str, dict] = {}
        for idu in all_idus:
            eco_raw = eco_by_idu.get(idu)
            durete_raw = durete_by_idu.get(idu)

            eco_norm = None
            if eco_raw is not None:
                eco_norm = max(0.0, min(100.0, (eco_raw / self.ECO_MAX) * 100.0))

            attractivite = None
            if durete_raw is not None:
                attractivite = max(0.0, min(100.0, 100.0 - durete_raw))

            foncier_redhibitoire = (
                attractivite is not None
                and attractivite < self.REDHIBITOIRE_ATTRACTIVITE_THRESHOLD
            )

            score_composite = None
            if eco_norm is not None and attractivite is not None:
                score_composite = (
                    self.W_ECO * eco_norm + self.W_FONCIER * attractivite
                )

            payload[idu] = {
                "score_composite": None if score_composite is None else round(score_composite, 2),
                "eco_score_raw": None if eco_raw is None else round(eco_raw, 3),
                "eco_score_max": self.ECO_MAX,
                "eco_score_norm": None if eco_norm is None else round(eco_norm, 2),
                "durete_fonciere": None if durete_raw is None else round(durete_raw, 2),
                "attractivite_fonciere": None if attractivite is None else round(attractivite, 2),
                "ponderation": {
                    "eco": self.W_ECO,
                    "foncier": self.W_FONCIER,
                },
                "foncier_redhibitoire": bool(foncier_redhibitoire),
                "redhibitoire_threshold": self.REDHIBITOIRE_ATTRACTIVITE_THRESHOLD,
            }

        return payload

