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
    GET    /api/projects/{id}/export/rapport-pdf   rapport PDF classement parcelles
    GET    /api/memory                        RAM du processus backend
    GET    /api/layers                        liste des couches
    WS     /ws/projects/{id}/fetch-progress   suivi temps réel des fetches
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import psutil
import geopandas as gpd
import pandas as pd
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
from routers.pool_router import router as pool_router
from routers.results_geojson_router import router as results_geojson_router
from routers.durete_router import router as durete_router
from exports.router_exports import router as exports_router
from routers.rapport_router import router as rapport_router
from pool import pool_service
from pool.pool_service import persist_parcelles_pool_run

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

app.include_router(foncier_router, prefix="/api/foncier")
app.include_router(pool_router)
app.include_router(results_geojson_router)
app.include_router(durete_router)
app.include_router(exports_router)
app.include_router(rapport_router)

# Monte le backend Identité Foncière V0 sans dupliquer de repo.
# Le préfixe dédié évite de surcharger ce main.py avec ses routes métiers.
try:
    workspace_root = Path(__file__).resolve().parents[3]
    if str(workspace_root) not in sys.path:
        sys.path.insert(0, str(workspace_root))

    from IDENTITE_FONCIERE.v0.main import app as identite_fonciere_app

    app.mount("/api/identite-fonciere", identite_fonciere_app)
    logger.info("✅ Identité Foncière V0 montée sur /api/identite-fonciere")
except Exception as e:
    logger.warning("⚠️  Impossible de monter Identité Foncière V0 : %s", e)


# ─────────────────────────────────────────────
# Schémas Pydantic
# ─────────────────────────────────────────────

class FiltreOptionsDTO(BaseModel):
    class VegetationHybrideDTO(BaseModel):
        zdv_natures: list[str] = Field(default_factory=list)
        cesbio_libelles: list[str] = Field(default_factory=list)
        mode: str = Field(default="OR", pattern="^(OR|AND)$")

    class FauneCriterionDTO(BaseModel):
        tax_nom_val: str = Field(..., min_length=1)
        mode: str = Field(default="intersect", pattern="^(intersect|within_radius)$")
        radius_m: float = Field(default=500.0, ge=0.0, le=5000.0)
        sources: list[str] = Field(default_factory=lambda: ["pct", "lin", "surf"])

    vegetation_hybride: VegetationHybrideDTO = Field(default_factory=VegetationHybrideDTO)
    funnel_mode: bool = Field(
        default=False,
        description="Active le calcul détaillé de l'entonnoir (plus lent).",
    )
    log_progress: bool = Field(
        default=False,
        description="Active les logs de progression du filtrage (étapes et volumes).",
    )
    carhab_nom_eunis:         list[str] = Field(
        default_factory=list,
        description="Habitats Carhab : libellés EUNIS (nom_eunis) ; intersection avec au moins un polygone.",
    )
    excluded_layers:          list[str] = Field(
        default_factory=list,
        description="Couches exclues automatiquement (ex: geomce, project_indesirables).",
    )
    arrachage_vignes_mode:    str   = Field(
        default="ignore",
        pattern="^(ignore|intersect|exclude)$",
        description="Arrachage de vignes : ignorer, intersecter la couche, ou exclure les parcelles qui intersectent.",
    )
    zone_humide_mode:         str   = Field(
        default="ignore",
        pattern="^(ignore|intersect|exclude)$",
        description="Zones humides : ignorer, intersecter la couche, ou exclure les parcelles qui intersectent.",
    )
    ebc_mode: str = Field(
        default="ignore",
        pattern="^(ignore|intersect|exclude)$",
        description="Espaces boisés classés (EBC) : ignorer, intersecter, ou exclure.",
    )
    natura2000_mode: str = Field(
        default="exclude",
        pattern="^(ignore|intersect|exclude)$",
        description="Natura 2000 : ignorer, intersecter, ou exclure (défaut = exclure, comportement historique).",
    )
    reserves_naturelles_mode: str = Field(
        default="ignore",
        pattern="^(ignore|intersect|exclude)$",
        description="Réserves naturelles : ignorer, intersecter, ou exclure.",
    )
    znieff_mode: str = Field(
        default="ignore",
        pattern="^(ignore|intersect|exclude)$",
        description="ZNIEFF (types I et II) : ignorer, intersecter, ou exclure.",
    )
    remontee_nappes_classefiab: list[str] = Field(
        default_factory=list,
        description="Remontées de nappes : valeurs classefiab à intersecter (liste vide = critère neutre).",
    )
    troncon_hydro_mode:       str   = "intersect"
    troncon_hydro_radius_m:   float = 500.0
    surface_hydro_mode:       str   = "within_radius"
    surface_hydro_radius_m:   float = 500.0
    faune_criteria:           list[FauneCriterionDTO] = Field(default_factory=list)
    miller_threshold:         float = 0.39
    min_area_ha:              float = 7.0
    target_count:             int   = Field(
        default=50,
        ge=0,
        le=20_000,
        description="Nombre max de parcelles (resp. UF) retournées après classement ; 0 = illimité.",
    )
    radius_start_km:          float = 10.0
    radius_min_km:            float = 1.0


