#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py
=======

FastAPI — routes uniquement. Zéro logique métier.
Toute la logique métier vit dans :
  - layers/filter_pipeline.py → pipeline filter_v2 (tiling + filtre SQL)
  - filter_orchestrator.py    → orchestration filter_v2 + UF
  - layers/layer_runner.py    → registre des clips AOI (ZH, UF)

Endpoints :
    POST   /api/projects                      créer un projet
    GET    /api/projects                      lister les projets
    GET    /api/projects/{id}                 détail d'un projet
    DELETE /api/projects/{id}                 supprimer projet + données AOI
    POST   /api/projects/{id}/filter-pipeline pipeline filter_v2 (CreateAoiPage)
    POST   /api/projects/{id}/fetch           clips AOI restants / UF (CLI)
    GET    /api/projects/{id}/results         derniers résultats
    GET    /api/projects/{id}/geojson         GeoJSON parcelles
    GET    /api/projects/{id}/export/csv      export CSV (?scope=parcelles|uf)
    GET    /api/projects/{id}/export/shp      export Shapefile (?scope=parcelles|uf)
    GET    /api/projects/{id}/export/rapport-pdf   rapport PDF classement parcelles
    GET    /api/memory                        RAM du processus backend
    GET    /api/layers                        liste des couches
    WS     /ws/projects/{id}/fetch-progress   suivi temps réel (fetch + filter_v2)
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import psutil
import geopandas as gpd
import pandas as pd
from dotenv import load_dotenv
from fastapi import (
    FastAPI, HTTPException, Request, UploadFile, File, Form, Query, Body,
    WebSocket, WebSocketDisconnect, BackgroundTasks,
)
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, text

# ── Modules métier ────────────────────────────────────────────────────────────
from orchestrator import run_orchestration, fetch_project_two_phases
from filter_orchestrator import FULL_PIPELINE_PHASES, run_filter_orchestration
from api.recherche_de_foncier.filtrage.layers.filter_pipeline import (
    FaunaCriterion,
    FilterConfig,
    FloraCriterion,
    ensure_columns,
)
from api.recherche_de_foncier.filtrage.layers.national_exclusions import DEFAULT_EXCLUDED_LAYERS
from api.recherche_de_foncier.filtrage.layers.layer_runner import LAYER_REGISTRY
from db import get_engine, get_engine_ppm
from api.project_kind import KIND_RECHERCHE, normalize_project_kind, project_kind_filter_values
from api.recherche_de_foncier.filtrage.layers.unites_foncieres.tables import ensure_tables as ensure_uf_tables
from api.recherche_de_foncier.filtrage.layers.rpg.rpg_ilots import api as ilots_rpg_api
from api.recherche_de_foncier.routers.foncier_router import router as foncier_router
from api.recherche_de_foncier.routers.pool_router import all_runs_router as pool_all_runs_router
from api.recherche_de_foncier.routers.pool_router import router as pool_router
from api.recherche_de_foncier.routers.results_geojson_router import router as results_geojson_router
from api.recherche_de_foncier.routers.durete_router import router as durete_router
from api.recherche_de_foncier.routers.autorisations_urba_router import router as autorisations_urba_router
from api.recherche_de_foncier.routers.cadastre_bbox_router import router as cadastre_bbox_router
from api.recherche_de_foncier.carte.cadastre_autour.cadastre_autour import router as cadastre_autour_router
from api.recherche_de_foncier.enrichir.pool.exports.router_exports import router as exports_router
from api.recherche_de_foncier.routers.rapport_router import router as rapport_router
from api.recherche_de_foncier.routers.fauna_router import router as fauna_router
from api.recherche_de_foncier.routers.hydro_preview import router as hydro_preview_router
from api.recherche_de_foncier.routers.flore_router import router as flore_router
from api.reserve_fonciere.ajout_de_foncier.flore.flore_import import router as flore_import_router
from api.reserve_fonciere.ajout_de_foncier.faune.faune_import import router as faune_import_router
from api.reserve_fonciere.router import router as data_interne_router
from api.reserve_fonciere.ajout_de_foncier.ajout_de_foncier_router import router as ajout_foncier_router
from api.recherche_de_foncier.enrichir.pool import pool_service

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────

load_dotenv(Path(__file__).parent / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

BUFFER_DEFAULT_M = 12_000
engine = get_engine()


def _parse_cors_origins() -> list[str]:
    """
    Lit CORS_ORIGINS depuis l'env (CSV) et fusionne avec les origines par défaut.
    Exemple:
      CORS_ORIGINS=https://ecocompensation-frontend.vercel.app,https://xxx.vercel.app
    """
    default_origins = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "https://ecocompensation-frontend.vercel.app",
        "https://ecocompensation-frontend-3nm5hbhou-matinducoins-projects.vercel.app",
    ]
    raw = os.getenv("CORS_ORIGINS", "")
    extra = [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]
    merged = [o.rstrip("/") for o in default_origins] + extra
    # Déduplication en conservant l'ordre
    return list(dict.fromkeys(merged))


def _cors_origin_regex() -> str:
    """
    Autorise toutes les origines Vercel du frontend ecocompensation.
    Exemples acceptés :
      - https://ecocompensation-frontend.vercel.app
      - https://ecocompensation-frontend-xxx.vercel.app
    """
    return r"^https://ecocompensation-frontend(?:[-a-zA-Z0-9.]*)?\.vercel\.app$"


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
    eng = get_engine()
    ensure_columns(eng)
    ensure_uf_tables(eng)
    yield


# ─────────────────────────────────────────────
# App
# ─────────────────────────────────────────────

app = FastAPI(title="KERELIA Ecocompensation API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_parse_cors_origins(),
    allow_origin_regex=_cors_origin_regex(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Rapport-Rss-Delta-Mb"],
)


def _cors_headers_for_request(request: Request) -> dict[str, str]:
    origin = (request.headers.get("origin") or "").rstrip("/")
    allowed = origin in _parse_cors_origins() or bool(
        re.fullmatch(_cors_origin_regex(), origin)
    )
    if not origin or not allowed:
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Credentials": "true",
        "Vary": "Origin",
    }


