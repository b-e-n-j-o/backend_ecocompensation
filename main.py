#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py
=======

FastAPI — routes uniquement. Zéro logique métier.
Toute la logique métier vit dans :
  - vrai_filtre.py          → filtre + scoring parcelles seules
  - filtre_uf.py            → filtre + scoring unités foncières
  - orchestrator.py         → orchestration des couches
  - layers/layer_runner.py  → registre des couches

Endpoints :
    POST   /api/projects                      créer un projet
    GET    /api/projects                      lister les projets
    GET    /api/projects/{id}                 détail d'un projet
    DELETE /api/projects/{id}                 supprimer projet + données AOI
    POST   /api/projects/{id}/fetch           lancer l'orchestration des couches
    POST   /api/projects/{id}/filter          filtrage parcelles seules + scoring
    POST   /api/projects/{id}/filter/uf       filtrage unités foncières + scoring
    GET    /api/projects/{id}/results         derniers résultats
    GET    /api/projects/{id}/geojson         GeoJSON parcelles
    GET    /api/projects/{id}/export/csv      export CSV (?scope=parcelles|uf)
    GET    /api/projects/{id}/export/shp      export Shapefile (?scope=parcelles|uf)
    GET    /api/memory                        RAM du processus backend
    GET    /api/layers                        liste des couches
    WS     /ws/projects/{id}/fetch-progress   suivi temps réel des fetches
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import psutil
import geopandas as gpd
from dotenv import load_dotenv
from fastapi import (
    FastAPI, HTTPException, UploadFile, File, Form, Query,
    WebSocket, WebSocketDisconnect, BackgroundTasks,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import text

# ── Modules métier ────────────────────────────────────────────────────────────
from orchestrator import run_orchestration
from layers.layer_runner import LAYER_REGISTRY
from db import get_engine
from routers.foncier_router import router as foncier_router

from vrai_filtre import (
    FiltreOptions,
    run_filter_and_score,      # filtre parcelles + scoring
)
from filtre_uf import (
    FiltreOptions as FiltreOptionsUF,   # même dataclass, réexportée
    run_filter_uf_and_score,            # filtre UF + scoring
)

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
        "https://ecocompensation-frontend.vercel.app/create-aoi",
        "https://ecocompensation-frontend.vercel.app",
        "https://ecocompensation-frontend-3nm5hbhou-matinducoins-projects.vercel.app",
    ]
    raw = os.getenv("CORS_ORIGINS", "")
    extra = [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]
    merged = [o.rstrip("/") for o in default_origins] + extra
    # Déduplication en conservant l'ordre
    return list(dict.fromkeys(merged))


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
    yield


# ─────────────────────────────────────────────
# App
# ─────────────────────────────────────────────

app = FastAPI(title="KERELIA Ecocompensation API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_parse_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(foncier_router, prefix="/api/foncier")

_dist = Path(__file__).parent.parent / "frontend" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="static")


# ─────────────────────────────────────────────
# Schémas Pydantic
# ─────────────────────────────────────────────

class FiltreOptionsDTO(BaseModel):
    zdv_natures:              list[str] = Field(default_factory=list)
    troncon_hydro_mode:       str   = "intersect"
    troncon_hydro_radius_m:   float = 500.0
    surface_hydro_mode:       str   = "within_radius"
    surface_hydro_radius_m:   float = 500.0
    miller_threshold:         float = 0.39
    min_area_ha:              float = 7.0
    target_count:             int   = Field(
        default=50,
        ge=0,
        le=20_000,
        description="Nombre max de parcelles (resp. UF) retournées après scoring ; 0 = illimité.",
    )
    radius_start_km:          float = 10.0
    radius_min_km:            float = 1.0
    # Poids du scoring
    score_dist_lt2km:         int   = 3
    score_dist_lt5km:         int   = 2
    score_dist_lt10km:        int   = 1
    score_surface_ge20ha:     int   = 1
    score_miller_ge05:        int   = 1
    score_hydro_lt100m:       int   = 1
    # Seuils du scoring
    score_threshold_miller:   float = 0.5
    score_threshold_surface_ha: float = 20.0
    score_threshold_hydro_m:  float = 100.0
    score_threshold_dist_2km: float = 2.0
    score_threshold_dist_5km: float = 5.0


