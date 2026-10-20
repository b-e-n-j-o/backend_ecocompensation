"""
Enrichit chaque parcelle du pool avec les autorisations Sitadel.

Distinct de la dureté foncière : pas d'appel API externe, pas de LLM,
pas de modification de ``score_final``. La métrique ``autorisations_urba``
est écrite à côté des autres, et un endpoint dédié sert la même recherche
sans lancer l'analyse foncière.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from api.recherche_de_foncier.enrichir.durete_fonciere.AUTORISATIONS_URBA_SITADEL.service import lookup_for_idus
from .base import BasePoolProfiler

logger = logging.getLogger(__name__)


class AutorisationsUrbaProfiler(BasePoolProfiler):
    metric_key = "autorisations_urba"

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

    def compute_for_run(
        self,
        conn,
        project_id: str,
        run_id: str,
        *,
        only_idus: set[str] | None = None,
        avec_voisins: bool = True,
    ) -> dict[str, dict]:
        idus = self._all_idus(conn, project_id, run_id)
        if only_idus is not None:
            idus = [idu for idu in idus if idu in only_idus]
        if not idus:
            return {}
        logger.info(
            "AUTORISATIONS URBA RUN | project_id=%s run_id=%s idus=%d avec_voisins=%s",
            project_id,
            run_id,
            len(idus),
            avec_voisins,
        )
        return lookup_for_idus(conn, idus, avec_voisins=avec_voisins)