@app.exception_handler(HTTPException)
async def http_exception_cors_handler(request: Request, exc: HTTPException):
    """404/400 FastAPI avec CORS (évite un faux « blocked by CORS » dans le navigateur)."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers={**_cors_headers_for_request(request), **(exc.headers or {})},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """500 FastAPI avec CORS (le 502 Render, lui, reste hors app)."""
    if isinstance(exc, HTTPException):
        return await http_exception_cors_handler(request, exc)
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Erreur interne du serveur"},
        headers=_cors_headers_for_request(request),
    )

app.add_middleware(GZipMiddleware, minimum_size=1000)

_analyse_router_path = Path(__file__).resolve().parent / "api" / "analyse-foncier" / "router.py"
_analyse_spec = importlib.util.spec_from_file_location("analyse_foncier_router", _analyse_router_path)
_analyse_mod = importlib.util.module_from_spec(_analyse_spec)
assert _analyse_spec and _analyse_spec.loader
_analyse_spec.loader.exec_module(_analyse_mod)
analyse_foncier_router = _analyse_mod.router

app.include_router(analyse_foncier_router)
app.include_router(foncier_router, prefix="/api/foncier")
app.include_router(cadastre_autour_router, prefix="/api")
app.include_router(pool_router)
app.include_router(pool_all_runs_router)
app.include_router(results_geojson_router)
app.include_router(durete_router)
app.include_router(autorisations_urba_router)
app.include_router(cadastre_bbox_router)
app.include_router(exports_router)
app.include_router(rapport_router)
app.include_router(fauna_router, prefix="/api/fauna", tags=["fauna"])
app.include_router(hydro_preview_router, prefix="/api/hydro", tags=["hydro"])
app.include_router(flore_router, prefix="/api/flore", tags=["flore"])
app.include_router(flore_import_router, prefix="/api/reserve-fonciere/flore", tags=["flore-import"])
app.include_router(faune_import_router, prefix="/api/reserve-fonciere/faune", tags=["faune-import"])
app.include_router(data_interne_router, prefix="/api/data-interne", tags=["data-interne"])
app.include_router(ajout_foncier_router, prefix="/api/reserve-fonciere", tags=["ajout-foncier"])

# Monte le backend Identité Foncière depuis le package local backend/identite_fonciere.
# Le préfixe dédié évite de surcharger ce main.py avec ses routes métiers.
try:
    from identite_fonciere.main import app as identite_fonciere_app

    app.mount("/api/identite-fonciere", identite_fonciere_app)
    logger.info("✅ Identité Foncière montée sur /api/identite-fonciere")
except Exception as e:
    logger.warning("⚠️  Impossible de monter Identité Foncière : %s", e)


# ─────────────────────────────────────────────
# Schémas Pydantic
# ─────────────────────────────────────────────

class FromParcelleRequest(BaseModel):
    code_insee: str   = Field(..., min_length=4, max_length=5)
    section:    str   = Field(..., min_length=1, max_length=4)
    numero:     str   = Field(..., min_length=1, max_length=10)
    name:       str   = Field(..., min_length=1)
    buffer_km:  float = Field(default=5.0, ge=0.0, le=20.0)
    study_type: str   = Field(default="faune_buffer")


class ParcelleRefDTO(BaseModel):
    code_insee: str = Field(..., min_length=4, max_length=5)
    section: str = Field(..., min_length=1, max_length=4)
    numero: str = Field(..., min_length=1, max_length=10)


class FromParcellesRequest(BaseModel):
    parcelles: list[ParcelleRefDTO] = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    buffer_km: float = Field(default=5.0, ge=0.0, le=20.0)
    study_type: str = Field(default="faune_buffer")


class FetchRequest(BaseModel):
    """POST /api/projects/{id}/fetch — corps optionnel."""
    layers: list[str] | None = Field(
        default=None,
        description="Clés de couches à lancer (ordre = registre). None = toutes.",
    )
    fauna_species: list[str] | None = Field(
        default=None,
        description="Liste optionnelle de taxons à appliquer au fetch des couches faune.",
    )
    dry_run: bool = Field(
        default=False,
        description="Si True, suppression des lignes insérées après chaque couche (test).",
    )
    uf_max_parcelles: int | None = Field(
        default=None,
        ge=2,
        le=10,
        description="Cap optionnel de parcelles par unité foncière pour le calcul des sous-ensembles (2–10). None = pas de cap forcé par l'orchestrateur.",
    )
    uf_min_area_ha: float = Field(
        default=7.0,
        ge=1.0,
        description="Surface minimale (ha) d'une unité foncière conservée au pré-filtre.",
    )


class FaunaFilterCriterionDTO(BaseModel):
    species: str = Field(..., min_length=1)
    dist_m: float = Field(..., ge=0, le=50_000)
    aliases: list[str] = Field(default_factory=list)


class FloraFilterCriterionDTO(BaseModel):
    taxon_key: str = Field(..., min_length=1)
    label: str = Field(default="")
    dist_m: float = Field(..., ge=0, le=50_000)


class FilterPipelineRequest(BaseModel):
    """POST /api/projects/{id}/filter-pipeline — pipeline filter_v2."""
    min_area_ha: float = Field(default=7.0, ge=0.1, le=500)
    miller_thresh: float | None = Field(default=None, ge=0.0, le=1.0)
    cesbio_libelles: list[str] = Field(default_factory=list)
    fauna_criteria: list[FaunaFilterCriterionDTO] = Field(default_factory=list)
    fauna_combine: str = Field(
        default="or",
        description="Combinaison entre espèces : or = au moins une, and = toutes.",
    )
    fauna_joins: list[str] = Field(default_factory=list)
    flora_criteria: list[FloraFilterCriterionDTO] = Field(default_factory=list)
    flora_combine: str = Field(
        default="or",
        description="Combinaison entre taxons : or = au moins un, and = tous.",
    )
    flora_joins: list[str] = Field(default_factory=list)
    bio_combine: str = Field(
        default="or",
        description="Entre faune et flore : or = l'un ou l'autre, and = les deux.",
    )
    zone_humide_mode: str = Field(default="ignore")
    zones_humides_probables_mode: str = Field(default="ignore")
    min_zone_humide_ha: float = Field(
        default=0.0,
        ge=0.0,
        le=500.0,
        description="Surface minimale (ha) de zone humide établie intersectant la parcelle.",
    )
    excluded_layers: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDED_LAYERS),
        description="Couches nationales à exclure (geomce, preemption_ens, ens, natura_2000, znieff_1, znieff_2, ebc). znieff_1 est dans le défaut. ebc = couverture totale.",
    )
    troncons_hydros_max_dist_m: float | None = Field(
        default=None,
        ge=0,
        le=5000,
        description="Distance max (m) au tronçon hydro le plus proche ; null = critère ignoré.",
    )
    surfaces_hydros_max_dist_m: float | None = Field(
        default=None,
        ge=0,
        le=5000,
        description="Distance max (m) à une surface hydro ; null = critère ignoré.",
    )
    same_bassin_versant: bool = Field(
        default=False,
        description="Ne garder que les parcelles des bassins versants qui intersectent le foncier du projet.",
    )
    arrachage_vignes: bool = Field(
        default=False,
        description="Ne garder que les parcelles qui intersectent ecocompensation.arrachage_vignes.",
    )
    arrachage_vignes_types: list[str] = Field(
        default_factory=list,
        description="Valeurs de c_div_re_1. Liste vide = tous les types.",
    )
    include_unites_foncieres: bool = Field(
        default=True,
        description="Calcule aussi les unités foncières après le filtre parcelles.",
    )
    include_ilots_rpg: bool = Field(
        default=False,
        description="Calcule aussi les îlots agricoles RPG après le filtre parcelles.",
    )


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _get_process_memory_mb() -> float:
    try:
        return round(psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024), 2)
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
    return _project_row_out(row)


# ─────────────────────────────────────────────
# Routes — Projets
# ─────────────────────────────────────────────

def _project_row_out(row) -> dict:
    d = dict(row)
    d["study_type"] = normalize_project_kind(d.get("study_type"))
    return d


@app.get("/api/projects")
def list_projects(study_type: str | None = None):
    sql = """
        SELECT id, name, status, layers_status, created_at, updated_at,
               COALESCE(study_type, 'recherche') AS study_type
        FROM ecocompensation.projects
    """
    params: dict = {}
    kinds = project_kind_filter_values(study_type)
    if kinds:
        sql += " WHERE COALESCE(study_type, 'recherche') IN :kinds"
        params["kinds"] = kinds
    sql += " ORDER BY created_at DESC"
    stmt = text(sql)
    if kinds:
        stmt = stmt.bindparams(bindparam("kinds", expanding=True))
    with engine.begin() as conn:
        rows = conn.execute(stmt, params).mappings().all()
    return [_project_row_out(r) for r in rows]


@app.get("/api/projects/history")
def list_projects_history(study_type: str | None = None):
    study_filter = ""
    params: dict = {}
    kinds = project_kind_filter_values(study_type)
    if kinds:
        study_filter = " WHERE COALESCE(p.study_type, 'recherche') IN :kinds"
        params["kinds"] = kinds

    with engine.begin() as conn:
        has_pool_runs = bool(
            conn.execute(
                text("SELECT to_regclass(:tbl) IS NOT NULL"),
                {"tbl": "ecocompensation_results.parcelles_pool_runs"},
            ).scalar()
        )

        base_select = """
                    SELECT
                        p.id,
                        p.name,
                        p.status,
                        p.layers_status,
                        p.created_at,
                        p.updated_at,
                        p.last_filter,
                        p.filter_config,
                        COALESCE(p.study_type, 'recherche') AS study_type,
                        a.buffer_m,
                        f.area_ha AS foncier_area_ha,
                        {pool_col}
                    FROM ecocompensation.projects p
                    LEFT JOIN ecocompensation.aoi a
                        ON a.id = p.aoi_id
                    LEFT JOIN ecocompensation.foncier f
                        ON f.id = p.foncier_id
                    {pool_join}
                    {study_filter}
                    ORDER BY p.created_at DESC
        """

        if has_pool_runs:
            sql = base_select.format(
                pool_col="pr.total_count AS pool_total_count",
                pool_join="""
                    LEFT JOIN LATERAL (
                        SELECT total_count
                        FROM ecocompensation_results.parcelles_pool_runs
                        WHERE project_id = p.id
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) pr ON TRUE
                """,
                study_filter=study_filter,
            )
        else:
            sql = base_select.format(
                pool_col="NULL::integer AS pool_total_count",
                pool_join="",
                study_filter=study_filter,
            )

        hist_stmt = text(sql)
        if kinds:
            hist_stmt = hist_stmt.bindparams(bindparam("kinds", expanding=True))
        rows = conn.execute(hist_stmt, params).mappings().all()

    out: list[dict] = []
    for r in rows:
        last_filter = r.get("last_filter")
        if isinstance(last_filter, str):
            try:
                last_filter = json.loads(last_filter)
            except Exception:
                last_filter = None
        if not isinstance(last_filter, dict):
            last_filter = None

        filter_config = r.get("filter_config")
        if isinstance(filter_config, str):
            try:
                filter_config = json.loads(filter_config)
            except Exception:
                filter_config = None
        if not isinstance(filter_config, dict):
            filter_config = None

        buffer_m = r.get("buffer_m")
        buffer_km = (float(buffer_m) / 1000.0) if buffer_m is not None else None
        out.append(
            {
                "id": str(r["id"]),
                "name": r.get("name"),
                "status": r.get("status"),
                "study_type": normalize_project_kind(r.get("study_type")),
                "layers_status": r.get("layers_status") or {},
                "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
                "updated_at": r["updated_at"].isoformat() if r.get("updated_at") else None,
                "filter_config": filter_config,
                "history": {
                    "buffer_km": buffer_km,
                    "foncier_area_ha": float(r["foncier_area_ha"]) if r.get("foncier_area_ha") is not None else None,
                    "pool_total_count": int(r["pool_total_count"]) if r.get("pool_total_count") is not None else None,
                    "last_filter": last_filter,
                },
            }
        )
    return out


@app.get("/api/projects/history-map")
def list_projects_history_map():
    """Centroïdes + contours des géométries projet (parcelle source, sinon foncier)."""
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT
                    p.id,
                    p.name,
                    ST_AsGeoJSON(ST_Transform(
                        ST_PointOnSurface(COALESCE(src.geom, f.geom_2154, a.geom_2154)),
                        4326
                    ))::json AS point_geom,
                    ST_AsGeoJSON(ST_Transform(
                        ST_SimplifyPreserveTopology(COALESCE(src.geom, f.geom_2154), 15),
                        4326
                    ))::json AS outline_geom
                FROM ecocompensation.projects p
                LEFT JOIN LATERAL (
                    SELECT ST_Union(geom_2154) AS geom
                    FROM ecocompensation.project_parcelles
                    WHERE project_id = p.id
                    HAVING COUNT(*) > 0
                ) src ON TRUE
                LEFT JOIN ecocompensation.aoi a ON a.id = p.aoi_id
                LEFT JOIN ecocompensation.foncier f ON f.id = p.foncier_id
                WHERE COALESCE(src.geom, f.geom_2154, a.geom_2154) IS NOT NULL
                """
            )
        ).mappings().all()

    points = []
    outlines = []
    for r in rows:
        pid = str(r["id"])
        name = r.get("name") or pid[:8]
        if r.get("point_geom"):
            points.append(
                {
                    "type": "Feature",
                    "geometry": dict(r["point_geom"]),
                    "properties": {"project_id": pid, "name": name},
                }
            )
        if r.get("outline_geom"):
            outlines.append(
                {
                    "type": "Feature",
                    "geometry": dict(r["outline_geom"]),
                    "properties": {"project_id": pid, "name": name},
                }
            )
    return {
        "points": {"type": "FeatureCollection", "features": points},
        "outlines": {"type": "FeatureCollection", "features": outlines},
    }


