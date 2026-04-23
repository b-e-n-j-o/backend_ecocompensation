from __future__ import annotations

import logging
import math
import os
from collections import defaultdict

from sqlalchemy import text

from durete_fonciere.V3_POOL.durete_cache import get_cached_siren, set_cached_siren
from durete_fonciere.V3_POOL.durete_service import run_durete_for_siren
from .base import BasePoolProfiler

logger = logging.getLogger(__name__)


def _durete_progress_enabled() -> bool:
    raw = str(os.getenv("POOL_DURETE_PROGRESS", "1")).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _durete_verbose_pipeline_enabled() -> bool:
    raw = str(os.getenv("POOL_DURETE_VERBOSE", "")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _durete_mode() -> str:
    raw = str(os.getenv("POOL_DURETE_MODE", "per_siren")).strip().lower()
    if raw == "per_idu":
        return "per_idu"
    return "per_siren"


def _normalize_score_final(raw: object) -> float | None:
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return None
    x = float(raw)
    if not math.isfinite(x) or x < 0.0 or x > 100.0:
        return None
    return x


def _normalize_score_with_vigne_bonus(score: float | None, intersects_vigne: bool) -> float | None:
    """Applique le surcharge +15pts si intersection arrachage de vigne, cap à 100."""
    if score is None:
        return None
    if intersects_vigne:
        return min(100.0, score + 15.0)
    return score


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
        "intersects_arrachage_vigne": False,
    }


