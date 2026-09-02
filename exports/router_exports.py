#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Routes d'exports CSV/SHP (parcelles, UF, indésirables)."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import text

from db import get_engine
from pool import pool_service
from pool.pool_service import get_all_metrics_grouped_by_idu
from exports.qgis_encoding import QGIS_CSV_ENCODING
from filtre_options import FiltreOptions
from exports.export_classement_csv import export_classement_csv
from exports.export_uf_classement_csv import export_uf_classement_csv
from exports.export_classement_shp import export_classement_shp
from exports.export_uf_classement_shp import export_uf_classement_shp

logger = logging.getLogger(__name__)
engine = get_engine()
router = APIRouter(tags=["exports"])


def _get_project(project_id: str) -> dict:
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT * FROM ecocompensation.projects WHERE id = :pid"),
            {"pid": project_id},
        ).mappings().one_or_none()
    if not row:
        raise HTTPException(404, f"Projet {project_id} introuvable")
    return dict(row)


def _to_filtre_options_from_dict(raw: dict[str, Any]) -> FiltreOptions:
    return FiltreOptions.from_dict(raw)


@router.get("/api/projects/{project_id}/export/csv")
def export_csv(
    project_id: str,
    background_tasks: BackgroundTasks,
    scope: str = Query("parcelles", description="parcelles | uf | indesirables"),
    run_id: str | None = Query(None, description="Run pool parcelles — sinon dernier last_results du projet"),
):
    s = scope.lower().strip()
    if s not in ("parcelles", "uf", "indesirables"):
        raise HTTPException(400, "Paramètre scope invalide (parcelles, uf ou indesirables).")

    proj = _get_project(project_id)

    if s == "parcelles":
        pool_run_id: str | None = None
        parcelles: list = []
        last_filter_dict: dict | None = None
        if run_id:
            aoi_id_str = str(proj.get("aoi_id") or "")
            with engine.begin() as conn:
                pool_service.ensure_tables(conn)
                if not pool_service.run_belongs_to_project(conn, project_id, run_id):
                    raise HTTPException(404, f"Run {run_id} introuvable pour ce projet")
                snap = pool_service.build_filter_snapshot_from_run(conn, project_id, run_id, aoi_id_str)
            if not snap:
                raise HTTPException(404, "Run introuvable ou scope non parcelles")
            parcelles = snap.get("parcelles") or []
            pool_run_id = str(snap.get("pool_run_id") or run_id)
            last_filter_dict = snap.get("filter_options") or {}
        else:
            last_results = proj.get("last_results")
            last_filter = proj.get("last_filter")
            if not last_results:
                raise HTTPException(400, "Aucun résultat parcelles")
            if isinstance(last_results, str):
                last_results = json.loads(last_results)
            parcelles = last_results.get("parcelles", [])
            pool_run_id = last_results.get("pool_run_id")
            if isinstance(last_filter, str):
                last_filter = json.loads(last_filter)
            last_filter_dict = last_filter if isinstance(last_filter, dict) else {}
        if not parcelles:
            raise HTTPException(400, "Aucune parcelle")
        with engine.begin() as conn:
            pool_service.ensure_tables(conn)
            parcelles = pool_service.filter_parcelles_excluding_project_indesirables(
                conn, project_id, parcelles
            )
        if not parcelles:
            raise HTTPException(400, "Aucune parcelle dans le classement (toutes sont indésirables).")
        options = _to_filtre_options_from_dict(last_filter_dict or {})
        metrics_by_idu = None
        if pool_run_id:
            try:
                with engine.begin() as conn:
                    metrics_by_idu = get_all_metrics_grouped_by_idu(conn, project_id, str(pool_run_id))
            except Exception:
                logger.exception(
                    "Export CSV parcelles : lecture métriques pool ignorée (project_id=%s)",
                    project_id,
                )
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".csv", encoding=QGIS_CSV_ENCODING) as nf:
            csv_path = Path(nf.name)
            export_classement_csv(parcelles, csv_path, metrics_by_idu=metrics_by_idu, options=options)
        background_tasks.add_task(os.remove, str(csv_path))
        return FileResponse(
            str(csv_path),
            filename=f"parcelles_{project_id[:8]}.csv",
            media_type="text/csv; charset=utf-8",
        )

    if s == "indesirables":
        with engine.begin() as conn:
            pool_service.ensure_tables(conn)
            payload = pool_service.get_project_indesirables_payload(conn, project_id=project_id)
        parcelles = payload.get("parcelles") or []
        if not parcelles:
            raise HTTPException(400, "Aucune parcelle indésirable")
        metrics_by_idu = payload.get("by_idu") if isinstance(payload.get("by_idu"), dict) else None
        options = _to_filtre_options_from_dict({})
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".csv", encoding=QGIS_CSV_ENCODING) as nf:
            csv_path = Path(nf.name)
            export_classement_csv(parcelles, csv_path, metrics_by_idu=metrics_by_idu, options=options)
        background_tasks.add_task(os.remove, str(csv_path))
        return FileResponse(
            str(csv_path),
            filename=f"indesirables_{project_id[:8]}.csv",
            media_type="text/csv; charset=utf-8",
        )

    # scope == uf
    last_results_uf = proj.get("last_results_uf")
    if not last_results_uf:
        raise HTTPException(400, "Aucun résultat unités foncières")
    if isinstance(last_results_uf, str):
        last_results_uf = json.loads(last_results_uf)
    unites = last_results_uf.get("unites_foncieres") or []
    n_ss = sum(len(uf.get("sous_ensembles") or []) for uf in unites)
    if n_ss == 0:
        raise HTTPException(400, "Aucun sous-ensemble UF à exporter")
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".csv", encoding=QGIS_CSV_ENCODING) as nf:
        csv_path = Path(nf.name)
        export_uf_classement_csv(last_results_uf, csv_path)
    background_tasks.add_task(os.remove, str(csv_path))
    return FileResponse(
        str(csv_path),
        filename=f"uf_{project_id[:8]}.csv",
        media_type="text/csv; charset=utf-8",
    )