@app.post("/api/projects", status_code=201)
async def create_project(
    name:       str           = Form(...),
    buffer_m:   int           = Form(BUFFER_DEFAULT_M),
    gpkg_file:  UploadFile | None = File(None),
    code_insee: str | None    = Form(None),
):
    if not gpkg_file and not code_insee:
        raise HTTPException(400, "Fournir soit un fichier GPKG soit un code_insee")

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
        import requests as req
        resp = req.get(
            "https://data.geopf.fr/wfs/ows",
            params={"service": "WFS", "version": "2.0.0", "request": "GetFeature",
                    "typeNames": "BDTOPO_V3:commune", "outputFormat": "application/json",
                    "CQL_FILTER": f"code_insee='{code_insee}'"},
            timeout=30,
        )
        gdf = gpd.read_file(resp.text)
        if gdf.empty:
            raise HTTPException(404, f"Commune {code_insee} introuvable")
        if gdf.crs is None or gdf.crs.to_string() != "EPSG:2154":
            gdf = gdf.to_crs("EPSG:2154")
        code = code_insee

    aoi_geom = gdf.union_all().buffer(buffer_m, resolution=32)

    with engine.begin() as conn:
        project_id = str(uuid.uuid4())
        aoi_id = str(conn.execute(
            text(
                """
                INSERT INTO ecocompensation.aoi (id, code_insee, buffer_m, geom_2154, project_id)
                VALUES (:project_id, :code, :buf, ST_GeomFromText(:wkt, 2154), :project_id)
                RETURNING id
                """
            ),
            {"project_id": project_id, "code": code, "buf": buffer_m, "wkt": aoi_geom.wkt},
        ).scalar_one())
        proj = conn.execute(
            text(
                """
                INSERT INTO ecocompensation.projects (id, name, aoi_id, status, study_type)
                VALUES (:project_id, :name, :aoi_id, 'created', :study_type)
                RETURNING id, name, status, created_at
                """
            ),
            {
                "project_id": project_id,
                "name": name,
                "aoi_id": aoi_id,
                "study_type": KIND_RECHERCHE,
            },
        ).mappings().one()

    return {"id": str(proj["id"]), "name": proj["name"], "status": proj["status"],
            "aoi_id": aoi_id, "created_at": proj["created_at"].isoformat()}


def _load_parcelle_wfs(code_insee: str, section: str, numero: str) -> gpd.GeoDataFrame:
    import requests as req
    resp = req.get(
        "https://data.geopf.fr/wfs/ows",
        params={"service": "WFS", "version": "2.0.0", "request": "GetFeature",
                "typeNames": "CADASTRALPARCELS.PARCELLAIRE_EXPRESS:parcelle",
                "srsName": "EPSG:2154", "outputFormat": "application/json",
                "CQL_FILTER": f"code_insee='{code_insee}' AND section='{section}' AND numero='{numero}'"},
        timeout=30,
    )
    resp.raise_for_status()
    gdf = gpd.read_file(resp.text)
    if gdf.empty:
        raise HTTPException(404, f"Parcelle {code_insee}/{section}/{numero} introuvable")
    if gdf.crs is None or gdf.crs.to_string() != "EPSG:2154":
        gdf = gdf.to_crs("EPSG:2154")
    return gdf