class FilterRequest(BaseModel):
    options: FiltreOptionsDTO = Field(default_factory=FiltreOptionsDTO)


class FromParcelleRequest(BaseModel):
    code_insee: str   = Field(..., min_length=4, max_length=5)
    section:    str   = Field(..., min_length=1, max_length=4)
    numero:     str   = Field(..., min_length=1, max_length=10)
    name:       str   = Field(..., min_length=1)
    buffer_km:  float = Field(default=5.0, ge=0.0, le=10.0)


class ParcelleRefDTO(BaseModel):
    code_insee: str = Field(..., min_length=4, max_length=5)
    section: str = Field(..., min_length=1, max_length=4)
    numero: str = Field(..., min_length=1, max_length=10)


class FromParcellesRequest(BaseModel):
    parcelles: list[ParcelleRefDTO] = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    buffer_km: float = Field(default=5.0, ge=0.0, le=10.0)


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
        vegetation_hybride={
            "zdv_natures": [
                x.strip() for x in dto.vegetation_hybride.zdv_natures if x and str(x).strip()
            ],
            "cesbio_libelles": [
                x.strip()
                for x in dto.vegetation_hybride.cesbio_libelles
                if x and str(x).strip()
            ],
            "mode": dto.vegetation_hybride.mode,
        },
        carhab_nom_eunis=[x.strip() for x in dto.carhab_nom_eunis if x and str(x).strip()],
        excluded_layers=[x.strip() for x in dto.excluded_layers if x and str(x).strip()],
        ebc_mode=dto.ebc_mode,
        natura2000_mode=dto.natura2000_mode,
        reserves_naturelles_mode=dto.reserves_naturelles_mode,
        znieff_mode=dto.znieff_mode,
        remontee_nappes_classefiab=[
            x.strip() for x in dto.remontee_nappes_classefiab if x and str(x).strip()
        ],
        arrachage_vignes_mode=dto.arrachage_vignes_mode,
        zone_humide_mode=dto.zone_humide_mode,
        troncon_hydro_mode=dto.troncon_hydro_mode,
        troncon_hydro_radius_m=dto.troncon_hydro_radius_m,
        surface_hydro_mode=dto.surface_hydro_mode,
        surface_hydro_radius_m=dto.surface_hydro_radius_m,
        faune_criteria=[
            {
                "tax_nom_val": c.tax_nom_val.strip(),
                "mode": c.mode,
                "radius_m": float(c.radius_m),
                "sources": [s for s in c.sources if s in ("pct", "lin", "surf")] or ["pct", "lin", "surf"],
            }
            for c in dto.faune_criteria
            if c.tax_nom_val.strip()
        ],
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