class FilterRequest(BaseModel):
    options: FiltreOptionsDTO = Field(default_factory=FiltreOptionsDTO)


class FromParcelleRequest(BaseModel):
    code_insee: str   = Field(..., min_length=4, max_length=5)
    section:    str   = Field(..., min_length=1, max_length=4)
    numero:     str   = Field(..., min_length=1, max_length=10)
    name:       str   = Field(..., min_length=1)
    buffer_km:  float = Field(default=5.0, ge=0.0, le=10.0)


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
    return dict(row)


def _get_aoi_centre(aoi_id: str) -> tuple[float, float]:
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
    return row["cx"], row["cy"]


def _dto_to_filtre_options(dto: FiltreOptionsDTO) -> FiltreOptions:
    return FiltreOptions(
        zdv_natures=dto.zdv_natures,
        troncon_hydro_mode=dto.troncon_hydro_mode,
        troncon_hydro_radius_m=dto.troncon_hydro_radius_m,
        surface_hydro_mode=dto.surface_hydro_mode,
        surface_hydro_radius_m=dto.surface_hydro_radius_m,
    )


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
                INSERT INTO ecocompensation.projects (id, name, aoi_id, status)
                VALUES (:project_id, :name, :aoi_id, 'created')
                RETURNING id, name, status, created_at
                """
            ),
            {"project_id": project_id, "name": name, "aoi_id": aoi_id},
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


@app.post("/api/projects/from-parcelle", status_code=201)
def create_project_from_parcelle(body: FromParcelleRequest):
    gdf       = _load_parcelle_wfs(body.code_insee, body.section, body.numero)
    union_geom = gdf.union_all()
    buffer_m  = int(body.buffer_km * 1000)
    area_ha   = float(union_geom.area / 10_000.0)

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
            {"name": body.name, "wkt": union_geom.wkt, "area_ha": area_ha},
        ).scalar_one())
        aoi_id = str(conn.execute(
            text(
                """
                INSERT INTO ecocompensation.aoi (id, code_insee, buffer_m, geom_2154, project_id)
                VALUES (:project_id, :code, :buf, ST_GeomFromText(:wkt, 2154), :project_id)
                RETURNING id
                """
            ),
            {"project_id": project_id, "code": body.code_insee, "buf": buffer_m, "wkt": union_geom.buffer(buffer_m, resolution=32).wkt},
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
                "code_insee": body.code_insee,
                "section": body.section,
                "numero": body.numero,
                "wkt": union_geom.wkt,
            },
        )
        proj = conn.execute(
            text(
                """
                INSERT INTO ecocompensation.projects (id, name, aoi_id, foncier_id, status)
                VALUES (:project_id, :name, :aoi_id, :foncier_id, 'created')
                RETURNING id, name, status, created_at
                """
            ),
            {"project_id": project_id, "name": body.name, "aoi_id": aoi_id, "foncier_id": foncier_id},
        ).mappings().one()

    return {"id": str(proj["id"]), "name": proj["name"], "status": proj["status"],
            "project_id": str(proj["id"]), "aoi_id": aoi_id, "foncier_id": foncier_id,
            "created_at": proj["created_at"].isoformat()}


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
                    pp.code_insee,
                    pp.section,
                    pp.numero,
                    ST_AsGeoJSON(ST_Transform(pp.geom_2154, 4326))::json AS parcelle_geometry,
                    ST_AsGeoJSON(ST_Transform(a.geom_2154, 4326))::json AS aoi_geometry
                FROM ecocompensation.projects p
                LEFT JOIN ecocompensation.project_parcelles pp ON pp.project_id = p.id
                LEFT JOIN ecocompensation.aoi a ON a.id = p.aoi_id
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

    return {
        "project_id": str(row["project_id"]),
        "name": row.get("name"),
        "parcelle_source": parcelle_feature,
        "aoi": aoi_feature,
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
        "ecocompensation_results.patrimoine_naturel", "ecocompensation_results.zone_de_vegetation",
        "ecocompensation_results.zone_humide", "ecocompensation_results.troncons_hydro",
        "ecocompensation_results.surfaces_hydro", "ecocompensation_results.surfaces_elementaires",
        "ecocompensation_results.routes", "ecocompensation_results.voies_ferrees",
        "ecocompensation_results.fragmentation_polygons", "ecocompensation_results.zones_humides_probables",
        "ecocompensation_results.znieff", "ecocompensation_results.frayeres",
        "ecocompensation_results.arrachage_vignes",
        "ecocompensation_results.unites_foncieres",   # ← nouveau
        "ecocompensation_results.sous_ensembles",     # ← nouveau
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


@app.post("/api/projects/{project_id}/fetch")
async def start_fetch(project_id: str, background_tasks: BackgroundTasks):
    proj = _get_project(project_id)
    if proj["status"] == "fetching" or project_id in _running_fetches:
        raise HTTPException(409, "Fetch déjà en cours")
    aoi_id = str(proj["aoi_id"])
    _running_fetches.add(project_id)

    async def _run():
        try:
            await run_orchestration(engine, project_id, aoi_id,
                                    lambda d: ws_manager.broadcast(project_id, d))
        finally:
            _running_fetches.discard(project_id)

    background_tasks.add_task(_run)
    return {"status": "started", "project_id": project_id}


# ─────────────────────────────────────────────
# Prérequis filtre UF : sous-ensembles générés en amont
# ─────────────────────────────────────────────

@app.get("/api/projects/{project_id}/sous-ensembles-status")
def sous_ensembles_status(project_id: str):
    """
    Indique si `ecocompensation_results.sous_ensembles` contient au moins une ligne
    pour ce projet. Sans cela, le filtre UF ne peut pas s'exécuter (parcelles seules uniquement).
    """
    _get_project(project_id)
    with engine.begin() as conn:
        exists = conn.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM ecocompensation_results.sous_ensembles
                    WHERE project_id = :pid
                    LIMIT 1
                )
                """
            ),
            {"pid": project_id},
        ).scalar_one()
    return {"has_sous_ensembles": bool(exists)}


