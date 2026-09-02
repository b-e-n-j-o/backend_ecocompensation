from __future__ import annotations

import uuid

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from db import get_engine
from pool import add_parcelles, pool_service, profiling_service


router = APIRouter(prefix="/api/projects", tags=["pool"])
all_runs_router = APIRouter(prefix="/api/pool", tags=["pool"])
engine = get_engine()


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


@router.post("/{project_id}/pool/runs/{run_id}/recompute-durete")
def recompute_durete(
    project_id: str,
    run_id: str,
    body: DureteRecomputeBody | None = Body(default=None),
):
    """
    Calcule la dureté foncière (attractivité) pour le pool du run,
    ou seulement les IDU fournis dans le body.

    Les parcelles marquées indésirables au niveau projet sont exclues du calcul.
    Rafraîchit aussi les métriques PM et le score composite.
    """
    if not _project_exists(project_id):
        raise HTTPException(404, f"Projet {project_id} introuvable")
    try:
        uuid.UUID(str(run_id))
    except ValueError:
        raise HTTPException(400, f"run_id invalide (UUID attendu): {run_id}")
    only_idus: set[str] | None = None
    if body and body.idus:
        only_idus = {str(x).strip() for x in body.idus if str(x).strip()}
        if not only_idus:
            only_idus = None
    with engine.begin() as conn:
        pool_service.ensure_tables(conn)
        if not pool_service.run_belongs_to_project(conn, project_id, run_id):
            raise HTTPException(404, f"Run {run_id} introuvable pour ce projet")
        stats = profiling_service.compute_durete_for_run(
            conn,
            project_id=project_id,
            run_id=run_id,
            exclude_indesirables=True,
            only_idus=only_idus,
        )
    return {
        "status": "ok",
        "project_id": project_id,
        "run_id": run_id,
        "metric_key": "durete_fonciere",
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