@router.get("/api/projects/{project_id}/export/shp")
def export_shp(
    project_id: str,
    background_tasks: BackgroundTasks,
    scope: str = Query("parcelles", description="parcelles | uf | indesirables"),
    run_id: str | None = Query(None, description="Run pool parcelles — sinon dernier last_results du projet"),
):
    s = scope.lower().strip()
    if s not in ("parcelles", "uf", "indesirables"):
        raise HTTPException(400, "Paramètre scope invalide (parcelles, uf ou indesirables).")

    proj = _get_project(project_id)

    if s == "parcelles":
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
        with engine.begin() as conn:
            pool_service.ensure_tables(conn)
            parcelles = pool_service.filter_parcelles_excluding_project_indesirables(
                conn, project_id, parcelles
            )
        if not parcelles:
            raise HTTPException(400, "Aucune parcelle dans le classement (toutes sont indésirables).")
        options = _to_filtre_options_from_dict(last_filter_dict or {})
        metrics_by_idu = None
        if pool_run_id:
            try:
                with engine.begin() as conn:
                    metrics_by_idu = get_all_metrics_grouped_by_idu(conn, project_id, str(pool_run_id))
            except Exception:
                logger.exception(
                    "Export SHP parcelles : lecture métriques pool ignorée (project_id=%s)",
                    project_id,
                )
        with tempfile.TemporaryDirectory() as tmpdir:
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
            zip_path = Path(tmpdir) / "parcelles.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
                    f = shp_path.with_suffix(ext)
                    if f.exists():
                        zf.write(f, f.name)
                gpkg = shp_path.with_suffix(".gpkg")
                if gpkg.exists():
                    zf.write(gpkg, gpkg.name)
            with zipfile.ZipFile(zip_path, "r") as zr:
                if not zr.namelist():
                    raise HTTPException(500, "Export SHP parcelles : archive vide.")
            with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as nf:
                shutil.copy2(zip_path, nf.name)
                final_path = nf.name
        background_tasks.add_task(os.remove, final_path)
        return FileResponse(
            final_path, filename=f"parcelles_{project_id[:8]}.zip", media_type="application/zip"
        )

    if s == "indesirables":
        aoi_id = str(proj.get("aoi_id") or "")
        with engine.begin() as conn:
            pool_service.ensure_tables(conn)
            payload = pool_service.get_project_indesirables_payload(conn, project_id=project_id)
        parcelles = payload.get("parcelles") or []
        if not parcelles:
            raise HTTPException(400, "Aucune parcelle indésirable")
        metrics_by_idu = payload.get("by_idu") if isinstance(payload.get("by_idu"), dict) else None
        options = _to_filtre_options_from_dict({})
        with tempfile.TemporaryDirectory() as tmpdir:
            shp_path = Path(tmpdir) / "indesirables.shp"
            try:
                export_classement_shp(
                    engine,
                    project_id,
                    parcelles,
                    options,
                    0.0,
                    shp_path,
                    aoi_id=aoi_id,
                    metrics_by_idu=metrics_by_idu,
                )
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
            zip_path = Path(tmpdir) / "indesirables.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
                    f = shp_path.with_suffix(ext)
                    if f.exists():
                        zf.write(f, f.name)
                gpkg = shp_path.with_suffix(".gpkg")
                if gpkg.exists():
                    zf.write(gpkg, gpkg.name)
            with zipfile.ZipFile(zip_path, "r") as zr:
                if not zr.namelist():
                    raise HTTPException(500, "Export SHP indésirables : archive vide.")
            with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as nf:
                shutil.copy2(zip_path, nf.name)
                final_path = nf.name
        background_tasks.add_task(os.remove, final_path)
        return FileResponse(
            final_path, filename=f"indesirables_{project_id[:8]}.zip", media_type="application/zip"
        )

    # scope == uf
    last_results_uf = proj.get("last_results_uf")
    last_filter_uf = proj.get("last_filter_uf")
    if not last_results_uf or not last_filter_uf:
        raise HTTPException(400, "Résultats ou filtre UF introuvables")
    if isinstance(last_results_uf, str):
        last_results_uf = json.loads(last_results_uf)
    if isinstance(last_filter_uf, str):
        last_filter_uf = json.loads(last_filter_uf)
    options = _to_filtre_options_from_dict(last_filter_uf if isinstance(last_filter_uf, dict) else {})
    final_radius_km = float(last_results_uf.get("final_radius_km", 0) or 0)
    with tempfile.TemporaryDirectory() as tmpdir:
        shp_path = Path(tmpdir) / "uf_subsets.shp"
        try:
            export_uf_classement_shp(
                engine, project_id, last_results_uf, options, shp_path, final_radius_km=final_radius_km
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        zip_path = Path(tmpdir) / "uf.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
                f = shp_path.with_suffix(ext)
                if f.exists():
                    zf.write(f, f.name)
        with zipfile.ZipFile(zip_path, "r") as zr:
            if not zr.namelist():
                raise HTTPException(500, "Export SHP UF : archive vide.")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as nf:
            shutil.copy2(zip_path, nf.name)
            final_path = nf.name
    background_tasks.add_task(os.remove, final_path)
    return FileResponse(final_path, filename=f"uf_{project_id[:8]}.zip", media_type="application/zip")