# ─────────────────────────────────────────────
# Route — Filter parcelles seules
# ─────────────────────────────────────────────

@app.post("/api/projects/{project_id}/filter")
def run_filter(project_id: str, body: FilterRequest):
    proj = _get_project(project_id)
    if proj["status"] not in ("ready", "ready_with_errors", "done"):
        raise HTTPException(400, f"Projet non prêt (status={proj['status']})")

    aoi_id  = str(proj["aoi_id"])
    opts_dto = body.options
    cx, cy  = _get_aoi_centre(aoi_id)
    options = _dto_to_filtre_options(opts_dto)

    ram_before = _get_process_memory_mb()

    # ── Toute la logique métier dans vrai_filtre.py ──
    result = run_filter_and_score(engine, project_id, aoi_id, cx, cy, options, opts_dto)

    ram_after = _get_process_memory_mb()
    memory_info = {"ram_mb_before": ram_before, "ram_mb_after": ram_after,
                   "ram_mb_delta": round(ram_after - ram_before, 2)}

    # Persistance
    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE ecocompensation.projects
                SET last_filter = :f, last_results = :r, status = 'done', updated_at = now()
                WHERE id = :pid
            """),
            {"f": json.dumps(opts_dto.model_dump()),
             "r": json.dumps({**result, "memory": memory_info}),
             "pid": project_id},
        )

    return {**result, "memory": memory_info}


# ─────────────────────────────────────────────
# Route — Filter unités foncières
# ─────────────────────────────────────────────

@app.post("/api/projects/{project_id}/filter/uf")
def run_filter_uf(project_id: str, body: FilterRequest):
    proj = _get_project(project_id)
    if proj["status"] not in ("ready", "ready_with_errors", "done"):
        raise HTTPException(400, f"Projet non prêt (status={proj['status']})")

    aoi_id   = str(proj["aoi_id"])
    opts_dto = body.options
    cx, cy   = _get_aoi_centre(aoi_id)
    options  = _dto_to_filtre_options(opts_dto)

    ram_before = _get_process_memory_mb()

    # ── Toute la logique métier dans filtre_uf.py ──
    result = run_filter_uf_and_score(engine, project_id, aoi_id, cx, cy, options, opts_dto)

    ram_after = _get_process_memory_mb()
    memory_info = {"ram_mb_before": ram_before, "ram_mb_after": ram_after,
                   "ram_mb_delta": round(ram_after - ram_before, 2)}

    # Persistance
    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE ecocompensation.projects
                SET last_filter_uf = :f, last_results_uf = :r, status = 'done', updated_at = now()
                WHERE id = :pid
            """),
            {"f": json.dumps(opts_dto.model_dump()),
             "r": json.dumps({**result, "memory": memory_info}),
             "pid": project_id},
        )

    return {**result, "memory": memory_info}


