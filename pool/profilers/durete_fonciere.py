from __future__ import annotations

import logging
from collections import defaultdict

from sqlalchemy import text

from durete_fonciere.V3_POOL.durete_service import run_durete_for_siren
from .base import BasePoolProfiler

logger = logging.getLogger(__name__)


def _payload_non_eligible(reason: str) -> dict[str, object]:
    return {
        "eligible": False,
        "reason": reason,
        "siren": None,
        "denomination": None,
        "forme_juridique": None,
        "score_final": None,
        "niveau_durete": None,
        "explication": None,
        "detail_axes": None,
        "statut": "skipped",
    }


class DureteFonciereProfiler(BasePoolProfiler):
    """
    Calcule la métrique de dureté foncière pour les parcelles du pool
    appartenant à des personnes morales (détectées par le profiler PM).

    Stratégie :
      1) Lire la métrique `parcelles_personnes_morales` déjà calculée.
      2) Grouper les parcelles éligibles par SIREN.
      3) Appeler le pipeline dureté une fois par SIREN.
      4) Répliquer le résultat SIREN sur chaque IDU de ce SIREN.
    """

    metric_key = "durete_fonciere"

    def _all_idus(self, conn, project_id: str, run_id: str) -> list[str]:
        rows = conn.execute(
            text(
                """
                SELECT DISTINCT pp.idu
                FROM ecocompensation_results.parcelles_pool pp
                WHERE pp.project_id = CAST(:project_id AS uuid)
                  AND pp.run_id = CAST(:run_id AS uuid)
                ORDER BY pp.idu
                """
            ),
            {"project_id": project_id, "run_id": run_id},
        ).fetchall()
        return [str(r[0]) for r in rows if r[0]]

    def _pm_metric_by_idu(self, conn, project_id: str, run_id: str) -> dict[str, dict]:
        rows = conn.execute(
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
        out: dict[str, dict] = {}
        for r in rows:
            idu = str(r["idu"])
            val = r.get("metric_value_jsonb")
            out[idu] = val if isinstance(val, dict) else {}
        return out

    def compute_for_run(self, conn, project_id: str, run_id: str) -> dict[str, dict]:
        all_idus = self._all_idus(conn, project_id, run_id)
        if not all_idus:
            return {}

        pm_by_idu = self._pm_metric_by_idu(conn, project_id, run_id)
        payload: dict[str, dict] = {}

        siren_to_idus: dict[str, list[str]] = defaultdict(list)
        siren_to_meta: dict[str, dict[str, str]] = {}

        for idu in all_idus:
            pm = pm_by_idu.get(idu, {})
            is_pm = pm.get("intersects_pm_database") is True
            if not is_pm:
                payload[idu] = _payload_non_eligible("not_pm")
                continue

            siren = str(pm.get("siren") or "").strip()
            if len(siren) != 9 or not siren.isdigit():
                payload[idu] = _payload_non_eligible("missing_or_invalid_siren")
                continue

            siren_to_idus[siren].append(idu)
            siren_to_meta[siren] = {
                "denomination": str(pm.get("denomination") or "").strip(),
                "forme_juridique": str(pm.get("forme_juridique") or "").strip(),
            }

        # Pipeline dureté appelé une fois par SIREN (pas une fois par IDU)
        for siren, idus_for_siren in siren_to_idus.items():
            meta = siren_to_meta.get(siren, {})
            try:
                res = run_durete_for_siren(
                    siren=siren,
                    idus=idus_for_siren,
                    denomination=meta.get("denomination", ""),
                    forme_juridique=meta.get("forme_juridique", ""),
                )
            except Exception:
                logger.exception(
                    "Profiler dureté foncière : erreur appel pipeline (project_id=%s, run_id=%s, siren=%s)",
                    project_id,
                    run_id,
                    siren,
                )
                for idu in idus_for_siren:
                    payload[idu] = {
                        "eligible": True,
                        "reason": "pipeline_exception",
                        "siren": siren,
                        "denomination": meta.get("denomination"),
                        "forme_juridique": meta.get("forme_juridique"),
                        "score_final": None,
                        "niveau_durete": None,
                        "explication": None,
                        "detail_axes": None,
                        "statut": "erreur",
                    }
                continue

            # Même résultat SIREN répliqué sur toutes les parcelles de ce SIREN
            for idu in idus_for_siren:
                payload[idu] = {
                    "eligible": True,
                    "reason": "ok",
                    "siren": res.get("siren") or siren,
                    "denomination": res.get("denomination") or meta.get("denomination"),
                    "forme_juridique": meta.get("forme_juridique"),
                    "score_final": res.get("score_final"),
                    "niveau_durete": res.get("niveau_durete"),
                    "explication": res.get("explication"),
                    "detail_axes": res.get("detail_axes"),
                    "statut": res.get("statut", "ok"),
                    "avertissements": res.get("avertissements", []),
                }

        return payload

