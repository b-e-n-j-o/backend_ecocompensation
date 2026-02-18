#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py
=======

FastAPI — API backend pour l'outil de filtrage/scoring parcellaire KERELIA.

Endpoints :
    POST   /api/projects                     créer un projet
    GET    /api/projects                     lister les projets
    GET    /api/projects/{id}                détail d'un projet
    DELETE /api/projects/{id}                supprimer projet + données AOI
    POST   /api/projects/{id}/fetch          lancer l'orchestration des couches
    POST   /api/projects/{id}/filter         appliquer le filtre + scoring
    GET    /api/projects/{id}/results        derniers résultats
    GET    /api/memory                       RAM du processus backend
    WS     /ws/projects/{id}/fetch-progress  suivi temps réel des fetches
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import psutil
import geopandas as gpd
from dotenv import load_dotenv
from fastapi import (
    FastAPI, HTTPException, UploadFile, File, Form,
    WebSocket, WebSocketDisconnect, BackgroundTasks,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import text

# Modules métier
from orchestrator import run_orchestration
from layers.layer_runner import LAYER_REGISTRY
from db import get_engine

# vrai_filtre et scoring (tes scripts actuels, importés directement)
import sys
sys.path.insert(0, str(Path(__file__).parent))
from vrai_filtre import FiltreOptions, run as run_filtre
from vrai_filtre_puis_scoring import _score_parcelle

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────

load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

BUFFER_DEFAULT_M = 12_000

# ─────────────────────────────────────────────
# DB engine (singleton)
# ─────────────────────────────────────────────

engine = get_engine()


# ─────────────────────────────────────────────
# DB init
# ─────────────────────────────────────────────

def init_db():
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE SCHEMA IF NOT EXISTS ecocompensation;

            CREATE TABLE IF NOT EXISTS ecocompensation.aoi (
                id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                code_insee  text NOT NULL,
                buffer_m    integer NOT NULL,
                geom_2154   geometry NOT NULL,
                created_at  timestamptz DEFAULT now()
            );

            CREATE TABLE IF NOT EXISTS ecocompensation.projects (
                id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                name           text NOT NULL,
                aoi_id         uuid REFERENCES ecocompensation.aoi(id) ON DELETE SET NULL,
                status         text NOT NULL DEFAULT 'created',
                layers_status  jsonb NOT NULL DEFAULT '{}',
                last_filter    jsonb NULL,
                last_results   jsonb NULL,
                created_at     timestamptz DEFAULT now(),
                updated_at     timestamptz DEFAULT now()
            );
        """))
    logger.info("DB initialisée.")


# ─────────────────────────────────────────────
# WebSocket manager
# ─────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self._connections: dict[str, list[WebSocket]] = {}

    async def connect(self, project_id: str, ws: WebSocket):
        await ws.accept()
        self._connections.setdefault(project_id, []).append(ws)

    def disconnect(self, project_id: str, ws: WebSocket):
        conns = self._connections.get(project_id, [])
        if ws in conns:
            conns.remove(ws)

    async def broadcast(self, project_id: str, data: dict):
        dead = []
        for ws in self._connections.get(project_id, []):
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(project_id, ws)


ws_manager = ConnectionManager()


# ─────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


# ─────────────────────────────────────────────
# App
# ─────────────────────────────────────────────

app = FastAPI(title="KERELIA Ecocompensation API", version="1.0.0", lifespan=lifespan)

origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "https://ecocompensation-frontend-khub.vercel.app",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Servir le front Vite buildé (si dist/ existe)
_dist = Path(__file__).parent.parent / "frontend" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="static")


# ─────────────────────────────────────────────
# Schémas Pydantic
# ─────────────────────────────────────────────

class ProjectCreate(BaseModel):
    name: str
    buffer_m: int = BUFFER_DEFAULT_M
    # code_insee OU GPKG uploadé — géré dans l'endpoint


class FiltreOptionsDTO(BaseModel):
    zdv_natures: list[str] = Field(default_factory=list)
    troncon_hydro_mode: str = "intersect"
    troncon_hydro_radius_m: float = 500.0
    surface_hydro_mode: str = "within_radius"
    surface_hydro_radius_m: float = 500.0
    miller_threshold: float = 0.39
    min_area_ha: float = 7.0
    target_count: int = 50
    radius_start_km: float = 10.0
    radius_min_km: float = 1.0
    # Poids du scoring (configurables)
    score_dist_lt2km: int = 3
    score_dist_lt5km: int = 2
    score_dist_lt10km: int = 1
    score_surface_ge20ha: int = 1
    score_miller_ge05: int = 1
    score_hydro_lt100m: int = 1
    # Seuils du scoring (configurables)
    score_threshold_miller: float = 0.5
    score_threshold_surface_ha: float = 20.0
    score_threshold_hydro_m: float = 100.0
    score_threshold_dist_2km: float = 2.0
    score_threshold_dist_5km: float = 5.0


class FilterRequest(BaseModel):
    options: FiltreOptionsDTO = Field(default_factory=FiltreOptionsDTO)


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _get_process_memory_mb() -> float:
    """RAM utilisée par le processus courant (RSS), en Mo."""
    try:
        proc = psutil.Process(os.getpid())
        return round(proc.memory_info().rss / (1024 * 1024), 2)
    except Exception:
        return 0.0


def _get_project(project_id: str) -> dict:
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT * FROM ecocompensation.projects WHERE id = :pid"),
            {"pid": project_id},
        ).mappings().one_or_none()
    if not row:
        raise HTTPException(404, f"Projet {project_id} introuvable")
    return dict(row)


def _dto_to_filtre_options(dto: FiltreOptionsDTO) -> FiltreOptions:
    return FiltreOptions(
        zdv_natures=dto.zdv_natures,
        troncon_hydro_mode=dto.troncon_hydro_mode,
        troncon_hydro_radius_m=dto.troncon_hydro_radius_m,
        surface_hydro_mode=dto.surface_hydro_mode,
        surface_hydro_radius_m=dto.surface_hydro_radius_m,
    )


def _score_with_weights(p: dict, opts: FiltreOptionsDTO) -> tuple[int, list[dict]]:
    """Scoring avec poids et seuils configurables depuis l'interface."""
    dist_m = p.get("distance_centre_m") or 999999.0
    surface_ha = float(p.get("surface_ha") or 0)
    miller = float(p.get("miller") or 0)
    dist_hydro_m = p.get("dist_surface_hydro_m")

    thr_dist_2_m = opts.score_threshold_dist_2km * 1000.0
    thr_dist_5_m = opts.score_threshold_dist_5km * 1000.0

    pts = 0
    score_details = []

    if dist_m < thr_dist_2_m:
        pts += opts.score_dist_lt2km
        score_details.append({"critere": "Distance au centre AOI", "points": opts.score_dist_lt2km, "raison": f"< {opts.score_threshold_dist_2km} km ({dist_m/1000:.1f} km)"})
    elif dist_m < thr_dist_5_m:
        pts += opts.score_dist_lt5km
        score_details.append({"critere": "Distance au centre AOI", "points": opts.score_dist_lt5km, "raison": f"{opts.score_threshold_dist_2km}–{opts.score_threshold_dist_5km} km ({dist_m/1000:.1f} km)"})
    else:
        pts += opts.score_dist_lt10km
        score_details.append({"critere": "Distance au centre AOI", "points": opts.score_dist_lt10km, "raison": f"{opts.score_threshold_dist_5km}–10 km ({dist_m/1000:.1f} km)"})

    if surface_ha >= opts.score_threshold_surface_ha:
        pts += opts.score_surface_ge20ha
        score_details.append({"critere": "Surface", "points": opts.score_surface_ge20ha, "raison": f"≥ {opts.score_threshold_surface_ha} ha ({surface_ha:.1f} ha)"})
    else:
        score_details.append({"critere": "Surface", "points": 0, "raison": f"< {opts.score_threshold_surface_ha} ha ({surface_ha:.1f} ha)"})

    if miller >= opts.score_threshold_miller:
        pts += opts.score_miller_ge05
        score_details.append({"critere": "Coefficient de Miller", "points": opts.score_miller_ge05, "raison": f"≥ {opts.score_threshold_miller} ({miller:.2f})"})
    else:
        score_details.append({"critere": "Coefficient de Miller", "points": 0, "raison": f"< {opts.score_threshold_miller} ({miller:.2f})"})

    if dist_hydro_m is not None and dist_hydro_m < opts.score_threshold_hydro_m:
        pts += opts.score_hydro_lt100m
        score_details.append({"critere": "Proximité hydro", "points": opts.score_hydro_lt100m, "raison": f"< {opts.score_threshold_hydro_m:.0f} m ({dist_hydro_m:.0f} m)"})
    else:
        score_details.append({"critere": "Proximité hydro", "points": 0, "raison": f"{dist_hydro_m:.0f} m" if dist_hydro_m else "—"})

    return pts, score_details


# ─────────────────────────────────────────────
# Routes — Projets
# ─────────────────────────────────────────────

@app.get("/api/projects")
def list_projects():
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT id, name, status, layers_status, created_at, updated_at FROM ecocompensation.projects ORDER BY created_at DESC")
        ).mappings().all()
    return [dict(r) for r in rows]