# ─────────────────────────────────────────────
# Routes — Résultats & exports
# ─────────────────────────────────────────────

@app.get("/api/projects/{project_id}/results")
def get_results(project_id: str):
    proj = _get_project(project_id)
    return {
        "status":          proj["status"],
        "last_filter":     proj.get("last_filter"),
        "last_results":    proj.get("last_results"),
        "last_filter_uf":  proj.get("last_filter_uf"),
        "last_results_uf": proj.get("last_results_uf"),
        "layers_status":   proj.get("layers_status", {}),
    }


@app.get("/api/projects/{project_id}/geojson")
def get_parcelles_geojson(project_id: str):
    proj = _get_project(project_id)
    if not proj.get("last_results"):
        raise HTTPException(400, "Aucun résultat")
    results = proj["last_results"]
    if isinstance(results, str):
        results = json.loads(results)
    parcelles_data = results.get("parcelles", [])
    if not parcelles_data:
        raise HTTPException(400, "Aucune parcelle dans les résultats")

    idus       = [p["idu"] for p in parcelles_data if p.get("idu")]
    scores     = [int(p.get("score", 0)) for p in parcelles_data if p.get("idu")]
    min_s, max_s = min(scores, default=0), max(scores, default=0)
    rng        = max_s - min_s or 1
    idu_to_score = {p["idu"]: int(p.get("score", 0)) for p in parcelles_data if p.get("idu")}
    aoi_id_str = str(proj.get("aoi_id") or "")

    with engine.begin() as conn:
        rows = conn.execute(
            text("""
                SELECT
                    p.idu,
                    ST_AsGeoJSON(ST_Transform(p.geom_2154, 4326))::json AS geometry
                FROM ecocompensation_results.parcelles p
                WHERE (p.project_id = :pid OR p.aoi_id = :aoi_id_str)
                  AND p.idu = ANY(:idus)
            """),
            {"pid": project_id, "aoi_id_str": aoi_id_str, "idus": idus},
        ).mappings().all()

    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": dict(r["geometry"]),
         "properties": {"idu": r["idu"], "score": idu_to_score.get(r["idu"], 0),
                        "score_norm": round((idu_to_score.get(r["idu"], 0) - min_s) / rng, 4)}}
        for r in rows
    ]}


@app.get("/api/projects/{project_id}/geojson/uf-subsets")
def get_uf_subsets_geojson(project_id: str):
    """
    GeoJSON des sous-ensembles UF (table sous_ensembles) pour le dernier filtre UF du projet.
    Les propriétés contiennent au minimum `subset_id` (le scoring est fait côté frontend).
    """
    proj = _get_project(project_id)
    if not proj.get("last_results_uf"):
        raise HTTPException(400, "Aucun résultat UF")

    results_uf = proj["last_results_uf"]
    if isinstance(results_uf, str):
        results_uf = json.loads(results_uf)

    unites = results_uf.get("unites_foncieres", [])
    subset_ids: list[str] = []
    for uf in unites:
        for ss in uf.get("sous_ensembles", []) or []:
            sid = ss.get("subset_id")
            if sid:
                subset_ids.append(sid)

    if not subset_ids:
        raise HTTPException(400, "Aucun sous-ensemble UF dans les résultats")

    with engine.begin() as conn:
        rows = conn.execute(
            text("""
                SELECT
                    s.subset_id,
                    s.siren,
                    s.denomination,
                    ST_AsGeoJSON(ST_Transform(s.geom_2154, 4326))::json AS geometry
                FROM ecocompensation_results.sous_ensembles s
                WHERE s.project_id = :pid
                  AND s.subset_id = ANY(:subset_ids)
            """),
            {"pid": project_id, "subset_ids": subset_ids},
        ).mappings().all()

    features = [
        {
            "type": "Feature",
            "geometry": dict(r["geometry"]),
            "properties": {
                "subset_id": r["subset_id"],
                "siren": r.get("siren"),
                "denomination": r.get("denomination"),
            },
        }
        for r in rows
    ]

    return {"type": "FeatureCollection", "features": features}


