from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid

from fastapi import APIRouter, Body, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from db import get_engine
from api.recherche_de_foncier.enrichir.pool import add_parcelles, pool_service, profiling_service
from api.recherche_de_foncier.enrichir.pool.map_overlay import build_pool_map_overlay


router = APIRouter(prefix="/api/projects", tags=["pool"])
all_runs_router = APIRouter(prefix="/api/pool", tags=["pool"])
engine = get_engine()
logger = logging.getLogger(__name__)

# Jobs dureté en mémoire (évite le timeout HTTP Render ~100s / 502 sans CORS).
_durete_jobs: dict[str, dict] = {}
_durete_jobs_lock = threading.Lock()
_DURETE_JOBS_MAX = 50


def _project_exists(project_id: str) -> bool:
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT 1 FROM ecocompensation.projects WHERE id = :pid LIMIT 1"),
            {"pid": project_id},
        ).first()
    return row is not None


def _project_aoi_id(project_id: str) -> str:
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT aoi_id FROM ecocompensation.projects WHERE id = CAST(:pid AS uuid) LIMIT 1"),
            {"pid": project_id},
        ).mappings().first()
    if not row or row.get("aoi_id") is None:
        return ""
    return str(row["aoi_id"])


@router.get("/{project_id}/pool/runs")
def list_pool_runs(project_id: str, limit: int = Query(50, ge=1, le=200)):
    if not _project_exists(project_id):
        raise HTTPException(404, f"Projet {project_id} introuvable")
    with engine.begin() as conn:
        pool_service.ensure_tables(conn)
        runs = pool_service.list_runs(conn, project_id=project_id, limit=limit)
    return {"runs": runs}


@all_runs_router.get("/runs")
def list_all_pool_runs(
    limit: int = Query(200, ge=1, le=500),
    scope: str = Query("parcelles"),
):
    """Liste globale des pools (tous projets) — évite N GET /projects/{id}/pool/runs."""
    with engine.begin() as conn:
        pool_service.ensure_tables(conn)
        runs = pool_service.list_all_runs(conn, scope=scope or None, limit=limit)
    return {"runs": runs}


@router.get("/{project_id}/pool/runs/{run_id}/snapshot")
def get_pool_run_snapshot(project_id: str, run_id: str):
    """
    Reconstitue les résultats parcelles d’un run historique (liste + options filtre + entonnoir
    si `result_summary` a été rempli lors du POST /filter).
    """
    if not _project_exists(project_id):
        raise HTTPException(404, f"Projet {project_id} introuvable")
    try:
        uuid.UUID(str(run_id))
    except ValueError:
        raise HTTPException(400, f"run_id invalide (UUID attendu): {run_id}")
    aoi_id = _project_aoi_id(project_id)
    with engine.begin() as conn:
        pool_service.ensure_tables(conn)
    with engine.begin() as conn:
        snap = pool_service.build_filter_snapshot_from_run(conn, project_id, run_id, aoi_id)
    if not snap:
        raise HTTPException(404, f"Run {run_id} introuvable ou non applicable (scope parcelles)")
    return snap


@router.get("/{project_id}/pool/runs/{run_id}/map-overlay")
def get_pool_map_overlay(project_id: str, run_id: str):
    """Parcelles du pool en GeoJSON WGS84 + attributs enrichis (carte Données internes)."""
    if not _project_exists(project_id):
        raise HTTPException(404, f"Projet {project_id} introuvable")
    try:
        uuid.UUID(str(run_id))
    except ValueError:
        raise HTTPException(400, f"run_id invalide (UUID attendu): {run_id}")
    with engine.begin() as conn:
        payload = build_pool_map_overlay(conn, project_id, run_id)
    if not payload.get("ok"):
        raise HTTPException(404, f"Run {run_id} introuvable pour ce projet")
    return payload


@router.get("/{project_id}/pool")
def get_pool(project_id: str, run_id: str):
    if not _project_exists(project_id):
        raise HTTPException(404, f"Projet {project_id} introuvable")
    with engine.begin() as conn:
        pool_service.ensure_tables(conn)
        rows = pool_service.get_pool(conn, project_id=project_id, run_id=run_id)
    return {"run_id": run_id, "parcelles": rows, "total": len(rows)}