@app.post("/api/projects", status_code=201)
async def create_project(
    name: str = Form(...),
    buffer_m: int = Form(BUFFER_DEFAULT_M),
    gpkg_file: UploadFile | None = File(None),
    code_insee: str | None = Form(None),
):
    """
    Crée un projet à partir d'un fichier GPKG uploadé OU d'un code INSEE.
    Insère l'AOI dans ecocompensation.aoi et crée l'entrée projet.
    """
    if not gpkg_file and not code_insee:
        raise HTTPException(400, "Fournir soit un fichier GPKG soit un code_insee")

    # --- Construire la géométrie de l'AOI ---
    if gpkg_file:
        import tempfile, shutil
        suffix = Path(gpkg_file.filename).suffix
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            shutil.copyfileobj(gpkg_file.file, tmp)
            tmp_path = tmp.name
        gdf = gpd.read_file(tmp_path)
        Path(tmp_path).unlink(missing_ok=True)
        if gdf.crs is None or gdf.crs.to_string() != "EPSG:2154":
            gdf = gdf.to_crs("EPSG:2154")
        code = "GPKG"
    else:
        # Fetch WFS IGN commune par code INSEE
        import requests as req
        resp = req.get(
            "https://data.geopf.fr/wfs/ows",
            params={
                "service": "WFS", "version": "2.0.0", "request": "GetFeature",
                "typeNames": "BDTOPO_V3:commune",
                "outputFormat": "application/json",
                "CQL_FILTER": f"code_insee='{code_insee}'",
            },
            timeout=30,
        )
        gdf = gpd.read_file(resp.text)
        if gdf.empty:
            raise HTTPException(404, f"Commune {code_insee} introuvable")
        if gdf.crs is None or gdf.crs.to_string() != "EPSG:2154":
            gdf = gdf.to_crs("EPSG:2154")
        code = code_insee

    union_geom = gdf.union_all()
    aoi_geom = union_geom.buffer(buffer_m, resolution=32)

    with engine.begin() as conn:
        row = conn.execute(
            text("""
                INSERT INTO ecocompensation.aoi (code_insee, buffer_m, geom_2154)
                VALUES (:code, :buf, ST_GeomFromText(:wkt, 2154))
                RETURNING id;
            """),
            {"code": code, "buf": buffer_m, "wkt": aoi_geom.wkt},
        ).mappings().one()
        aoi_id = str(row["id"])

        proj_row = conn.execute(
            text("""
                INSERT INTO ecocompensation.projects (name, aoi_id, status)
                VALUES (:name, :aoi_id, 'created')
                RETURNING id, name, status, created_at;
            """),
            {"name": name, "aoi_id": aoi_id},
        ).mappings().one()

    return {
        "id": str(proj_row["id"]),
        "name": proj_row["name"],
        "status": proj_row["status"],
        "aoi_id": aoi_id,
        "created_at": proj_row["created_at"].isoformat(),
    }