@app.get("/api/projects/{project_id}/export/csv")
def export_csv(
    project_id: str,
    background_tasks: BackgroundTasks,
    scope: str = Query("parcelles", description="parcelles | uf"),
):
    import tempfile
    from fastapi.responses import FileResponse
    from export_classement_csv import export_classement_csv
    from export_uf_classement_csv import export_uf_classement_csv

    s = scope.lower().strip()
    if s not in ("parcelles", "uf"):
        raise HTTPException(400, "Paramètre scope invalide (parcelles ou uf).")

    proj = _get_project(project_id)

    if s == "parcelles":
        last_results = proj.get("last_results")
        if not last_results:
            raise HTTPException(400, "Aucun résultat parcelles")
        if isinstance(last_results, str):
            last_results = json.loads(last_results)
        parcelles = last_results.get("parcelles", [])
        if not parcelles:
            raise HTTPException(400, "Aucune parcelle")
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".csv", encoding="utf-8-sig") as nf:
            csv_path = Path(nf.name)
            export_classement_csv(parcelles, csv_path)
        background_tasks.add_task(os.remove, str(csv_path))
        return FileResponse(
            str(csv_path),
            filename=f"parcelles_{project_id[:8]}.csv",
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
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".csv", encoding="utf-8-sig") as nf:
        csv_path = Path(nf.name)
        export_uf_classement_csv(last_results_uf, csv_path)
    background_tasks.add_task(os.remove, str(csv_path))
    return FileResponse(
        str(csv_path),
        filename=f"uf_{project_id[:8]}.csv",
        media_type="text/csv; charset=utf-8",
    )


@app.get("/api/projects/{project_id}/export/shp")
def export_shp(
    project_id: str,
    background_tasks: BackgroundTasks,
    scope: str = Query("parcelles", description="parcelles | uf"),
):
    import shutil, tempfile, zipfile
    from fastapi.responses import FileResponse
    from export_classement_shp import export_classement_shp
    from export_uf_classement_shp import export_uf_classement_shp

    s = scope.lower().strip()
    if s not in ("parcelles", "uf"):
        raise HTTPException(400, "Paramètre scope invalide (parcelles ou uf).")

    proj = _get_project(project_id)

    if s == "parcelles":
        last_results = proj.get("last_results")
        last_filter = proj.get("last_filter")
        if not last_results or not last_filter:
            raise HTTPException(400, "Résultats ou filtre parcelles introuvables")
        if isinstance(last_results, str):
            last_results = json.loads(last_results)
        if isinstance(last_filter, str):
            last_filter = json.loads(last_filter)
        parcelles = last_results.get("parcelles", [])
        if not parcelles:
            raise HTTPException(400, "Aucune parcelle")
        opts_dto = FiltreOptionsDTO(**last_filter)
        options = _dto_to_filtre_options(opts_dto)
        final_radius_km = last_results.get("final_radius_km", 0)
        aoi_id = str(proj.get("aoi_id") or "")
        with tempfile.TemporaryDirectory() as tmpdir:
            shp_path = Path(tmpdir) / "parcelles.shp"
            try:
                export_classement_shp(
                    engine, project_id, parcelles, options, final_radius_km, shp_path, aoi_id=aoi_id
                )
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
            zip_path = Path(tmpdir) / "parcelles.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
                    f = shp_path.with_suffix(ext)
                    if f.exists():
                        zf.write(f, f.name)
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

    # scope == uf
    last_results_uf = proj.get("last_results_uf")
    last_filter_uf = proj.get("last_filter_uf")
    if not last_results_uf or not last_filter_uf:
        raise HTTPException(400, "Résultats ou filtre UF introuvables")
    if isinstance(last_results_uf, str):
        last_results_uf = json.loads(last_results_uf)
    if isinstance(last_filter_uf, str):
        last_filter_uf = json.loads(last_filter_uf)
    opts_dto = FiltreOptionsDTO(**last_filter_uf)
    options = _dto_to_filtre_options(opts_dto)
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
            await websocket.send_json({"event": "ping"})
    except WebSocketDisconnect:
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)