@router.get("/{project_id}/pool/metrics")
def get_pool_metrics_bulk(project_id: str, run_id: str = Query(..., description="UUID du run parcelles_pool_runs")):
    """Toutes les métriques du run, groupées par IDU (préchargement après filtrage)."""
    if not _project_exists(project_id):
        raise HTTPException(404, f"Projet {project_id} introuvable")
    with engine.begin() as conn:
        pool_service.ensure_tables(conn)
        by_idu = pool_service.get_all_metrics_grouped_by_idu(conn, project_id=project_id, run_id=run_id)
    return {"run_id": run_id, "by_idu": by_idu, "total_parcelles": len(by_idu)}


class IndesirablesAddBody(BaseModel):
    run_id: str = Field(..., min_length=1)
    idus: list[str] = Field(default_factory=list, max_length=500)


class DureteRecomputeBody(BaseModel):
    """Si `idus` est omis ou vide : tout le pool actif. Sinon : seulement ces parcelles."""
    idus: list[str] | None = Field(default=None, max_length=2000)


class UrbaRecomputeBody(BaseModel):
    """Si `idus` est omis ou vide : tout le pool actif. Sinon : seulement ces parcelles."""
    idus: list[str] | None = Field(default=None, max_length=2000)


class AutorisationsUrbaRecomputeBody(BaseModel):
    """Si `idus` est omis ou vide : tout le pool actif. Sinon : seulement ces parcelles."""
    idus: list[str] | None = Field(default=None, max_length=2000)
    avec_voisins: bool = Field(
        default=True,
        description="Inclure les autorisations des parcelles qui touchent la cible.",
    )


class AddParcellesBody(BaseModel):
    """IDU à injecter dans le pool sans rejouer le filtrage (1 à 50)."""
    idus: list[str] = Field(..., min_length=1, max_length=50)


@router.get("/{project_id}/pool/indesirables")
def list_pool_indesirables(project_id: str):
    """Parcelles indésirables persistées au niveau projet (réutilisées par les futurs filtres)."""
    if not _project_exists(project_id):
        raise HTTPException(404, f"Projet {project_id} introuvable")
    with engine.begin() as conn:
        pool_service.ensure_tables(conn)
        payload = pool_service.get_project_indesirables_payload(conn, project_id=project_id)
    return {"project_id": project_id, **payload}


@router.get("/{project_id}/pool/indesirables-count")
def count_pool_indesirables(project_id: str):
    if not _project_exists(project_id):
        raise HTTPException(404, f"Projet {project_id} introuvable")
    with engine.begin() as conn:
        pool_service.ensure_tables(conn)
        total = pool_service.count_project_indesirables(conn, project_id=project_id)
    return {"project_id": project_id, "total": total}


@router.post("/{project_id}/pool/indesirables")
def add_pool_indesirables(project_id: str, body: IndesirablesAddBody):
    """Ajoute des parcelles au pool indésirable projet (source run requis pour capturer les détails)."""
    if not _project_exists(project_id):
        raise HTTPException(404, f"Projet {project_id} introuvable")
    with engine.begin() as conn:
        pool_service.ensure_tables(conn)
        if not pool_service.run_belongs_to_project(conn, project_id, body.run_id):
            raise HTTPException(404, f"Run {body.run_id} introuvable pour ce projet")
        n = pool_service.add_project_indesirables_from_run(conn, project_id, body.run_id, body.idus)
    return {"status": "ok", "project_id": project_id, "run_id": body.run_id, "inserted": n}


@router.delete("/{project_id}/pool/indesirables/{idu}")
def remove_pool_indesirable(
    project_id: str,
    idu: str,
    run_id: str | None = Query(None, description="Paramètre conservé pour rétrocompatibilité"),
):
    """Retire une parcelle de la liste indésirable projet (ex. réintégration au classement)."""
    if not _project_exists(project_id):
        raise HTTPException(404, f"Projet {project_id} introuvable")
    with engine.begin() as conn:
        pool_service.ensure_tables(conn)
        ok = pool_service.remove_project_indesirable(conn, project_id, idu)
    if not ok:
        raise HTTPException(404, "Parcelle indésirable introuvable pour ce projet")
    return {"status": "ok", "project_id": project_id, "run_id": run_id, "idu": idu}


@router.get("/{project_id}/pool/{idu}/metrics")
def get_pool_metrics(project_id: str, idu: str, run_id: str):
    if not _project_exists(project_id):
        raise HTTPException(404, f"Projet {project_id} introuvable")
    with engine.begin() as conn:
        pool_service.ensure_tables(conn)
        rows = pool_service.get_metrics(conn, project_id=project_id, run_id=run_id, idu=idu)
    return {"run_id": run_id, "idu": idu, "metrics": rows}