class DureteFonciereProfiler(BasePoolProfiler):
    """
    Calcule la métrique de dureté foncière pour les parcelles du pool
    appartenant à des personnes morales (détectées par le profiler PM).

    Stratégie :
      1) Lire la métrique `parcelles_personnes_morales` déjà calculée.
      2) Checker l'intersection de chaque IDU éligible avec la couche arrachage_vignes.
      3) Grouper les parcelles éligibles par SIREN (ou SIREN::IDU en mode per_idu).
      4) Appeler le pipeline dureté une fois par groupe.
      5) Appliquer le surcharge +15pts si intersection vigne, puis répliquer.

    Note : deux parcelles d'un même SIREN peuvent avoir des scores finaux différents
    si l'une intersecte la couche arrachage_vignes et pas l'autre.
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

    def _vigne_intersection_by_idu(
        self, conn, project_id: str, run_id: str, idus: list[str]
    ) -> dict[str, bool]:
        if not idus:
            return {}

        rows = conn.execute(
            text(
                """
                SELECT
                    p.idu,
                    EXISTS (
                        SELECT 1
                        FROM ecocompensation.arrachage_vignes av
                        WHERE ST_Intersects(p.geom_2154, av.geom_2154)
                    ) AS intersects_vigne
                FROM ecocompensation_results.parcelles_pool pp
                JOIN ecocompensation_results.parcelles p
                ON p.project_id = pp.project_id
                AND p.idu = pp.idu
                WHERE pp.project_id = CAST(:project_id AS uuid)
                AND pp.run_id = CAST(:run_id AS uuid)
                AND pp.idu = ANY(:idus)
                AND p.geom_2154 IS NOT NULL
                """
            ),
            {"project_id": project_id, "run_id": run_id, "idus": idus},
        ).fetchall()

        return {str(r[0]): bool(r[1]) for r in rows}

    def compute_for_run(self, conn, project_id: str, run_id: str) -> dict[str, dict]:
        progress_logs = _durete_progress_enabled()
        verbose_pipeline = _durete_verbose_pipeline_enabled()
        mode = _durete_mode()

        all_idus = self._all_idus(conn, project_id, run_id)
        if not all_idus:
            return {}

        skipped_not_pm = 0
        skipped_invalid_siren = 0
        eligible_idus = 0
        siren_groups_errors = 0
        siren_groups_with_warnings = 0

        if progress_logs:
            logger.info(
                "DURETE RUN START | project_id=%s run_id=%s idus_pool=%d mode=%s",
                project_id,
                run_id,
                len(all_idus),
                mode,
            )

        pm_by_idu = self._pm_metric_by_idu(conn, project_id, run_id)

        # ── Intersection arrachage vignes (batch, 1 seul appel SQL) ──────────
        vigne_by_idu = self._vigne_intersection_by_idu(conn, project_id, run_id, all_idus)
        vigne_hits = sum(1 for v in vigne_by_idu.values() if v)
        if progress_logs:
            logger.info(
                "DURETE VIGNE CHECK | idus_checked=%d idus_intersecting_vigne=%d",
                len(vigne_by_idu),
                vigne_hits,
            )

        payload: dict[str, dict] = {}
        group_to_idus: dict[str, list[str]] = defaultdict(list)
        group_to_meta: dict[str, dict[str, str]] = {}

        for idu in all_idus:
            pm = pm_by_idu.get(idu, {})
            is_pm = pm.get("intersects_pm_database") is True
            if not is_pm:
                payload[idu] = _payload_non_eligible("not_pm")
                skipped_not_pm += 1
                if progress_logs:
                    logger.info("DURETE IDU | idu=%s status=skipped reason=not_pm", idu)
                continue

            siren = str(pm.get("siren") or "").strip()
            if len(siren) != 9 or not siren.isdigit():
                payload[idu] = _payload_non_eligible("missing_or_invalid_siren")
                skipped_invalid_siren += 1
                if progress_logs:
                    logger.info(
                        "DURETE IDU | idu=%s status=skipped reason=missing_or_invalid_siren", idu
                    )
                continue

            if mode == "per_idu":
                group_key = f"{siren}::{idu}"
            else:
                group_key = siren
            group_to_idus[group_key].append(idu)
            eligible_idus += 1
            group_to_meta[group_key] = {
                "siren": siren,
                "denomination": str(pm.get("denomination") or "").strip(),
                "forme_juridique": str(pm.get("forme_juridique") or "").strip(),
            }
            if progress_logs:
                logger.info(
                    "DURETE IDU | idu=%s status=eligible siren=%s intersects_vigne=%s",
                    idu,
                    siren,
                    vigne_by_idu.get(idu, False),
                )

        if progress_logs:
            logger.info(
                "DURETE RUN PLAN | idus_pool=%d eligible_idus=%d skipped_not_pm=%d "
                "skipped_invalid_siren=%d groups=%d",
                len(all_idus),
                eligible_idus,
                skipped_not_pm,
                skipped_invalid_siren,
                len(group_to_idus),
            )

        total_groups = len(group_to_idus)
        for group_idx, (group_key, idus_for_siren) in enumerate(group_to_idus.items(), 1):
            meta = group_to_meta.get(group_key, {})
            siren = str(meta.get("siren") or "").strip()
            if progress_logs:
                logger.info(
                    "DURETE GROUP [%d/%d] START | mode=%s group=%s siren=%s idus_count=%d idus=%s",
                    group_idx,
                    total_groups,
                    mode,
                    group_key,
                    siren,
                    len(idus_for_siren),
                    ",".join(idus_for_siren),
                )

            # ── 1) Tentative cache SIREN ──────────────────────────────────────
            res = get_cached_siren(conn, siren)
            if res is not None:
                if progress_logs:
                    logger.info(
                        "DURETE GROUP [%d/%d] CACHE HIT | mode=%s group=%s siren=%s idus=%s",
                        group_idx,
                        total_groups,
                        mode,
                        group_key,
                        siren,
                        ",".join(idus_for_siren),
                    )
            else:
                # ── 2) Appel pipeline LLM ────────────────────────────────────
                try:
                    res = run_durete_for_siren(
                        siren=siren,
                        idus=idus_for_siren,
                        denomination=meta.get("denomination", ""),
                        forme_juridique=meta.get("forme_juridique", ""),
                        verbose=verbose_pipeline,
                    )
                except Exception:
                    logger.exception(
                        "DURETE GROUP [%d/%d] ERROR | project_id=%s run_id=%s siren=%s",
                        group_idx,
                        total_groups,
                        project_id,
                        run_id,
                        siren,
                    )
                    siren_groups_errors += 1
                    for idu in idus_for_siren:
                        payload[idu] = {
                            "eligible": True,
                            "reason": "pipeline_exception",
                            "siren": siren,
                            "denomination": meta.get("denomination"),
                            "forme_juridique": meta.get("forme_juridique"),
                            "score_final": None,
                            "score_llm_base": None,
                            "niveau_durete": None,
                            "explication": None,
                            "detail_axes": None,
                            "statut": "erreur",
                            "avertissements": [],
                            "intersects_arrachage_vigne": vigne_by_idu.get(idu, False),
                            "from_cache": False,
                        }
                        if progress_logs:
                            logger.info(
                                "DURETE IDU | idu=%s status=error reason=pipeline_exception siren=%s",
                                idu,
                                siren,
                            )
                    continue

                # ── 3) Cache write non bloquant ──────────────────────────────
                try:
                    set_cached_siren(
                        conn,
                        siren,
                        res,
                        denomination=meta.get("denomination", ""),
                        forme_juridique=meta.get("forme_juridique", ""),
                    )
                except Exception:
                    logger.warning(
                        "DURETE CACHE WRITE FAIL | siren=%s — résultat LLM utilisé quand même",
                        siren,
                        exc_info=True,
                    )

            sf_raw = _normalize_score_final(res.get("score_final"))
            warnings = res.get("avertissements", []) or []
            if warnings:
                siren_groups_with_warnings += 1
                logger.warning(
                    "DURETE GROUP [%d/%d] WARN | mode=%s group=%s siren=%s warnings=%s",
                    group_idx,
                    total_groups,
                    mode,
                    group_key,
                    siren,
                    warnings,
                )
            if progress_logs:
                logger.info(
                    "DURETE GROUP [%d/%d] DONE | mode=%s group=%s siren=%s statut=%s score_final_raw=%s",
                    group_idx,
                    total_groups,
                    mode,
                    group_key,
                    siren,
                    res.get("statut", "ok"),
                    sf_raw,
                )

            # ── Réplication sur chaque IDU du groupe, avec bonus vigne IDU-spécifique ──
            for idu in idus_for_siren:
                intersects_vigne = vigne_by_idu.get(idu, False)
                sf_final = _normalize_score_with_vigne_bonus(sf_raw, intersects_vigne)

                payload[idu] = {
                    "eligible": True,
                    "reason": "ok",
                    "siren": res.get("siren") or siren,
                    "denomination": res.get("denomination") or meta.get("denomination"),
                    "forme_juridique": meta.get("forme_juridique"),
                    "score_final": sf_final,
                    "score_llm_base": sf_raw,          # score avant bonus vigne, utile pour debug
                    "niveau_durete": res.get("niveau_durete"),
                    "explication": res.get("explication"),
                    "detail_axes": res.get("detail_axes"),
                    "statut": res.get("statut", "ok"),
                    "avertissements": res.get("avertissements", []),
                    "intersects_arrachage_vigne": intersects_vigne,
                    "from_cache": bool(res.get("_from_cache")),
                }
                if progress_logs:
                    logger.info(
                        "DURETE IDU | idu=%s status=done siren=%s statut=%s "
                        "score_llm_base=%s intersects_vigne=%s score_final=%s from_cache=%s",
                        idu,
                        res.get("siren") or siren,
                        res.get("statut", "ok"),
                        sf_raw,
                        intersects_vigne,
                        sf_final,
                        bool(res.get("_from_cache")),
                    )

        if progress_logs:
            logger.info(
                "DURETE RUN COMPLETE | project_id=%s run_id=%s idus_pool=%d eligible_idus=%d "
                "payload_idus=%d groups=%d groups_error=%d groups_with_warnings=%d mode=%s",
                project_id,
                run_id,
                len(all_idus),
                eligible_idus,
                len(payload),
                len(group_to_idus),
                siren_groups_errors,
                siren_groups_with_warnings,
                mode,
            )

        return payload