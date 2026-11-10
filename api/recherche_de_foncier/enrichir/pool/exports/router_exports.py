#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Routes d'exports CSV/SHP (parcelles, UF, indésirables)."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import text

from db import get_engine
from api.recherche_de_foncier.enrichir.pool import pool_service
from api.recherche_de_foncier.enrichir.pool.pool_service import get_all_metrics_grouped_by_idu
from api.recherche_de_foncier.enrichir.pool.exports.qgis_encoding import QGIS_CSV_ENCODING
from filtre_options import FiltreOptions
from api.recherche_de_foncier.enrichir.pool.exports.export_classement_csv import export_classement_csv
from api.recherche_de_foncier.enrichir.pool.exports.export_uf_classement_csv import export_uf_classement_csv
from api.recherche_de_foncier.enrichir.pool.exports.export_classement_shp import export_classement_shp
from api.recherche_de_foncier.enrichir.pool.exports.export_uf_classement_shp import export_uf_classement_shp
from api.recherche_de_foncier.enrichir.pool.exports.export_etude_gpkg import write_etude_geopackage
from api.recherche_de_foncier.enrichir.pool.exports.export_jauge import filter_ilots, filter_parcelles, filter_results_uf
from api.recherche_de_foncier.enrichir.pool.exports.export_zip_readme import add_export_readme