@router.post("/{project_id}/pool/runs/{run_id}/recompute-metrics")
def recompute_metrics(project_id: str, run_id: str, score_only: bool = Query(False)):
    if not _project_exists(project_id):
        raise HTTPException(404, f"Projet {project_id} introuvable")
    with engine.begin() as conn:
        pool_service.ensure_tables(conn)
        if score_only:
            updated = profiling_service.compute_parcel_score_for_run(
                conn,
                project_id=project_id,
                run_id=run_id,
            )
            return {
                "status": "ok",
                "project_id": project_id,
                "run_id": run_id,
                "metric_key": "score_eco",
                "updated_count": updated,
                "mode": "score_only",
            }
        profiling_service.compute_metrics_for_run(conn, project_id=project_id, run_id=run_id)
    return {"status": "ok", "project_id": project_id, "run_id": run_id, "mode": "all_metrics"}


def _prune_durete_jobs() -> None:
    if len(_durete_jobs) <= _DURETE_JOBS_MAX:
        return
    done = [
        k
        for k, v in _durete_jobs.items()
        if v.get("status") in {"ok", "error"}
    ]
    for k in done[: max(0, len(_durete_jobs) - _DURETE_JOBS_MAX)]:
        _durete_jobs.pop(k, None)


def _patch_durete_job(job_id: str, **fields) -> None:
    with _durete_jobs_lock:
        current = _durete_jobs.get(job_id)
        if not current:
            return
        current.update(fields)


def _run_durete_job(
    job_id: str,
    project_id: str,
    run_id: str,
    only_idus: set[str] | None,
) -> None:
    def on_progress(info: dict) -> None:
        _patch_durete_job(job_id, **info)

    try:
        # connect() + commits internes : pas de transaction ouverte pendant le LLM.
        with engine.connect() as conn:
            pool_service.ensure_tables(conn)
            stats = profiling_service.compute_durete_for_run(
                conn,
                project_id=project_id,
                run_id=run_id,
                exclude_indesirables=True,
                only_idus=only_idus,
                on_progress=on_progress,
            )
        _patch_durete_job(
            job_id,
            status="ok",
            metric_key="durete_fonciere",
            **stats,
        )
        with _durete_jobs_lock:
            _prune_durete_jobs()
    except Exception as exc:
        logger.exception("DURETE JOB FAIL | job_id=%s project_id=%s run_id=%s", job_id, project_id, run_id)
        _patch_durete_job(
            job_id,
            status="error",
            metric_key="durete_fonciere",
            error=str(exc) or type(exc).__name__,
        )


@router.post("/{project_id}/pool/runs/{run_id}/recompute-durete", status_code=status.HTTP_202_ACCEPTED)
async def recompute_durete(
    project_id: str,
    run_id: str,
    body: DureteRecomputeBody | None = Body(default=None),
):
    """
    Lance le calcul de dureté foncière en arrière-plan (LLM long).

    Réponse 202 + job_id : poller GET .../recompute-durete/jobs/{job_id}.
    Évite le 502 Render (timeout proxy ~100s, headers CORS absents → « Failed to fetch »).
    """
    def _prepare() -> None:
        if not _project_exists(project_id):
            raise HTTPException(404, f"Projet {project_id} introuvable")
        try:
            uuid.UUID(str(run_id))
        except ValueError:
            raise HTTPException(400, f"run_id invalide (UUID attendu): {run_id}")
        with engine.begin() as conn:
            pool_service.ensure_tables(conn)
            if not pool_service.run_belongs_to_project(conn, project_id, run_id):
                raise HTTPException(404, f"Run {run_id} introuvable pour ce projet")

    await asyncio.to_thread(_prepare)
    only_idus: set[str] | None = None
    if body and body.idus:
        only_idus = {str(x).strip() for x in body.idus if str(x).strip()}
        if not only_idus:
            only_idus = None
    job_id = str(uuid.uuid4())
    with _durete_jobs_lock:
        _durete_jobs[job_id] = {
            "status": "running",
            "project_id": project_id,
            "run_id": run_id,
            "metric_key": "durete_fonciere",
            "phase": "queued",
            "done_idus": 0,
            "total_idus": len(only_idus) if only_idus is not None else None,
            "remaining_idus": len(only_idus) if only_idus is not None else None,
        }
    # Démarrer après l'envoi du 202 : un thread immédiat peut tuer le worker
    # (OOM dureté) avant que Render n'ait renvoyé la réponse → 502 sans CORS.
    threading.Timer(
        0.25,
        _run_durete_job,
        args=(job_id, project_id, run_id, only_idus),
    ).start()
    return {
        "status": "accepted",
        "job_id": job_id,
        "project_id": project_id,
        "run_id": run_id,
        "metric_key": "durete_fonciere",
    }