@app.get("/api/projects/{project_id}")
def get_project(project_id: str):
    return _get_project(project_id)


@app.delete("/api/projects/{project_id}", status_code=204)
def delete_project(project_id: str):
    proj = _get_project(project_id)
    aoi_id = proj.get("aoi_id")

    RESULT_TABLES = [
        "ecocompensation_results.parcelles",
        "ecocompensation_results.ebc",
        "ecocompensation_results.mesures_compensatoire_surf",
        "ecocompensation_results.mesures_compensatoire_lin",
        "ecocompensation_results.mesures_compensatoire_pct",
        "ecocompensation_results.mesures_compensatoire_commune",
        "ecocompensation_results.patrimoine_naturel",
        "ecocompensation_results.zone_de_vegetation",
        "ecocompensation_results.zone_humide",
        "ecocompensation_results.troncons_hydro",
        "ecocompensation_results.surfaces_hydro",
        "ecocompensation_results.surfaces_elementaires",
    ]

    with engine.begin() as conn:
        if aoi_id:
            for t in RESULT_TABLES:
                try:
                    conn.execute(text(f"DELETE FROM {t} WHERE aoi_id = :aid"), {"aid": str(aoi_id)})
                except Exception:
                    pass
            conn.execute(text("DELETE FROM ecocompensation.aoi WHERE id = :aid"), {"aid": str(aoi_id)})
        conn.execute(text("DELETE FROM ecocompensation.projects WHERE id = :pid"), {"pid": project_id})