def _create_project_from_union_geometry(
    *,
    project_name: str,
    buffer_km: float,
    parcelles_refs: list[dict[str, str]],
    union_geom,
    study_type: str = "faune_buffer",
):
    if study_type not in ("faune_buffer", "zones_humides_intra"):
        raise HTTPException(400, "study_type invalide")
    if study_type == "zones_humides_intra":
        buffer_m = 0
        aoi_geom_wkt = union_geom.wkt
    else:
        buffer_m = int(buffer_km * 1000)
        aoi_geom_wkt = union_geom.buffer(buffer_m, resolution=32).wkt
    area_ha = float(union_geom.area / 10_000.0)
    with engine.begin() as conn:
        project_id = str(uuid.uuid4())
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS ecocompensation.foncier (
                id uuid NOT NULL DEFAULT gen_random_uuid(), name text NOT NULL,
                geom_2154 geometry NOT NULL, area_ha numeric NULL,
                created_at timestamptz NULL DEFAULT now(), PRIMARY KEY (id)
            )
        """))
        foncier_id = str(conn.execute(
            text("INSERT INTO ecocompensation.foncier (name, geom_2154, area_ha) VALUES (:name, ST_Multi(ST_GeomFromText(:wkt, 2154)), :area_ha) RETURNING id"),
            {"name": project_name, "wkt": union_geom.wkt, "area_ha": area_ha},
        ).scalar_one())
        aoi_id = str(conn.execute(
            text(
                """
                INSERT INTO ecocompensation.aoi (id, code_insee, buffer_m, geom_2154, project_id)
                VALUES (:project_id, :code, :buf, ST_GeomFromText(:wkt, 2154), :project_id)
                RETURNING id
                """
            ),
            {
                "project_id": project_id,
                "code": parcelles_refs[0]["code_insee"],
                "buf": buffer_m,
                "wkt": aoi_geom_wkt,
            },
        ).scalar_one())
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS ecocompensation.project_parcelles (
                    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                    project_id uuid NOT NULL,
                    code_insee text NOT NULL,
                    section text NOT NULL,
                    numero text NOT NULL,
                    geom_2154 geometry(Geometry, 2154) NOT NULL,
                    created_at timestamptz DEFAULT now()
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_project_parcelles_project_id
                ON ecocompensation.project_parcelles(project_id)
                """
            )
        )
        for ref in parcelles_refs:
            gdf_one = _load_parcelle_wfs(ref["code_insee"], ref["section"], ref["numero"])
            geom_one = gdf_one.union_all()
            conn.execute(
                text(
                    """
                    INSERT INTO ecocompensation.project_parcelles
                        (project_id, code_insee, section, numero, geom_2154)
                    VALUES
                        (:project_id, :code_insee, :section, :numero, ST_Multi(ST_GeomFromText(:wkt, 2154)))
                    """
                ),
                {
                    "project_id": project_id,
                    "code_insee": ref["code_insee"],
                    "section": ref["section"],
                    "numero": ref["numero"],
                    "wkt": geom_one.wkt,
                },
            )
        proj = conn.execute(
            text(
                """
                INSERT INTO ecocompensation.projects (id, name, aoi_id, foncier_id, status, study_type)
                VALUES (:project_id, :name, :aoi_id, :foncier_id, 'created', :study_type)
                RETURNING id, name, status, created_at
                """
            ),
            {
                "project_id": project_id,
                "name": project_name,
                "aoi_id": aoi_id,
                "foncier_id": foncier_id,
                "study_type": KIND_RECHERCHE,
            },
        ).mappings().one()
    return {
        "id": str(proj["id"]),
        "name": proj["name"],
        "status": proj["status"],
        "project_id": str(proj["id"]),
        "aoi_id": aoi_id,
        "foncier_id": foncier_id,
        "created_at": proj["created_at"].isoformat(),
    }


@app.post("/api/projects/from-parcelle", status_code=201)
def create_project_from_parcelle(body: FromParcelleRequest):
    gdf = _load_parcelle_wfs(body.code_insee, body.section, body.numero)
    union_geom = gdf.union_all()
    return _create_project_from_union_geometry(
        project_name=body.name,
        buffer_km=body.buffer_km,
        parcelles_refs=[{
            "code_insee": body.code_insee.strip(),
            "section": body.section.strip().upper(),
            "numero": body.numero.strip(),
        }],
        union_geom=union_geom,
        study_type=body.study_type,
    )


@app.post("/api/projects/from-parcelles", status_code=201)
def create_project_from_parcelles(body: FromParcellesRequest):
    refs = [
        {
            "code_insee": p.code_insee.strip(),
            "section": p.section.strip().upper(),
            "numero": p.numero.strip(),
        }
        for p in body.parcelles
    ]
    uniq_refs: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for ref in refs:
        key = (ref["code_insee"], ref["section"], ref["numero"])
        if key in seen:
            continue
        seen.add(key)
        uniq_refs.append(ref)
    if not uniq_refs:
        raise HTTPException(400, "Aucune parcelle valide fournie.")
    frames = [_load_parcelle_wfs(r["code_insee"], r["section"], r["numero"]) for r in uniq_refs]
    gdf = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=frames[0].crs)
    union_geom = gdf.union_all()
    return _create_project_from_union_geometry(
        project_name=body.name,
        buffer_km=body.buffer_km,
        parcelles_refs=uniq_refs,
        union_geom=union_geom,
        study_type=body.study_type,
    )


