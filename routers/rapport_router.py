"""
Rapport PDF — classement parcelles (même jeu de données que CSV / SHP).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

import psutil
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import FileResponse

from db import get_engine
from exports.export_classement_shp import export_classement_shp
from exports.router_exports import _get_project, _to_filtre_options_from_dict
from pool import pool_service
from pool.pool_service import get_all_metrics_grouped_by_idu

logger = logging.getLogger(__name__)
engine = get_engine()
router = APIRouter(tags=["rapport"])

# Répertoire rapport (pour import generer_rapport)
_RAPPORT_DIR = Path(__file__).resolve().parent.parent / "rapport"


def _load_parcelles_classement(project_id: str, run_id: str | None) -> tuple:
    """
    Même logique que l’export CSV/SHP parcelles : snapshot run ou last_results.
    Retourne (parcelles, options, pool_run_id, final_radius_km, aoi_id_str).
    """
    proj = _get_project(project_id)
    aoi_id = str(proj.get("aoi_id") or "")
    pool_run_id: str | None = None
    parcelles: list = []
    last_filter_dict: dict | None = None
    final_radius_km = 0.0
    if run_id:
        with engine.begin() as conn:
            pool_service.ensure_tables(conn)
            if not pool_service.run_belongs_to_project(conn, project_id, run_id):
                raise HTTPException(404, f"Run {run_id} introuvable pour ce projet")
            snap = pool_service.build_filter_snapshot_from_run(conn, project_id, run_id, aoi_id)
        if not snap:
            raise HTTPException(404, "Run introuvable ou scope non parcelles")
        parcelles = snap.get("parcelles") or []
        last_filter_dict = snap.get("filter_options") or {}
        final_radius_km = float(snap.get("final_radius_km") or 0)
        pool_run_id = str(snap.get("pool_run_id") or run_id)
    else:
        last_results = proj.get("last_results")
        last_filter = proj.get("last_filter")
        if not last_results or not last_filter:
            raise HTTPException(400, "Résultats ou filtre parcelles introuvables")
        if isinstance(last_results, str):
            last_results = json.loads(last_results)
        if isinstance(last_filter, str):
            last_filter = json.loads(last_filter)
        parcelles = last_results.get("parcelles", [])
        last_filter_dict = last_filter if isinstance(last_filter, dict) else {}
        final_radius_km = float(last_results.get("final_radius_km", 0) or 0)
        pool_run_id = last_results.get("pool_run_id")
    if not parcelles:
        raise HTTPException(400, "Aucune parcelle")
    options = _to_filtre_options_from_dict(last_filter_dict or {})
    return parcelles, options, pool_run_id, final_radius_km, aoi_id, proj


@router.get("/api/projects/{project_id}/export/rapport-pdf")
def export_rapport_pdf(
    project_id: str,
    background_tasks: BackgroundTasks,
    run_id: str | None = Query(None, description="Run pool — sinon dernier last_results"),
):
    """
    Génère le rapport PDF (pré-identification) à partir du classement parcelles courant
    (identique au CSV/SHP : même liste + métriques pool).
    """
    parcelles, options, pool_run_id, final_radius_km, aoi_id, proj = _load_parcelles_classement(
        project_id, run_id
    )

    metrics_by_idu = None
    if pool_run_id:
        try:
            with engine.begin() as conn:
                metrics_by_idu = get_all_metrics_grouped_by_idu(conn, project_id, str(pool_run_id))
        except Exception:
            logger.exception("Rapport PDF : lecture métriques pool ignorée (project_id=%s)", project_id)

    tmpdir = tempfile.mkdtemp(prefix="rapport_pdf_")
    dest: str | None = None
    rss_before = psutil.Process(os.getpid()).memory_info().rss
    try:
        shp_path = Path(tmpdir) / "parcelles.shp"
        try:
            export_classement_shp(
                engine,
                project_id,
                parcelles,
                options,
                final_radius_km,
                shp_path,
                aoi_id=aoi_id,
                metrics_by_idu=metrics_by_idu,
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

        if not shp_path.exists():
            raise HTTPException(500, "Génération SHP intermédiaire échouée.")

        pdf_path = Path(tmpdir) / "rapport_ecocompensation.pdf"
        foncier_id = str(proj.get("foncier_id") or "")
        aoi_uuid = str(proj.get("aoi_id") or "")

        # Import orchestrateur rapport (chemins relatifs au dossier rapport/)
        if str(_RAPPORT_DIR) not in sys.path:
            sys.path.insert(0, str(_RAPPORT_DIR))
        from generer_rapport import RapportInput, generer_rapport_complet

        inp = RapportInput(
            shp_path=str(shp_path),
            foncier_id=foncier_id,
            aoi_id=aoi_uuid,
            maitre_ouvrage=str(proj.get("name") or "—"),
            commune=str(proj.get("name") or "—"),
            type_projet="—",
            besoin_compensatoire_ha=0.0,
            especes_cibles=[],
            bureau_etudes="—",
            output_pdf=str(pdf_path),
            buffer_carte_m=600,
            buffer_contexte_m=1500,
        )

        try:
            generer_rapport_complet(inp)
        except Exception as e:
            logger.exception("Génération rapport PDF")
            raise HTTPException(500, f"Échec génération PDF : {e!s}") from e

        if not pdf_path.exists():
            raise HTTPException(500, "Fichier PDF non créé.")

        dest = shutil.copy2(pdf_path, tempfile.mktemp(suffix=".pdf"))
    finally:
        background_tasks.add_task(shutil.rmtree, tmpdir, ignore_errors=True)

    if not dest:
        raise HTTPException(500, "Copie du PDF finale impossible.")

    rss_after = psutil.Process(os.getpid()).memory_info().rss
    # Δ RSS sur la plage SHP + PDF (pas le pic instantané ; peut être légèrement bruité par le GC).
    delta_mb = (rss_after - rss_before) / (1024 * 1024)

    background_tasks.add_task(os.remove, dest)
    return FileResponse(
        dest,
        filename=f"rapport_{project_id[:8]}.pdf",
        media_type="application/pdf",
        headers={"X-Rapport-Rss-Delta-Mb": f"{delta_mb:.2f}"},
    )