_FILTER_REPORT_DIR = (
    Path(__file__).resolve().parents[3] / "filtrage" / "export" / "filter_report"
)

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
    if not unites:
        raise HTTPException(400, "Aucune unité foncière à exporter")
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
                add_export_readme(zf, "parcelles")
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
                add_export_readme(zf, "indesirables")
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
    if not last_results_uf:
        raise HTTPException(400, "Résultats UF introuvables")
    if isinstance(last_results_uf, str):
        last_results_uf = json.loads(last_results_uf)
    if isinstance(last_filter_uf, str):
        last_filter_uf = json.loads(last_filter_uf)
    options = _to_filtre_options_from_dict(last_filter_uf if isinstance(last_filter_uf, dict) else {})
    final_radius_km = float(last_results_uf.get("final_radius_km", 0) or 0)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        try:
            export_uf_classement_shp(
                engine, project_id, last_results_uf, options, tmp / "uf.shp", final_radius_km=final_radius_km
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        zip_path = tmp / "uf.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            add_export_readme(zf, "uf")
            for stem in ("unites_foncieres", "parcelles"):
                for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
                    f = tmp / f"{stem}{ext}"
                    if f.exists():
                        zf.write(f, f.name)
            gpkg = tmp / "uf.gpkg"
            if gpkg.exists():
                zf.write(gpkg, gpkg.name)
        with zipfile.ZipFile(zip_path, "r") as zr:
            if not zr.namelist():
                raise HTTPException(500, "Export SHP UF : archive vide.")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as nf:
            shutil.copy2(zip_path, nf.name)
            final_path = nf.name
    background_tasks.add_task(os.remove, final_path)
    return FileResponse(final_path, filename=f"uf_{project_id[:8]}.zip", media_type="application/zip")


def _load_parcelles_export(project_id: str, proj: dict, run_id: str | None):
    """Parcelles du classement (hors indésirables), options et métriques. Vide si aucun résultat."""
    aoi_id = str(proj.get("aoi_id") or "")
    parcelles: list = []
    last_filter_dict: dict | None = None
    pool_run_id: str | None = None
    if run_id:
        with engine.begin() as conn:
            pool_service.ensure_tables(conn)
            if not pool_service.run_belongs_to_project(conn, project_id, run_id):
                raise HTTPException(404, f"Run {run_id} introuvable pour ce projet")
            snap = pool_service.build_filter_snapshot_from_run(conn, project_id, run_id, aoi_id)
        if snap:
            parcelles = snap.get("parcelles") or []
            last_filter_dict = snap.get("filter_options") or {}
            pool_run_id = str(snap.get("pool_run_id") or run_id)
    else:
        last_results = proj.get("last_results")
        last_filter = proj.get("last_filter")
        if isinstance(last_results, str):
            last_results = json.loads(last_results)
        if isinstance(last_filter, str):
            last_filter = json.loads(last_filter)
        if isinstance(last_results, dict):
            parcelles = last_results.get("parcelles") or []
            pool_run_id = last_results.get("pool_run_id")
        last_filter_dict = last_filter if isinstance(last_filter, dict) else {}
    if parcelles:
        with engine.begin() as conn:
            pool_service.ensure_tables(conn)
            parcelles = pool_service.filter_parcelles_excluding_project_indesirables(
                conn, project_id, parcelles
            )
    metrics_by_idu = None
    if pool_run_id and parcelles:
        try:
            with engine.begin() as conn:
                metrics_by_idu = get_all_metrics_grouped_by_idu(conn, project_id, str(pool_run_id))
        except Exception:
            logger.exception("Export étude : métriques pool ignorées (project_id=%s)", project_id)
    return parcelles, _to_filtre_options_from_dict(last_filter_dict or {}), aoi_id, metrics_by_idu


@router.get("/api/projects/{project_id}/export/etude.gpkg")
def export_etude_gpkg(
    project_id: str,
    background_tasks: BackgroundTasks,
    layers: str = Query(
        "parcelles,uf,ilots",
        description="Liste séparée par des virgules : parcelles, uf, ilots",
    ),
    run_id: str | None = Query(None, description="Run pool parcelles — sinon dernier last_results"),
    max_distance_km: float | None = Query(None, description="Jauge : distance max au projet, en km"),
    min_surface_ha: float | None = Query(None, description="Jauge : surface minimale, en ha"),
):
    """Un GeoPackage QGIS : les couches cochées + zone projet + aire d'étude."""
    wanted = {p.strip().lower() for p in layers.split(",") if p.strip()}
    unknown = wanted - {"parcelles", "uf", "ilots"}
    if not wanted or unknown:
        raise HTTPException(400, "Paramètre layers : parcelles, uf, ilots.")
    proj = _get_project(project_id)
    jauge = max_distance_km is not None or (min_surface_ha is not None and min_surface_ha > 0)
    parcelles, options, aoi_id, metrics = _load_parcelles_export(project_id, proj, run_id)
    if jauge and "parcelles" in wanted:
        parcelles = filter_parcelles(parcelles, max_distance_km, min_surface_ha)
    results_uf = proj.get("last_results_uf")
    if isinstance(results_uf, str):
        results_uf = json.loads(results_uf)
    if not isinstance(results_uf, dict):
        results_uf = None
    elif jauge and "uf" in wanted:
        results_uf = filter_results_uf(results_uf, max_distance_km, min_surface_ha)
    ilot_ids: set[str] | None = None
    if jauge and "ilots" in wanted:
        from api.recherche_de_foncier.filtrage.layers.rpg.rpg_ilots.api import get_results

        raw_ilots = (get_results(engine, project_id) or {}).get("ilots") or []
        ilot_ids = {
            str(il.get("num_ilot"))
            for il in filter_ilots(raw_ilots, max_distance_km, min_surface_ha)
            if il.get("num_ilot") is not None
        }
    with tempfile.NamedTemporaryFile(delete=False, suffix=".gpkg") as nf:
        gpkg_path = Path(nf.name)
    try:
        written = write_etude_geopackage(
            engine,
            project_id,
            gpkg_path,
            include_parcelles="parcelles" in wanted,
            include_uf="uf" in wanted,
            include_ilots="ilots" in wanted,
            parcelles=parcelles,
            parcelle_options=options,
            aoi_id=aoi_id,
            metrics_by_idu=metrics,
            results_uf=results_uf,
            ilot_ids=ilot_ids,
        )
    except ValueError as e:
        gpkg_path.unlink(missing_ok=True)
        raise HTTPException(400, str(e)) from e
    background_tasks.add_task(os.remove, str(gpkg_path))
    logger.info("Export étude GPKG %s couches=%s", project_id, written)
    return FileResponse(
        str(gpkg_path),
        filename=f"etude_{project_id[:8]}.gpkg",
        media_type="application/geopackage+sqlite3",
    )


@router.get("/api/projects/{project_id}/export/fiche-filtrage.pdf")
def export_fiche_filtrage(
    project_id: str,
    background_tasks: BackgroundTasks,
):
    """Fiche A4 des critères de filtrage exécutés (config d'entrée + entonnoir)."""
    proj = _get_project(project_id)
    last = proj.get("last_results")
    if isinstance(last, str):
        last = json.loads(last)
    if not isinstance(last, dict):
        last = {}
    cfg = last.get("filter_config")
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except json.JSONDecodeError:
            cfg = None
    if not isinstance(cfg, dict) or not cfg:
        stored = proj.get("last_filter")
        if isinstance(stored, str):
            try:
                stored = json.loads(stored)
            except json.JSONDecodeError:
                stored = None
        cfg = stored if isinstance(stored, dict) else {}
    funnel = last.get("funnel") or []
    if isinstance(funnel, str):
        try:
            funnel = json.loads(funnel)
        except json.JSONDecodeError:
            funnel = []
    if not isinstance(funnel, list):
        funnel = []
    if not funnel and not cfg:
        raise HTTPException(400, "Aucun filtrage enregistré pour cette étude.")
    radius = last.get("final_radius_km")
    if radius is None:
        radius = cfg.get("buffer_km")
    total = last.get("total")
    if total is None:
        total = last.get("n_after_filter")
    if total is None:
        total = len(last.get("parcelles") or [])
    payload = {
        "etude": {
            "nom": proj.get("name"),
            "projet": proj.get("name"),
            "organisme": "Kerelia",
        },
        "computed_at": last.get("run_created_at"),
        "buffer_km": radius,
        "filter_config": cfg,
        "funnel": funnel,
        "total": int(total or 0),
        "aoi_total": last.get("n_tiled"),
    }
    if str(_FILTER_REPORT_DIR) not in sys.path:
        sys.path.insert(0, str(_FILTER_REPORT_DIR))
    from fiche import render_fiche_pdf_bytes

    try:
        pdf = render_fiche_pdf_bytes(payload)
    except Exception as exc:
        logger.exception("Fiche filtrage PDF")
        raise HTTPException(500, f"Impossible de générer la fiche : {exc}") from exc
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as nf:
        path = Path(nf.name)
        path.write_bytes(pdf)
    background_tasks.add_task(os.remove, str(path))
    return FileResponse(
        str(path),
        filename=f"fiche_filtrage_{project_id[:8]}.pdf",
        media_type="application/pdf",
    )