@app.get("/api/projects/{project_id}/context-geometry")
def get_project_context_geometry(project_id: str):
    _get_project(project_id)

    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                SELECT
                    p.id AS project_id,
                    p.name,
                    p.aoi_id,
                    p.foncier_id,
                    pp.code_insee,
                    pp.section,
                    pp.numero,
                    ST_AsGeoJSON(ST_Transform(pp.geom_2154, 4326))::json AS parcelle_geometry,
                    ST_AsGeoJSON(ST_Transform(a.geom_2154, 4326))::json AS aoi_geometry,
                    ST_AsGeoJSON(ST_Transform(f.geom_2154, 4326))::json AS foncier_geometry
                FROM ecocompensation.projects p
                LEFT JOIN ecocompensation.project_parcelles pp ON pp.project_id = p.id
                LEFT JOIN ecocompensation.aoi a ON a.id = p.aoi_id
                LEFT JOIN ecocompensation.foncier f ON f.id = p.foncier_id
                WHERE p.id = :pid
                LIMIT 1
                """
            ),
            {"pid": project_id},
        ).mappings().one_or_none()

    if not row:
        raise HTTPException(404, f"Projet {project_id} introuvable")

    parcelle_feature = None
    if row.get("parcelle_geometry"):
        parcelle_feature = {
            "type": "Feature",
            "geometry": dict(row["parcelle_geometry"]),
            "properties": {
                "project_id": str(row["project_id"]),
                "code_insee": row.get("code_insee"),
                "section": row.get("section"),
                "numero": row.get("numero"),
            },
        }

    aoi_feature = None
    if row.get("aoi_geometry"):
        aoi_feature = {
            "type": "Feature",
            "geometry": dict(row["aoi_geometry"]),
            "properties": {
                "project_id": str(row["project_id"]),
                "aoi_id": str(row["aoi_id"]) if row.get("aoi_id") else None,
            },
        }

    foncier_feature = None
    if row.get("foncier_geometry"):
        foncier_feature = {
            "type": "Feature",
            "geometry": dict(row["foncier_geometry"]),
            "properties": {
                "project_id": str(row["project_id"]),
                "foncier_id": str(row["foncier_id"]) if row.get("foncier_id") else None,
            },
        }

    return {
        "project_id": str(row["project_id"]),
        "name": row.get("name"),
        "parcelle_source": parcelle_feature,
        "aoi": aoi_feature,
        "foncier": foncier_feature,
    }


@app.get("/api/projects/{project_id}")
def get_project(project_id: str):
    return _get_project(project_id)


@app.delete("/api/projects/{project_id}", status_code=204)
def delete_project(project_id: str):
    proj = _get_project(project_id)
    RESULT_TABLES = [
        "ecocompensation_results.parcelles", "ecocompensation_results.ebc",
        "ecocompensation_results.mesures_compensatoire_surf", "ecocompensation_results.mesures_compensatoire_lin",
        "ecocompensation_results.mesures_compensatoire_pct", "ecocompensation_results.mesures_compensatoire_commune",
        "ecocompensation_results.zone_de_vegetation",
        "ecocompensation_results.zone_humide",
        "ecocompensation_results.remontee_de_nappes",
        "ecocompensation_results.troncons_hydro",
        "ecocompensation_results.surfaces_hydro", "ecocompensation_results.surfaces_elementaires",
        "ecocompensation_results.routes", "ecocompensation_results.voies_ferrees",
        "ecocompensation_results.fragmentation_polygons", "ecocompensation_results.zones_humides_probables",
        "ecocompensation_results.znieff",
        "ecocompensation_results.reserves_naturelles",
        "ecocompensation_results.sites_classes",
        "ecocompensation_results.natura2000",
        "ecocompensation_results.prairies_sensibles",
        "ecocompensation_results.arrachage_vignes",
        "ecocompensation_results.fauna",
        # Compat anciens schémas (si déjà calculés)
        "ecocompensation_results.faune_pct",
        "ecocompensation_results.faune_lin",
        "ecocompensation_results.faune_surf",
        "ecocompensation_results.unites_foncieres",
        "ecocompensation_results.sous_ensembles",
        "ecocompensation_results.uf_parcelles",
        "ecocompensation_results.uf_terrains",
        "ecocompensation_results.parcelles_pool_indesirables",
        "ecocompensation_results.parcelles_project_indesirables",
        "ecocompensation_results.parcelles_pool_metrics",
        "ecocompensation_results.parcelles_pool",
        "ecocompensation_results.parcelles_pool_runs",
    ]
    for t in RESULT_TABLES:
        try:
            with engine.begin() as conn:
                conn.execute(text(f"DELETE FROM {t} WHERE project_id = :pid"), {"pid": project_id})
        except Exception:
            pass
    for table, col, val in [
        ("ecocompensation.project_parcelles", "project_id", project_id),
        ("ecocompensation.aoi",      "id",  proj.get("aoi_id")),
        ("ecocompensation.projects", "id",  project_id),
        ("ecocompensation.foncier",  "id",  proj.get("foncier_id")),
    ]:
        if val:
            try:
                with engine.begin() as conn:
                    conn.execute(text(f"DELETE FROM {table} WHERE {col} = :v"), {"v": str(val)})
            except Exception:
                pass


# ─────────────────────────────────────────────
# Route — Fetch
# ─────────────────────────────────────────────

_running_fetches: set[str] = set()
_running_filters: set[str] = set()


@app.post("/api/projects/{project_id}/filter-pipeline")
async def start_filter_pipeline(
    project_id: str,
    background_tasks: BackgroundTasks,
    body: FilterPipelineRequest,
):
    """
    Lance le pipeline de filtrage écologique (tiling → filtre → enrich).
    Les parcelles retenues restent dans ecocompensation_results.parcelles.
    Progression via WS /ws/projects/{id}/fetch-progress.
    """
    proj = _get_project(project_id)
    if proj["status"] in ("fetching", "filtering") or project_id in _running_filters:
        raise HTTPException(409, "Filtrage déjà en cours")

    study_type = str(proj.get("study_type") or KIND_RECHERCHE)
    search_mode = "exclude_source_buffer"

    zh_mode = body.zone_humide_mode if body.zone_humide_mode in ("ignore", "intersect", "exclude") else "ignore"
    zhp_mode = (
        body.zones_humides_probables_mode
        if body.zones_humides_probables_mode in ("ignore", "intersect", "exclude")
        else "ignore"
    )

    aoi_id = str(proj["aoi_id"])
    config = FilterConfig(
        min_area_ha=body.min_area_ha,
        miller_thresh=body.miller_thresh,
        cesbio_libelles=[x.strip() for x in body.cesbio_libelles if x.strip()],
        fauna_criteria=[
            FaunaCriterion(
                species=fc.species.strip(),
                dist_m=fc.dist_m,
                aliases=[a.strip() for a in fc.aliases if a.strip() and a.strip() != fc.species.strip()],
            )
            for fc in body.fauna_criteria
            if fc.species.strip()
        ],
        fauna_combine="and" if str(body.fauna_combine).lower() == "and" else "or",
        fauna_joins=[j for j in body.fauna_joins if str(j).lower() in ("and", "or")],
        flora_criteria=[
            FloraCriterion(
                taxon_key=fc.taxon_key.strip(),
                label=(fc.label or fc.taxon_key).strip(),
                dist_m=fc.dist_m,
            )
            for fc in body.flora_criteria
            if fc.taxon_key.strip()
        ],
        flora_combine="and" if str(body.flora_combine).lower() == "and" else "or",
        flora_joins=[j for j in body.flora_joins if str(j).lower() in ("and", "or")],
        bio_combine="and" if str(body.bio_combine).lower() == "and" else "or",
        search_mode=search_mode,
        zone_humide_mode=zh_mode,
        zones_humides_probables_mode=zhp_mode,
        min_zone_humide_ha=body.min_zone_humide_ha,
        excluded_layers=[
            x.strip()
            for x in body.excluded_layers
            if x and str(x).strip()
        ] or list(DEFAULT_EXCLUDED_LAYERS),
        troncons_hydros_max_dist_m=body.troncons_hydros_max_dist_m,
        surfaces_hydros_max_dist_m=body.surfaces_hydros_max_dist_m,
        same_bassin_versant=body.same_bassin_versant,
        arrachage_vignes=body.arrachage_vignes,
        arrachage_vignes_types=[x.strip() for x in body.arrachage_vignes_types if x and str(x).strip()],
        include_unites_foncieres=body.include_unites_foncieres,
        include_ilots_rpg=body.include_ilots_rpg,
    )
    _running_filters.add(project_id)

    async def _run():
        try:
            await run_filter_orchestration(
                engine,
                project_id,
                aoi_id,
                config,
                lambda d: ws_manager.broadcast(project_id, d),
            )
        finally:
            _running_filters.discard(project_id)

    background_tasks.add_task(_run)
    return {"status": "started", "project_id": project_id}


@app.post("/api/projects/{project_id}/filter-uf")
async def start_uf_only(project_id: str):
    """Relance uniquement les unités foncières avec les paramètres du dernier filtre."""
    proj = _get_project(project_id)
    if proj["status"] in ("fetching", "filtering") or project_id in _running_filters:
        raise HTTPException(409, "Un calcul est déjà en cours pour ce projet")
    raw = proj.get("last_filter") or proj.get("filter_config") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    if not isinstance(raw, dict) or not raw:
        raise HTTPException(400, "Aucun paramètre de filtre enregistré pour ce projet")

    fauna = []
    for fc in raw.get("fauna_criteria") or []:
        if not isinstance(fc, dict):
            continue
        species = str(fc.get("species") or "").strip()
        if not species:
            continue
        dist = fc.get("dist_m")
        aliases = [str(a).strip() for a in (fc.get("aliases") or []) if str(a).strip() and str(a).strip() != species]
        fauna.append(FaunaCriterion(
            species=species,
            dist_m=float(dist) if dist is not None else 1000.0,
            aliases=aliases,
        ))

    flora = []
    for fc in raw.get("flora_criteria") or []:
        if not isinstance(fc, dict):
            continue
        key = str(fc.get("taxon_key") or "").strip()
        if not key:
            continue
        dist = fc.get("dist_m")
        flora.append(FloraCriterion(
            taxon_key=key,
            label=str(fc.get("label") or key).strip(),
            dist_m=float(dist) if dist is not None else 200.0,
        ))

    miller = raw.get("miller_thresh")
    config = FilterConfig(
        min_area_ha=float(raw.get("min_area_ha") or 7),
        miller_thresh=None if miller is None else float(miller),
        cesbio_libelles=[str(x).strip() for x in (raw.get("cesbio_libelles") or []) if str(x).strip()],
        fauna_criteria=fauna,
        fauna_combine="and" if str(raw.get("fauna_combine") or "or").lower() == "and" else "or",
        fauna_joins=[str(j).lower() for j in (raw.get("fauna_joins") or []) if str(j).lower() in ("and", "or")],
        flora_criteria=flora,
        flora_combine="and" if str(raw.get("flora_combine") or "or").lower() == "and" else "or",
        flora_joins=[str(j).lower() for j in (raw.get("flora_joins") or []) if str(j).lower() in ("and", "or")],
        bio_combine="and" if str(raw.get("bio_combine") or "or").lower() == "and" else "or",
        search_mode=str(raw.get("search_mode") or "exclude_source_buffer"),
        zone_humide_mode=str(raw.get("zone_humide_mode") or "ignore"),
        zones_humides_probables_mode=str(raw.get("zones_humides_probables_mode") or "ignore"),
        min_zone_humide_ha=float(raw.get("min_zone_humide_ha") or 0),
        excluded_layers=[str(x).strip() for x in (raw.get("excluded_layers") or []) if str(x).strip()]
        or list(DEFAULT_EXCLUDED_LAYERS),
        troncons_hydros_max_dist_m=raw.get("troncons_hydros_max_dist_m"),
        surfaces_hydros_max_dist_m=raw.get("surfaces_hydros_max_dist_m"),
        same_bassin_versant=bool(raw.get("same_bassin_versant")),
        arrachage_vignes=bool(raw.get("arrachage_vignes")),
        arrachage_vignes_types=[str(x).strip() for x in (raw.get("arrachage_vignes_types") or []) if str(x).strip()],
        include_unites_foncieres=True,
        include_ilots_rpg=bool(raw.get("include_ilots_rpg")),
    )
    aoi_id = str(proj["aoi_id"])
    _running_filters.add(project_id)

    async def _run():
        try:
            from api.recherche_de_foncier.filtrage.layers.unites_foncieres.runner import run_uf_after_filter
            await run_uf_after_filter(
                engine,
                project_id,
                aoi_id,
                config,
                lambda d: ws_manager.broadcast(project_id, d),
                engine_ppm=get_engine_ppm(),
            )
        finally:
            _running_filters.discard(project_id)

    asyncio.create_task(_run())
    return {"status": "started", "project_id": project_id}


@app.post("/api/projects/{project_id}/fetch")
async def start_fetch(
    project_id: str,
    background_tasks: BackgroundTasks,
    body: FetchRequest | None = None,
):
    proj = _get_project(project_id)
    if proj["status"] == "fetching" or project_id in _running_fetches:
        raise HTTPException(409, "Fetch déjà en cours")
    aoi_id = str(proj["aoi_id"])
    req = body or FetchRequest()
    if req.layers is not None and len(req.layers) == 0:
        raise HTTPException(400, "La liste « layers » ne peut pas être vide")
    _running_fetches.add(project_id)

    async def _run():
        try:
            if req.layers is None:
                await fetch_project_two_phases(
                    engine,
                    project_id,
                    aoi_id,
                    lambda d: ws_manager.broadcast(project_id, d),
                    dry_run=req.dry_run,
                    fauna_species=req.fauna_species,
                )
            else:
                await run_orchestration(
                    engine,
                    project_id,
                    aoi_id,
                    lambda d: ws_manager.broadcast(project_id, d),
                    layer_keys=req.layers,
                    dry_run=req.dry_run,
                    fauna_species=req.fauna_species,
                )
        finally:
            _running_fetches.discard(project_id)

    background_tasks.add_task(_run)
    return {
        "status": "started",
        "project_id": project_id,
        "layers": req.layers,
        "dry_run": req.dry_run,
    }


# ─────────────────────────────────────────────
# Prérequis filtre UF : sous-ensembles générés en amont
# ─────────────────────────────────────────────

@app.get("/api/projects/{project_id}/sous-ensembles-status")
def sous_ensembles_status(project_id: str):
    """
    True s'il existe au moins un terrain retenu (ou manuel) pour ce projet.
    Le nom de la route et le champ `has_sous_ensembles` restent ceux du front.
    """
    _get_project(project_id)
    with engine.begin() as conn:
        exists = conn.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM ecocompensation_results.uf_terrains
                    WHERE project_id = CAST(:pid AS uuid)
                      AND (verdict OR source = 'manuel')
                )
                """
            ),
            {"pid": project_id},
        ).scalar_one()
    return {"has_sous_ensembles": bool(exists)}