@router.get("/{project_id}/pool/runs/{run_id}/recompute-durete/jobs/{job_id}")
def recompute_durete_job(project_id: str, run_id: str, job_id: str):
    with _durete_jobs_lock:
        job = _durete_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job dureté introuvable (expiré ou autre instance)")
    if job.get("project_id") != project_id or job.get("run_id") != run_id:
        raise HTTPException(404, "Job dureté introuvable pour ce run")
    return job


@router.post("/{project_id}/pool/runs/{run_id}/recompute-urbanisme")
def recompute_urbanisme(
    project_id: str,
    run_id: str,
    body: UrbaRecomputeBody | None = Body(default=None),
):
    """
    Croise les parcelles du run (ou une sélection) avec les flux WFS du GPU.

    Écrit la métrique `urbanisme_gpu`. Lancement manuel : les requêtes WFS
    ne partent pas pendant le filtrage.
    """
    from api.recherche_de_foncier.enrichir.pool.profilers.urba_profiler_avec_scoring.urba_profiler.schemas import (
        ParcelleIn,
        ProfilOptions,
        UrbaProfilRequest,
    )
    from api.recherche_de_foncier.enrichir.pool.profilers.urba_profiler_avec_scoring.urba_profiler.service import (
        run_urba_profile,
    )

    if not _project_exists(project_id):
        raise HTTPException(404, f"Projet {project_id} introuvable")
    try:
        uuid.UUID(str(run_id))
    except ValueError:
        raise HTTPException(400, f"run_id invalide (UUID attendu): {run_id}")
    only_idus: set[str] | None = None
    if body and body.idus:
        only_idus = {str(x).strip() for x in body.idus if str(x).strip()} or None
    t0 = time.perf_counter()
    with engine.begin() as conn:
        pool_service.ensure_tables(conn)
        if not pool_service.run_belongs_to_project(conn, project_id, run_id):
            raise HTTPException(404, f"Run {run_id} introuvable pour ce projet")
        loaded = profiling_service.load_urba_targets(
            conn,
            project_id,
            run_id,
            exclude_indesirables=True,
            only_idus=only_idus,
        )
    if not loaded["parcelles"]:
        return {
            "status": "ok",
            "project_id": project_id,
            "run_id": run_id,
            "metric_key": profiling_service.URBA_METRIC_KEY,
            "updated_count": 0,
            "active_idus": loaded["active_idus"],
            "requested_idus": loaded["requested_idus"],
            "skipped_indesirables": loaded["skipped_indesirables"],
            "missing_geom": loaded["missing_geom"],
            "wfs_requests": 0,
            "errors": [],
            "duration_s": round(time.perf_counter() - t0, 2),
        }
    context_hits: list[dict] = []
    try:
        response = run_urba_profile(
            UrbaProfilRequest(
                parcelles=[ParcelleIn(**p) for p in loaded["parcelles"]],
                options=ProfilOptions(include_ecartes=False, include_raw=False, include_geometries=False),
            ),
            context_out=context_hits,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    with engine.begin() as conn:
        updated = profiling_service.store_urba_profile(conn, project_id, run_id, response)
        from api.recherche_de_foncier.enrichir.pool.urba_map_store import store_urba_context

        entites = store_urba_context(conn, project_id, run_id, context_hits, response)
    return {
        "status": "ok",
        "project_id": project_id,
        "run_id": run_id,
        "metric_key": profiling_service.URBA_METRIC_KEY,
        "updated_count": updated,
        "entites_count": entites,
        "active_idus": loaded["active_idus"],
        "requested_idus": loaded["requested_idus"],
        "skipped_indesirables": loaded["skipped_indesirables"],
        "missing_geom": loaded["missing_geom"],
        "wfs_requests": response.meta.wfs_requests,
        "errors": response.meta.errors,
        "duration_s": round(time.perf_counter() - t0, 2),
    }


@router.get("/{project_id}/pool/runs/{run_id}/urbanisme-geojson")
def urbanisme_geojson(project_id: str, run_id: str):
    """Entités GPU retenues du run, découpées à la parcelle + 100 m, en GeoJSON 4326."""
    from api.recherche_de_foncier.enrichir.pool.urba_map_store import load_urba_geojson

    if not _project_exists(project_id):
        raise HTTPException(404, f"Projet {project_id} introuvable")
    try:
        uuid.UUID(str(run_id))
    except ValueError:
        raise HTTPException(400, f"run_id invalide (UUID attendu): {run_id}")
    with engine.begin() as conn:
        if not pool_service.run_belongs_to_project(conn, project_id, run_id):
            raise HTTPException(404, f"Run {run_id} introuvable pour ce projet")
        return load_urba_geojson(conn, project_id, run_id)


@router.post("/{project_id}/pool/runs/{run_id}/recompute-autorisations-urba")
def recompute_autorisations_urba(
    project_id: str,
    run_id: str,
    body: AutorisationsUrbaRecomputeBody | None = Body(default=None),
):
    """
    Autorisations Sitadel (cadastre actuel) pour le pool du run.

    Écrit la métrique `autorisations_urba`. Ne lance pas la dureté foncière.
    Les parcelles indésirables du projet sont exclues.
    """
    if not _project_exists(project_id):
        raise HTTPException(404, f"Projet {project_id} introuvable")
    try:
        uuid.UUID(str(run_id))
    except ValueError:
        raise HTTPException(400, f"run_id invalide (UUID attendu): {run_id}")
    only_idus: set[str] | None = None
    avec_voisins = True
    if body is not None:
        avec_voisins = body.avec_voisins
        if body.idus:
            only_idus = {str(x).strip() for x in body.idus if str(x).strip()}
            if not only_idus:
                only_idus = None
    with engine.begin() as conn:
        pool_service.ensure_tables(conn)
        if not pool_service.run_belongs_to_project(conn, project_id, run_id):
            raise HTTPException(404, f"Run {run_id} introuvable pour ce projet")
        stats = profiling_service.compute_autorisations_urba_for_run(
            conn,
            project_id=project_id,
            run_id=run_id,
            exclude_indesirables=True,
            only_idus=only_idus,
            avec_voisins=avec_voisins,
        )
    return {
        "status": "ok",
        "project_id": project_id,
        "run_id": run_id,
        "metric_key": "autorisations_urba",
        **stats,
    }


@router.post("/{project_id}/pool/runs/{run_id}/parcelles")
def add_parcelles_to_run(
    project_id: str,
    run_id: str,
    body: AddParcellesBody,
):
    """
    Ajoute des parcelles au pool d'un run déjà calculé, sans rejouer le filtrage.

    Géométrie : cadastre local puis WFS IGN. Enrichissement + profilage (PM, score éco)
    comme les parcelles du run. La dureté foncière n'est pas lancée.
    """
    if not _project_exists(project_id):
        raise HTTPException(404, f"Projet {project_id} introuvable")
    try:
        uuid.UUID(str(run_id))
    except ValueError:
        raise HTTPException(400, f"run_id invalide (UUID attendu): {run_id}")
    idus = [str(x).strip() for x in body.idus if str(x).strip()]
    if not idus:
        raise HTTPException(400, "Aucun IDU fourni")
    result = add_parcelles.add_idus_to_pool_run(
        engine,
        project_id=project_id,
        run_id=run_id,
        idus=idus,
    )
    if not result.get("ok"):
        if result.get("error") == "run_not_found":
            raise HTTPException(404, f"Run {run_id} introuvable pour ce projet")
        raise HTTPException(400, result.get("error") or "Ajout impossible")
    return {
        "status": "ok",
        "project_id": project_id,
        "run_id": run_id,
        **{k: v for k, v in result.items() if k != "ok"},
    }


@router.post("/{project_id}/pool/runs/{run_id}/recompute-score")
def recompute_score(project_id: str, run_id: str):
    """
    Recalcule uniquement `score_eco` (sans exécuter le bulk des autres profilers).
    """
    if not _project_exists(project_id):
        raise HTTPException(404, f"Projet {project_id} introuvable")
    with engine.begin() as conn:
        pool_service.ensure_tables(conn)
        updated = profiling_service.compute_parcel_score_for_run(
            conn,
            project_id=project_id,
            run_id=run_id,
        )
    return {
        "status": "ok",
        "project_id": project_id,
        "run_id": run_id,
        "metric_key": "score_eco",
        "updated_count": updated,
    }