@app.get("/api/projects/history")
def list_projects_history():
    with engine.begin() as conn:
        has_pool_runs = bool(
            conn.execute(
                text("SELECT to_regclass(:tbl) IS NOT NULL"),
                {"tbl": "ecocompensation_results.parcelles_pool_runs"},
            ).scalar()
        )

        if has_pool_runs:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        p.id,
                        p.name,
                        p.status,
                        p.layers_status,
                        p.created_at,
                        p.updated_at,
                        p.last_filter,
                        a.buffer_m,
                        f.area_ha AS foncier_area_ha,
                        pr.total_count AS pool_total_count
                    FROM ecocompensation.projects p
                    LEFT JOIN ecocompensation.aoi a
                        ON a.id = p.aoi_id
                    LEFT JOIN ecocompensation.foncier f
                        ON f.id = p.foncier_id
                    LEFT JOIN LATERAL (
                        SELECT total_count
                        FROM ecocompensation_results.parcelles_pool_runs
                        WHERE project_id = p.id
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) pr ON TRUE
                    ORDER BY p.created_at DESC
                    """
                )
            ).mappings().all()
        else:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        p.id,
                        p.name,
                        p.status,
                        p.layers_status,
                        p.created_at,
                        p.updated_at,
                        p.last_filter,
                        a.buffer_m,
                        f.area_ha AS foncier_area_ha,
                        NULL::integer AS pool_total_count
                    FROM ecocompensation.projects p
                    LEFT JOIN ecocompensation.aoi a
                        ON a.id = p.aoi_id
                    LEFT JOIN ecocompensation.foncier f
                        ON f.id = p.foncier_id
                    ORDER BY p.created_at DESC
                    """
                )
            ).mappings().all()

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

        buffer_m = r.get("buffer_m")
        buffer_km = (float(buffer_m) / 1000.0) if buffer_m is not None else None
        out.append(
            {
                "id": str(r["id"]),
                "name": r.get("name"),
                "status": r.get("status"),
                "layers_status": r.get("layers_status") or {},
                "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
                "updated_at": r["updated_at"].isoformat() if r.get("updated_at") else None,
                "history": {
                    "buffer_km": buffer_km,
                    "foncier_area_ha": float(r["foncier_area_ha"]) if r.get("foncier_area_ha") is not None else None,
                    "pool_total_count": int(r["pool_total_count"]) if r.get("pool_total_count") is not None else None,
                    "last_filter": last_filter,
                },
            }
        )
    return out


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


def _create_project_from_union_geometry(
    *,
    project_name: str,
    buffer_km: float,
    parcelles_refs: list[dict[str, str]],
    union_geom,
):
    buffer_m = int(buffer_km * 1000)
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
                "wkt": union_geom.buffer(buffer_m, resolution=32).wkt,
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
                INSERT INTO ecocompensation.projects (id, name, aoi_id, foncier_id, status)
                VALUES (:project_id, :name, :aoi_id, :foncier_id, 'created')
                RETURNING id, name, status, created_at
                """
            ),
            {"project_id": project_id, "name": project_name, "aoi_id": aoi_id, "foncier_id": foncier_id},
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
        "ecocompensation_results.unites_foncieres",   # ← nouveau
        "ecocompensation_results.sous_ensembles",     # ← nouveau
        "ecocompensation_results.parcelles_pool_indesirables",
        "ecocompensation_results.parcelles_project_indesirables",
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
            await run_orchestration(
                engine,
                project_id,
                aoi_id,
                lambda d: ws_manager.broadcast(project_id, d),
                layer_keys=req.layers,
                dry_run=req.dry_run,
                uf_max_parcelles=req.uf_max_parcelles,
                uf_min_area_ha=req.uf_min_area_ha,
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
    log_progress = bool(getattr(opts_dto, "log_progress", False))

    ram_before = _get_process_memory_mb()

    progress_cb = None
    if log_progress:
        logger.info("[filter][%s] Démarrage filtrage avec logs de progression activés", project_id)

        def _progress_cb(step_label: str, count: int) -> None:
            logger.info("[filter][%s] %s -> %s parcelles", project_id, step_label, count)

        progress_cb = _progress_cb

    # ── Toute la logique métier dans vrai_filtre.py ──
    result = run_filter_and_score(
        engine,
        project_id,
        aoi_id,
        cx,
        cy,
        options,
        opts_dto,
        progress_callback=progress_cb,
    )
    try:
        run_id = persist_parcelles_pool_run(
            engine,
            project_id=project_id,
            options_json=opts_dto.model_dump(),
            parcelles=result.get("parcelles", []),
            scope="parcelles",
            keep_last=5,
            result_summary={
                "final_radius_km": float(result.get("final_radius_km") or 0),
                "funnel": result.get("funnel") or [],
                "total": int(result.get("total") or 0),
                **(
                    {"pool_build": result["pool_build"]}
                    if isinstance(result.get("pool_build"), dict)
                    else {}
                ),
            },
        )
        result["pool_run_id"] = run_id
    except Exception:
        logger.exception("Persistance pool parcelles échouée")

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

    features: list[dict] = []
    for r in rows:
        idu = r["idu"]
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