@app.get("/api/projects/{project_id}/uf-pool")
def get_uf_pool(
    project_id: str,
    cesbio_libelles: list[str] = Query(
        default=["Forêts de conifères", "Forêts de feuillus"],
    ),
    fauna_species: str = Query(default=""),
    fauna_dist_m: float = Query(default=1000.0, ge=0),
    miller_thresh: float = Query(default=0.39, ge=0, le=1),
    study_type: str = Query(default=""),
    min_zone_humide_ha: float | None = Query(default=None, ge=0),
):
    """
    Dernier JSON d'unités foncières enregistré par le calcul (last_results_uf).
    Les paramètres de requête restent acceptés pour le front actuel.
    """
    proj = _get_project(project_id)
    del cesbio_libelles, fauna_species, fauna_dist_m, miller_thresh, study_type, min_zone_humide_ha
    raw = proj.get("last_results_uf")
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        return {"version": 2, "total_uf": 0, "unites_foncieres": [], "diagnostic": {}}
    return raw


@app.post("/api/projects/{project_id}/uf/recompute-durete", status_code=202)
def recompute_uf_durete(project_id: str, body: dict | None = Body(default=None)):
    """Dureté foncière légère des unités foncières : un score par SIREN."""
    _get_project(project_id)
    from api.recherche_de_foncier.filtrage.layers.unites_foncieres.durete_legere import start_job

    raw = (body or {}).get("uf_ids") or []
    uf_ids = [str(x).strip() for x in raw if str(x).strip()] or None
    job_id = start_job(engine, project_id, uf_ids)
    return {"status": "accepted", "job_id": job_id, "project_id": project_id}


@app.get("/api/projects/{project_id}/uf/recompute-durete/jobs/{job_id}")
def recompute_uf_durete_job(project_id: str, job_id: str):
    from api.recherche_de_foncier.filtrage.layers.unites_foncieres.durete_legere import get_job

    job = get_job(job_id)
    if not job or job.get("project_id") != project_id:
        raise HTTPException(404, "Job dureté unités foncières introuvable")
    return job


@app.post("/api/projects/{project_id}/uf-manuelle")
def post_uf_manuelle(project_id: str, body: dict = Body(...)):
    """Regroupe des parcelles contiguës du cadastre en une unité foncière du pool UF."""
    from api.recherche_de_foncier.filtrage.layers.unites_foncieres.manuel import AjoutUfError, add_manual_uf

    _get_project(project_id)
    idus = body.get("idus") if isinstance(body, dict) else None
    if not isinstance(idus, list):
        raise HTTPException(400, "Fournir la liste idus.")
    try:
        return add_manual_uf(engine, project_id, [str(i) for i in idus])
    except AjoutUfError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.put("/api/projects/{project_id}/uf/{uf_id}/parcelles")
