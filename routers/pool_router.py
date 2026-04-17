from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
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


@router.get("/{project_id}/pool/runs/{run_id}/snapshot")
def get_pool_run_snapshot(project_id: str, run_id: str):
    """
    Reconstitue les résultats parcelles d’un run historique (liste + options filtre + entonnoir
    si `result_summary` a été rempli lors du POST /filter).
    """
    if not _project_exists(project_id):
        raise HTTPException(404, f"Projet {project_id} introuvable")
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
                "metric_key": "parcel_score_v1",
                "updated_count": updated,
                "mode": "score_only",
            }
        profiling_service.compute_metrics_for_run(conn, project_id=project_id, run_id=run_id)
    return {"status": "ok", "project_id": project_id, "run_id": run_id, "mode": "all_metrics"}


@router.post("/{project_id}/pool/runs/{run_id}/recompute-score")
def recompute_score(project_id: str, run_id: str):
    """
    Recalcule uniquement `parcel_score_v1` (sans exécuter le bulk des autres profilers).
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
        "metric_key": "parcel_score_v1",
        "updated_count": updated,
    }