# ─────────────────────────────────────────────
# Route — Fetch (lance l'orchestration)
# ─────────────────────────────────────────────

# Stocke les tâches en cours pour éviter les doublons
_running_fetches: set[str] = set()


@app.post("/api/projects/{project_id}/fetch")
async def start_fetch(project_id: str, background_tasks: BackgroundTasks):
    proj = _get_project(project_id)
    if proj["status"] == "fetching":
        raise HTTPException(409, "Fetch déjà en cours pour ce projet")
    if project_id in _running_fetches:
        raise HTTPException(409, "Fetch déjà en cours pour ce projet")

    aoi_id = str(proj["aoi_id"])
    _running_fetches.add(project_id)

    async def _run():
        try:
            async def push(data: dict):
                await ws_manager.broadcast(project_id, data)

            await run_orchestration(engine, project_id, aoi_id, push)
        finally:
            _running_fetches.discard(project_id)

    background_tasks.add_task(_run)
    return {"status": "started", "project_id": project_id}


# ─────────────────────────────────────────────
# Route — Filter + Scoring
# ─────────────────────────────────────────────

@app.post("/api/projects/{project_id}/filter")
def run_filter(project_id: str, body: FilterRequest):
    proj = _get_project(project_id)
    if proj["status"] not in ("ready", "ready_with_errors", "done"):
        raise HTTPException(400, f"Le projet n'est pas prêt (status={proj['status']})")

    aoi_id = str(proj["aoi_id"])
    opts_dto = body.options

    # Centre de l'AOI
    with engine.begin() as conn:
        row = conn.execute(
            text("""
                SELECT ST_X(ST_Centroid(geom_2154)) AS cx,
                       ST_Y(ST_Centroid(geom_2154)) AS cy
                FROM ecocompensation.aoi WHERE id = :aid
            """),
            {"aid": aoi_id},
        ).mappings().one_or_none()
    if not row:
        raise HTTPException(404, "AOI introuvable")

    cx, cy = row["cx"], row["cy"]
    options = _dto_to_filtre_options(opts_dto)

    # Comptage des parcelles candidates dans l'AOI (best-effort, ne doit jamais faire planter)
    parcelles_total: int | None = None
    try:
        with engine.begin() as conn:
            parcelles_total = conn.execute(
                text("""
                    SELECT COUNT(*)
                    FROM ecocompensation_results.parcelles p
                    WHERE p.aoi_id = :aid
                """),
                {"aid": aoi_id},
            ).scalar()
    except Exception:
        logger.debug("Impossible de compter cadastre.parcelles (table absente ?)", exc_info=True)

    if parcelles_total is not None:
        try:
            # On est dans un thread de worker (run_in_threadpool) : il faut
            # planifier le broadcast sur la boucle asyncio principale.
            loop = asyncio.get_event_loop()
            loop.call_soon_threadsafe(
                asyncio.ensure_future,
                ws_manager.broadcast(
                    project_id,
                    {
                        "event": "parcelles_total",
                        "total": int(parcelles_total or 0),
                    },
                ),
            )
        except Exception:
            logger.debug("Impossible d'envoyer l'event parcelles_total", exc_info=True)

    ram_before_mb = _get_process_memory_mb()
    logger.info("Filtre: RAM avant run_filtre = %.2f Mo", ram_before_mb)

    result = run_filtre(
        engine, aoi_id, cx, cy, options,
        return_parcelles=True,
        miller_threshold=opts_dto.miller_threshold,
        min_area_ha=opts_dto.min_area_ha,
        radius_start_km=opts_dto.radius_start_km,
        radius_min_km=opts_dto.radius_min_km,
        target_count=opts_dto.target_count,
    )

    ram_after_filtre_mb = _get_process_memory_mb()
    logger.info("Filtre: RAM après run_filtre = %.2f Mo (delta = %.2f Mo)", ram_after_filtre_mb, ram_after_filtre_mb - ram_before_mb)

    if result is None:
        return {
            "parcelles": [], "final_radius_km": 0, "total": 0, "funnel": [],
            "memory": {"ram_mb_before": ram_before_mb, "ram_mb_after": ram_after_filtre_mb, "ram_mb_delta": round(ram_after_filtre_mb - ram_before_mb, 2)},
        }

    parcelles_raw, final_radius_km, funnel = result

    # Scoring avec poids configurables
    scored = []
    for p in parcelles_raw:
        pts, score_details = _score_with_weights(p, opts_dto)
        scored.append({
            "idu": p.get("idu"),
            "code_insee": p.get("code_insee"),
            "section": p.get("section"),
            "numero": p.get("numero"),
            "surface_ha": round(float(p.get("surface_ha") or 0), 2),
            "miller": round(float(p.get("miller") or 0), 4),
            "distance_km": round((p.get("distance_centre_m") or 0) / 1000, 2),
            "dist_hydro_m": p.get("dist_surface_hydro_m"),
            "score": pts,
            "score_details": score_details,
        })

    scored.sort(key=lambda x: (-x["score"], x["distance_km"]))
    for i, p in enumerate(scored, 1):
        p["rank"] = i

    ram_after_scoring_mb = _get_process_memory_mb()
    logger.info("Filtre: RAM après scoring = %.2f Mo (delta total = %.2f Mo)", ram_after_scoring_mb, ram_after_scoring_mb - ram_before_mb)

    memory_info = {
        "ram_mb_before": ram_before_mb,
        "ram_mb_after_filtre": ram_after_filtre_mb,
        "ram_mb_after_scoring": ram_after_scoring_mb,
        "ram_mb_delta_total": round(ram_after_scoring_mb - ram_before_mb, 2),
    }

    # Persister les résultats dans le projet
    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE ecocompensation.projects
                SET last_filter  = :f,
                    last_results = :r,
                    status       = 'done',
                    updated_at   = now()
                WHERE id = :pid
            """),
            {
                "f": json.dumps(opts_dto.model_dump()),
                "r": json.dumps({
                    "final_radius_km": final_radius_km,
                    "total": len(scored),
                    "parcelles": scored,
                    "memory": memory_info,
                }),
                "pid": project_id,
            },
        )

    return {
        "total": len(scored),
        "final_radius_km": final_radius_km,
        "parcelles": scored,
        "funnel": funnel,
        "memory": memory_info,
    }


@app.get("/api/projects/{project_id}/results")
def get_results(project_id: str):
    proj = _get_project(project_id)
    return {
        "status": proj["status"],
        "last_filter": proj.get("last_filter"),
        "last_results": proj.get("last_results"),
        "layers_status": proj.get("layers_status", {}),
    }


@app.get("/api/projects/{project_id}/geojson")
def get_parcelles_geojson(project_id: str):
    """Retourne les parcelles du dernier filtre en GeoJSON (FeatureCollection)."""
    proj = _get_project(project_id)
    if not proj.get("last_results"):
        raise HTTPException(400, "Aucun résultat")

    aoi_id = str(proj["aoi_id"])
    results = proj["last_results"]
    if isinstance(results, str):
        results = json.loads(results)
    parcelles_data = results.get("parcelles", [])
    if not parcelles_data:
        raise HTTPException(400, "Aucune parcelle dans les résultats")

    idus = [p["idu"] for p in parcelles_data if p.get("idu")]
    scores = [int(p.get("score", 0)) for p in parcelles_data if p.get("idu")]
    min_score = min(scores) if scores else 0
    max_score = max(scores) if scores else 0
    score_range = max_score - min_score if max_score > min_score else 1

    def score_norm(score: int) -> float:
        return (score - min_score) / score_range

    idu_to_score = {p["idu"]: int(p.get("score", 0)) for p in parcelles_data if p.get("idu")}

    with engine.begin() as conn:
        rows = conn.execute(
            text("""
                SELECT
                    p.idu,
                    ST_AsGeoJSON(ST_Transform(p.geom_2154, 4326))::json AS geometry
                FROM ecocompensation_results.parcelles p
                WHERE p.aoi_id = :aid
                  AND p.idu = ANY(:idus)
            """),
            {"aid": aoi_id, "idus": idus},
        ).mappings().all()

    features = []
    for r in rows:
        score = idu_to_score.get(r["idu"], 0)
        norm = round(score_norm(score), 4)
        features.append({
            "type": "Feature",
            "geometry": dict(r["geometry"]),
            "properties": {
                "idu": r["idu"],
                "score": score,
                "score_norm": norm,
            },
        })

    return {"type": "FeatureCollection", "features": features}


@app.get("/api/projects/{project_id}/export/csv")
def export_csv(project_id: str, background_tasks: BackgroundTasks):
    """Exporte les parcelles classées en CSV."""
    import shutil
    import tempfile
    from fastapi.responses import FileResponse
    from export_classement_csv import export_classement_csv

    proj = _get_project(project_id)

    last_results = proj.get("last_results")
    if not last_results:
        raise HTTPException(400, "Aucun résultat à exporter")

    # Charger les données JSON
    if isinstance(last_results, str):
        last_results = json.loads(last_results)

    parcelles = last_results.get("parcelles", [])
    if not parcelles:
        raise HTTPException(400, "Aucune parcelle à exporter")

    # Créer un fichier CSV temporaire
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".csv", encoding="utf-8-sig") as nf:
        csv_path = Path(nf.name)
        export_classement_csv(parcelles, csv_path)

    background_tasks.add_task(os.remove, str(csv_path))
    return FileResponse(
        str(csv_path),
        filename=f"parcelles_{project_id[:8]}.csv",
        media_type="text/csv; charset=utf-8",
    )


@app.get("/api/projects/{project_id}/export/shp")
def export_shp(project_id: str, background_tasks: BackgroundTasks):
    """Exporte les parcelles classées en Shapefile (zip)."""
    import shutil
    import tempfile
    import zipfile
    from fastapi.responses import FileResponse
    from export_classement_shp import export_classement_shp

    proj = _get_project(project_id)
    aoi_id = str(proj["aoi_id"])

    last_results = proj.get("last_results")
    if not last_results:
        raise HTTPException(400, "Aucun résultat à exporter")

    last_filter = proj.get("last_filter")
    if not last_filter:
        raise HTTPException(400, "Options de filtre introuvables")

    # Charger les données JSON
    if isinstance(last_results, str):
        last_results = json.loads(last_results)
    if isinstance(last_filter, str):
        last_filter = json.loads(last_filter)

    parcelles = last_results.get("parcelles", [])
    if not parcelles:
        raise HTTPException(400, "Aucune parcelle à exporter")

    final_radius_km = last_results.get("final_radius_km", 0)

    # Reconstruire FiltreOptions depuis le DTO
    opts_dto = FiltreOptionsDTO(**last_filter)
    options = _dto_to_filtre_options(opts_dto)

    # Construire le SHP puis le zip dans un répertoire temporaire
    with tempfile.TemporaryDirectory() as tmpdir:
        shp_path = Path(tmpdir) / "parcelles.shp"
        export_classement_shp(engine, aoi_id, parcelles, options, final_radius_km, shp_path)

        zip_path = Path(tmpdir) / "parcelles.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            for ext in [".shp", ".shx", ".dbf", ".prj"]:
                f = shp_path.with_suffix(ext)
                if f.exists():
                    zf.write(f, f.name)

        # Copier le zip vers un fichier temporaire persistant : FileResponse lit le fichier
        # après le return, donc le répertoire temporaire serait déjà supprimé. On garde
        # une copie et on la supprime après envoi de la réponse.
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as nf:
            shutil.copy2(zip_path, nf.name)
            final_path = nf.name

    background_tasks.add_task(os.remove, final_path)
    return FileResponse(
        final_path,
        filename=f"parcelles_{project_id[:8]}.zip",
        media_type="application/zip",
    )


# ─────────────────────────────────────────────
# WebSocket — Suivi des fetches
# ─────────────────────────────────────────────

@app.websocket("/ws/projects/{project_id}/fetch-progress")
async def fetch_progress_ws(project_id: str, websocket: WebSocket):
    await ws_manager.connect(project_id, websocket)
    try:
        # Envoyer l'état actuel dès la connexion
        proj = _get_project(project_id)
        await websocket.send_json({
            "event": "connected",
            "status": proj["status"],
            "layers_status": proj.get("layers_status", {}),
        })
        # Garder la connexion ouverte
        while True:
            await asyncio.sleep(30)
            await websocket.send_json({"event": "ping"})
    except WebSocketDisconnect:
        ws_manager.disconnect(project_id, websocket)


# ─────────────────────────────────────────────
# Endpoint utilitaire — RAM du backend
# ─────────────────────────────────────────────

@app.get("/api/memory")
def get_memory():
    """RAM utilisée par le processus backend (pour vérifier que le filtre reste léger)."""
    ram_mb = _get_process_memory_mb()
    try:
        proc = psutil.Process(os.getpid())
        vms_mb = round(proc.memory_info().vms / (1024 * 1024), 2)
    except Exception:
        vms_mb = None
    return {"ram_mb": ram_mb, "vms_mb": vms_mb, "description": "RSS = mémoire physique utilisée, VMS = virtuelle"}


# ─────────────────────────────────────────────
# Endpoint utilitaire — liste des couches
# ─────────────────────────────────────────────

@app.get("/api/layers")
def list_layers():
    return [{"key": l["key"], "label": l["label"], "fast": l["fast"]} for l in LAYER_REGISTRY]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)