def put_uf_manuelle_parcelles(project_id: str, uf_id: str, body: dict = Body(...)):
    """Recompose une UF assemblée à la main et recalcule ses métriques."""
    from api.recherche_de_foncier.filtrage.layers.unites_foncieres.manuel import AjoutUfError, replace_manual_uf

    _get_project(project_id)
    idus = body.get("idus") if isinstance(body, dict) else None
    if not isinstance(idus, list):
        raise HTTPException(400, "Fournir la liste idus.")
    try:
        return replace_manual_uf(engine, project_id, uf_id, [str(i) for i in idus])
    except AjoutUfError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/projects/{project_id}/uf/{uf_id}")
def delete_unite_fonciere(project_id: str, uf_id: str):
    from api.recherche_de_foncier.filtrage.layers.unites_foncieres.manuel import AjoutUfError, delete_uf

    _get_project(project_id)
    try:
        return delete_uf(engine, project_id, uf_id)
    except AjoutUfError as exc:
        raise HTTPException(404, str(exc)) from exc


# ─────────────────────────────────────────────
# Routes — Résultats & exports
# ─────────────────────────────────────────────

@app.get("/api/projects/{project_id}/results")
def get_results(project_id: str):
    proj = _get_project(project_id)
    buffer_km = None
    aoi_id = proj.get("aoi_id")
    if aoi_id:
        with engine.connect() as conn:
            buf_m = conn.execute(
                text("SELECT buffer_m FROM ecocompensation.aoi WHERE id = CAST(:aid AS uuid)"),
                {"aid": str(aoi_id)},
            ).scalar()
        if buf_m is not None:
            buffer_km = round(float(buf_m) / 1000.0, 3)
    return {
        "status":          proj["status"],
        "last_filter":     proj.get("last_filter"),
        "last_results":    proj.get("last_results"),
        "last_filter_uf":  proj.get("last_filter_uf"),
        "last_results_uf": proj.get("last_results_uf"),
        "last_results_ilots_rpg": ilots_rpg_api.attach_proprietaires(
            engine, project_id, ilots_rpg_api.get_results(engine, project_id)
        ),
        "layers_status":   proj.get("layers_status", {}),
        "buffer_km":       buffer_km,
    }


@app.get("/api/projects/{project_id}/geojson")
def get_parcelles_geojson(
    project_id: str,
    run_id: str | None = Query(None, description="UUID run pool — sinon dernier last_results du projet"),
):
    proj = _get_project(project_id)
    aoi_id_str = str(proj.get("aoi_id") or "")

    if run_id:
        with engine.begin() as conn:
            pool_service.ensure_tables(conn)
            if not pool_service.run_belongs_to_project(conn, project_id, run_id):
                raise HTTPException(404, f"Run {run_id} introuvable pour ce projet")
            parcelles_data = pool_service.get_parcelles_for_run_results(
                conn, project_id, run_id, aoi_id_str
            )
        pool_run_id = run_id
        results = {"parcelles": parcelles_data, "pool_run_id": pool_run_id}
    else:
        if not proj.get("last_results"):
            raise HTTPException(400, "Aucun résultat")
        results = proj["last_results"]
        if isinstance(results, str):
            results = json.loads(results)
        parcelles_data = results.get("parcelles", [])
        pool_run_id = results.get("pool_run_id")

    if not parcelles_data:
        raise HTTPException(400, "Aucune parcelle dans les résultats")

    idus       = [p["idu"] for p in parcelles_data if p.get("idu")]
    idu_to_rank = {p["idu"]: int(p.get("rank", 0)) for p in parcelles_data if p.get("idu")}
    ranks = [r for r in idu_to_rank.values() if r > 0]
    min_r = min(ranks, default=1)
    max_r = max(ranks, default=1)
    rng = max_r - min_r or 1

    idu_to_parcel_score: dict[str, float] = {}
    if pool_run_id:
        try:
            with engine.begin() as conn:
                srows = conn.execute(
                    text(
                        """
                        SELECT idu, (metric_value_jsonb->>'total_score')::double precision AS ts
                        FROM ecocompensation_results.parcelles_pool_metrics
                        WHERE project_id = CAST(:pid AS uuid)
                          AND run_id = CAST(:rid AS uuid)
                          AND metric_key = 'score_eco'
                          AND idu = ANY(:idus)
                        """
                    ),
                    {"pid": project_id, "rid": str(pool_run_id), "idus": idus},
                ).mappings().all()
            for sr in srows:
                ts = sr.get("ts")
                if ts is not None and sr.get("idu"):
                    idu_to_parcel_score[str(sr["idu"])] = float(ts)
        except Exception:
            logger.exception(
                "GeoJSON parcelles : lecture score_eco ignorée (project_id=%s)",
                project_id,
            )

    score_vals = list(idu_to_parcel_score.values())
    if score_vals:
        min_s = min(score_vals)
        max_s = max(score_vals)
        rng_s = max_s - min_s or 1.0
    else:
        min_s = max_s = rng_s = None

    with engine.begin() as conn:
        pool_service.ensure_tables(conn)
        if pool_run_id:
            rows = pool_service.get_parcelles_geometries_for_run(
                conn, project_id, str(pool_run_id)
            )
        else:
            rows = pool_service.get_parcelles_geometries_by_idus(conn, project_id, idus)

    features: list[dict] = []
    for r in rows:
        idu = str(r["idu"])
        rank_norm = round(
            1.0 - ((idu_to_rank.get(idu, max_r) - min_r) / rng),
            4,
        )
        props: dict = {
            "idu": idu,
            "rank": idu_to_rank.get(idu, 0),
            "score_norm": rank_norm,
        }
        if rng_s is not None and min_s is not None and idu in idu_to_parcel_score:
            t = idu_to_parcel_score[idu]
            props["total_score"] = round(t, 4)
            props["score_norm"] = round((t - min_s) / rng_s, 4)
            props["score_norm_source"] = "score_eco"
        else:
            props["score_norm_source"] = "distance_rank"

        features.append(
            {
                "type": "Feature",
                "geometry": dict(r["geometry"]),
                "properties": props,
            }
        )

    return {"type": "FeatureCollection", "features": features}



@app.get("/api/projects/{project_id}/geojson/uf-parcelles")
def get_uf_parcelles_geojson(project_id: str):
    """Parcelles des unités foncières pour la carte UF."""
    from api.recherche_de_foncier.filtrage.layers.unites_foncieres.pool import build_geojson

    proj = _get_project(project_id)
    results_uf = proj.get("last_results_uf")
    if not results_uf:
        raise HTTPException(400, "Aucun résultat UF")
    if isinstance(results_uf, str):
        results_uf = json.loads(results_uf)
    return build_geojson(engine, project_id, results_uf)


@app.get("/api/projects/{project_id}/uf/{uf_id}/occupation-sol.geojson")
def get_uf_occupation_sol_geojson(project_id: str, uf_id: str):
    """Occupation du sol CESBIO d'une unité foncière."""
    from api.recherche_de_foncier.filtrage.layers.unites_foncieres.pool import build_veg_geojson

    _get_project(project_id)
    return build_veg_geojson(engine, project_id, uf_id)


