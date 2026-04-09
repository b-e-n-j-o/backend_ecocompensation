from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from db import get_engine
from pool import pool_service, profiling_service


router = APIRouter(prefix="/api/projects", tags=["pool"])
engine = get_engine()


def _project_exists(project_id: str) -> bool:
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT 1 FROM ecocompensation.projects WHERE id = :pid LIMIT 1"),
            {"pid": project_id},
        ).first()
    return row is not None


@router.get("/{project_id}/pool/runs")
def list_pool_runs(project_id: str):
    if not _project_exists(project_id):
        raise HTTPException(404, f"Projet {project_id} introuvable")
    with engine.begin() as conn:
        pool_service.ensure_tables(conn)
        runs = pool_service.list_runs(conn, project_id=project_id, limit=20)
    return {"runs": runs}


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


@router.get("/{project_id}/pool/{idu}/metrics")
def get_pool_metrics(project_id: str, idu: str, run_id: str):
    if not _project_exists(project_id):
        raise HTTPException(404, f"Projet {project_id} introuvable")
    with engine.begin() as conn:
        pool_service.ensure_tables(conn)
        rows = pool_service.get_metrics(conn, project_id=project_id, run_id=run_id, idu=idu)
    return {"run_id": run_id, "idu": idu, "metrics": rows}


@router.post("/{project_id}/pool/runs/{run_id}/recompute-metrics")
def recompute_metrics(project_id: str, run_id: str):
    if not _project_exists(project_id):
        raise HTTPException(404, f"Projet {project_id} introuvable")
    with engine.begin() as conn:
        pool_service.ensure_tables(conn)
        profiling_service.compute_metrics_for_run(conn, project_id=project_id, run_id=run_id)
    return {"status": "ok", "project_id": project_id, "run_id": run_id}
