"""Dureté foncière légère des unités foncières : un score par SIREN, pas par parcelle.

Même cache `v4-legere` que les îlots RPG. Pas de cadastre, RPG, zonage ni DVF.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid

from sqlalchemy import text

from api.recherche_de_foncier.enrichir.durete_fonciere.V3_POOL.durete_cache import (
    get_cached_siren,
    set_cached_siren,
)
from api.recherche_de_foncier.enrichir.durete_fonciere.durete_v4.scoring import niveau
from api.recherche_de_foncier.enrichir.durete_fonciere.durete_v4.service import run_durete_for_siren
from api.recherche_de_foncier.enrichir.pool.profilers.durete_fonciere import (
    _normalize_score_final,
    _normalize_siren,
)
from api.recherche_de_foncier.filtrage.layers.rpg.rpg_ilots.durete import _CACHE_VERSION_LEGERE

logger = logging.getLogger(__name__)

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _patch(job_id: str, **fields) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job:
            job.update(fields)


def get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def _payload(res: dict, siren: str, denomination: str, forme: str) -> dict:
    score = _normalize_score_final(res.get("score_final"))
    return {
        "eligible": score is not None,
        "reason": None if score is not None else (res.get("reason") or "no_score"),
        "score_final": score,
        "niveau_durete": niveau(int(round(score))) if score is not None else res.get("niveau_durete"),
        "explication": res.get("explication"),
        "detail_axes": res.get("detail_axes"),
        "siren": siren,
        "denomination": res.get("denomination") or denomination or None,
        "forme_juridique": res.get("forme_juridique") or forme or None,
        "intersects_arrachage_vigne": False,
        "mode": "durete_fonciere_legere",
        "from_cache": bool(res.get("_from_cache") or res.get("from_cache")),
    }


def _analyse(conn, siren: str, idu: str, denomination: str, forme: str) -> dict:
    cached = get_cached_siren(conn, siren, _CACHE_VERSION_LEGERE)
    try:
        conn.commit()
    except Exception:
        pass
    if cached is not None:
        return _payload(cached, siren, denomination, forme)
    res = run_durete_for_siren(
        siren=siren,
        idus=[idu] if idu else [],
        denomination=denomination,
        forme_juridique=forme,
        avec_recherche_web=True,
        avec_rpg=False,
        avec_gpu=False,
        legere=True,
    )
    if not isinstance(res, dict) or res.get("statut") == "erreur":
        return _payload(
            {"score_final": None, "reason": "pipeline_exception", "statut": "erreur"},
            siren, denomination, forme,
        )
    try:
        set_cached_siren(
            conn, siren, res,
            denomination=denomination,
            forme_juridique=forme,
            cache_version=_CACHE_VERSION_LEGERE,
        )
        conn.commit()
    except Exception:
        logger.warning("DURETE UF CACHE WRITE FAIL | siren=%s", siren, exc_info=True)
        try:
            conn.rollback()
        except Exception:
            pass
    res["from_cache"] = False
    return _payload(res, siren, denomination, forme)


def compute(conn, project_id: str, uf_ids: list[str] | None, on_progress=None) -> dict:
    row = conn.execute(
        text("SELECT last_results_uf FROM ecocompensation.projects WHERE id = CAST(:pid AS uuid)"),
        {"pid": project_id},
    ).scalar()
    if isinstance(row, str):
        row = json.loads(row)
    ufs = (row or {}).get("unites_foncieres") if isinstance(row, dict) else None
    if not ufs:
        return {"phase": "done", "done_ufs": 0, "total_ufs": 0}
    keep = {str(x) for x in uf_ids} if uf_ids else None
    targets = [
        u for u in ufs
        if u.get("source") != "manuel" and _normalize_siren(u.get("siren"))
        and (keep is None or str(u.get("uf_id")) in keep)
    ]
    by_siren: dict[str, dict] = {}
    for u in targets:
        siren = _normalize_siren(u.get("siren"))
        if siren and siren not in by_siren:
            parcelles = u.get("parcelles") or []
            idu = str(parcelles[0].get("idu")) if parcelles and parcelles[0].get("idu") else ""
            by_siren[siren] = {
                "idu": idu,
                "denomination": str(u.get("denomination") or ""),
                "forme": str(u.get("forme_juridique") or ""),
            }
    total = len(by_siren)
    done = 0
    scores: dict[str, dict] = {}
    if on_progress:
        on_progress({"phase": "siren", "done_ufs": 0, "total_ufs": len(targets), "done_sirens": 0, "total_sirens": total})
    for siren, meta in by_siren.items():
        try:
            scores[siren] = _analyse(conn, siren, meta["idu"], meta["denomination"], meta["forme"])
        except Exception:
            logger.exception("DURETE UF | siren=%s", siren)
            scores[siren] = _payload({"score_final": None, "reason": "pipeline_exception"}, siren, meta["denomination"], meta["forme"])
        done += 1
        if on_progress:
            on_progress({"phase": "siren", "done_sirens": done, "total_sirens": total, "done_ufs": 0, "total_ufs": len(targets)})
    for u in targets:
        siren = _normalize_siren(u.get("siren"))
        if siren and siren in scores:
            u["durete_fonciere"] = scores[siren]
    conn.execute(
        text("""
            UPDATE ecocompensation.projects
            SET last_results_uf = CAST(:payload AS jsonb), updated_at = now()
            WHERE id = CAST(:pid AS uuid)
        """),
        {"pid": project_id, "payload": json.dumps(row, default=str)},
    )
    conn.commit()
    stats = {"phase": "done", "done_ufs": len(targets), "total_ufs": len(targets), "done_sirens": total, "total_sirens": total}
    if on_progress:
        on_progress(stats)
    return stats


def start_job(engine, project_id: str, uf_ids: list[str] | None) -> str:
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "running",
            "project_id": project_id,
            "phase": "queued",
            "done_ufs": 0,
            "total_ufs": len(uf_ids) if uf_ids else None,
        }

    def _run() -> None:
        try:
            with engine.connect() as conn:
                stats = compute(conn, project_id, uf_ids, on_progress=lambda info: _patch(job_id, **info))
            _patch(job_id, status="ok", **stats)
        except Exception as exc:
            logger.exception("DURETE UF JOB FAIL | project=%s", project_id)
            _patch(job_id, status="error", error=str(exc) or type(exc).__name__)

    threading.Timer(0.25, _run).start()
    return job_id