@app.get("/api/projects/{project_id}/parcelles/{idu}/occupation-sol.geojson")
def get_parcelle_occupation_sol_geojson(project_id: str, idu: str):
    """Occupation du sol CESBIO découpée sur une parcelle du pool, ou du cadastre national."""
    from api.recherche_de_foncier.filtrage.layers.cesbio_clip import clip_cesbio_geojson

    _get_project(project_id)
    geom_sql = """
        SELECT geom_2154 AS geom
        FROM ecocompensation_results.parcelles
        WHERE project_id = CAST(:pid AS uuid) AND idu = :idu AND geom_2154 IS NOT NULL
        UNION ALL
        SELECT geom_2154 AS geom
        FROM ecocompensation.parcelles
        WHERE idu = :idu AND geom_2154 IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM ecocompensation_results.parcelles
              WHERE project_id = CAST(:pid AS uuid) AND idu = :idu AND geom_2154 IS NOT NULL
          )
        LIMIT 1
    """
    params = {"pid": project_id, "idu": idu}
    with engine.connect() as conn:
        found = conn.execute(text(f"SELECT geom IS NOT NULL AS ok FROM ({geom_sql}) s"), params).first()
        if not found:
            raise HTTPException(404, "Parcelle introuvable")
        return clip_cesbio_geojson(conn, geom_sql, params)


@app.get("/api/projects/{project_id}/ilots-rpg")
def get_ilots_rpg(project_id: str):
    _get_project(project_id)
    res = ilots_rpg_api.attach_proprietaires(engine, project_id, ilots_rpg_api.get_results(engine, project_id))
    return res or {"source": "rpg_ilots", "millesime": "2025", "total_candidats": 0, "total_ilots": 0, "ilots": []}


@app.get("/api/projects/{project_id}/geojson/ilots-rpg")
def get_ilots_rpg_geojson(project_id: str):
    _get_project(project_id)
    return ilots_rpg_api.get_geojson(engine, project_id)


@app.post("/api/projects/{project_id}/ilots-rpg/recompute-durete", status_code=202)
def recompute_ilots_rpg_durete(project_id: str, body: dict | None = Body(default=None)):
    """Dureté foncière des îlots : un LLM par SIREN (cache), score pondéré par la surface."""
    _get_project(project_id)
    from api.recherche_de_foncier.filtrage.layers.rpg.rpg_ilots.durete import start_job

    raw = (body or {}).get("num_ilots") or []
    num_ilots = [str(x).strip() for x in raw if str(x).strip()] or None
    job_id = start_job(engine, project_id, num_ilots)
    return {"status": "accepted", "job_id": job_id, "project_id": project_id}


@app.get("/api/projects/{project_id}/ilots-rpg/recompute-durete/jobs/{job_id}")
def recompute_ilots_rpg_durete_job(project_id: str, job_id: str):
    from api.recherche_de_foncier.filtrage.layers.rpg.rpg_ilots.durete import get_job

    job = get_job(job_id)
    if not job or job.get("project_id") != project_id:
        raise HTTPException(404, "Job dureté îlots introuvable")
    return job


# ─────────────────────────────────────────────
# WebSocket
# ─────────────────────────────────────────────

@app.websocket("/ws/projects/{project_id}/fetch-progress")
async def fetch_progress_ws(project_id: str, websocket: WebSocket):
    await ws_manager.connect(project_id, websocket)
    try:
        proj = _get_project(project_id)
        await websocket.send_json({"event": "connected", "status": proj["status"],
                                   "layers_status": proj.get("layers_status", {})})
        while True:
            await asyncio.sleep(30)
            try:
                await websocket.send_json({"event": "ping"})
            except (WebSocketDisconnect, RuntimeError):
                break
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(project_id, websocket)


# ─────────────────────────────────────────────
# Utilitaires
# ─────────────────────────────────────────────

@app.get("/api/memory")
def get_memory():
    ram_mb = _get_process_memory_mb()
    try:
        vms_mb = round(psutil.Process(os.getpid()).memory_info().vms / (1024 * 1024), 2)
    except Exception:
        vms_mb = None
    return {"ram_mb": ram_mb, "vms_mb": vms_mb}


@app.get("/api/layers")
def list_layers():
    return [{"key": l["key"], "label": l["label"], "fast": l["fast"]} for l in LAYER_REGISTRY]


@app.get("/api/filter-phases")
def list_filter_phases():
    """Phases du nouveau pipeline (remplace la sélection de couches SIG)."""
    return FULL_PIPELINE_PHASES


@app.get("/api/reference/remontee-nappes-classefiab")
def list_remontee_nappes_classefiab():
    """Valeurs distinctes de `classefiab` dans la couche nationale (menu filtre)."""
    with engine.begin() as conn:
        exists = conn.execute(
            text("SELECT to_regclass(:r) IS NOT NULL").execution_options(no_prepare=True),
            {"r": "ecocompensation.remontee_de_nappes"},
        ).scalar_one()
        if not exists:
            return {"values": []}
        rows = conn.execute(
            text(
                """
                SELECT DISTINCT btrim(classefiab::text) AS v
                FROM ecocompensation.remontee_de_nappes
                WHERE classefiab IS NOT NULL
                  AND btrim(classefiab::text) <> ''
                ORDER BY 1
                """
            )
        ).all()
    return {"values": [str(v) for (v,) in rows if v]}


def _resolve_taxnomval_column(conn, full_table: str) -> str | None:
    if "." not in full_table:
        return None
    schema, table = full_table.split(".", 1)
    # Priorité : ancienne convention taxnomval, puis colonnes présentes
    # dans `ecocompensation.fauna` (ex: nom_vernaculaire).
    candidates = ["taxnomval", "nom_vernaculaire", "nom_taxref", "tax_nom_val", "nom_vern"]
    for cand in candidates:
        row = conn.execute(
            text(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = :schema
                  AND table_name = :table
                  AND lower(column_name) = :col
                LIMIT 1
                """
            ),
            {"schema": schema, "table": table, "col": cand},
        ).mappings().one_or_none()
        if row and row.get("column_name"):
            col = str(row["column_name"])
            return f'"{col}"' if col != col.lower() else col
    return None


@app.get("/api/fauna/taxa")
def list_fauna_taxa():
    with engine.begin() as conn:
        exists = conn.execute(
            text("SELECT to_regclass(:r) IS NOT NULL").execution_options(no_prepare=True),
            {"r": "ecocompensation.fauna_taxa_ref"},
        ).scalar_one()
        if not exists:
            logger.warning("Table de référence absente: ecocompensation.fauna_taxa_ref")
            return {"taxa": []}

        rows = conn.execute(
            text(
                """
                SELECT btrim(tax::text) AS tax
                FROM ecocompensation.fauna_taxa_ref
                WHERE tax IS NOT NULL
                  AND btrim(tax::text) <> ''
                ORDER BY tax
                """
            )
        ).all()
    return {"taxa": [str(v) for (v,) in rows if v]}


@app.get("/api/projects/{project_id}/fauna/taxa")
def list_project_fauna_taxa(project_id: str):
    _get_project(project_id)
    with engine.begin() as conn:
        exists = conn.execute(
            text("SELECT to_regclass(:r) IS NOT NULL").execution_options(no_prepare=True),
            {"r": "ecocompensation_results.fauna"},
        ).scalar_one()
        if not exists:
            return {"taxa": []}

        tax_col = _resolve_taxnomval_column(conn, "ecocompensation_results.fauna")
        if not tax_col:
            return {"taxa": []}

        rows = conn.execute(
            text(
                f"""
                SELECT DISTINCT btrim({tax_col}::text) AS tax
                FROM ecocompensation_results.fauna
                WHERE project_id = :pid
                  AND {tax_col} IS NOT NULL
                  AND btrim({tax_col}::text) <> ''
                ORDER BY tax
                """
            ),
            {"pid": project_id},
        ).all()
    return {"taxa": [str(v) for (v,) in rows if v]}


# Monter le frontend statique en dernier pour ne pas intercepter /api/*
_dist = Path(__file__).parent.parent / "frontend" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)