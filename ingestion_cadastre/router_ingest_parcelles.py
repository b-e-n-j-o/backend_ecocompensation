"""
router_ingest_parcelles.py

Endpoint FastAPI d'ingestion des parcelles cadastrales Etalab.
À monter sur le backend Render — le fetch Etalab et l'upsert Supabase
se font entièrement côté serveur, sans transit par le Mac.

Montage dans main.py :
    from router_ingest_parcelles import router as parcelles_router
    app.include_router(parcelles_router, prefix="/admin")

Endpoints :
    POST /admin/parcelles/ingest
    Body: {
        "communes":     ["33234", "33063"],   # option A
        "departements": ["33", "17"],          # option B
        "nouvelle_aquitaine": true             # option C (shortcut)
    }

    GET /admin/parcelles/status/{job_id}       # suivre l'avancement
"""

import os
import asyncio
import uuid
import time
import requests
import geopandas as gpd
from datetime import datetime
from shapely.geometry import shape
from typing import Optional
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

# ─── CONFIG ───────────────────────────────────────────────────────────────────

DB_URL = (
    f"postgresql://{os.getenv('SUPABASE_USER')}:{os.getenv('SUPABASE_PASSWORD')}"
    f"@{os.getenv('SUPABASE_HOST')}:{os.getenv('SUPABASE_PORT', '5432')}/{os.getenv('SUPABASE_DB', 'postgres')}"
)

ETALAB_COMMUNE    = "https://cadastre.data.gouv.fr/bundler/cadastre-etalab/communes/{insee}/geojson/parcelles"
ETALAB_DEP        = "https://cadastre.data.gouv.fr/bundler/cadastre-etalab/departements/{dep}/geojson/parcelles"

TARGET_TABLE      = "parcelles.parcelles"
BATCH_SIZE        = 1500
PAUSE_BETWEEN_DEP = 1.0  # secondes entre départements

NOUVELLE_AQUITAINE = ["16", "17", "19", "23", "24", "33", "40", "47", "64", "79", "86", "87"]

# Store en mémoire des jobs (en prod : remplacer par Redis ou table Supabase)
JOBS: dict[str, dict] = {}

# ─── ROUTER ───────────────────────────────────────────────────────────────────

router = APIRouter(tags=["parcelles"])


class IngestRequest(BaseModel):
    communes:            Optional[list[str]] = None   # codes INSEE ["33234", ...]
    departements:        Optional[list[str]] = None   # codes dep ["33", "17"]
    nouvelle_aquitaine:  bool = False                 # shortcut
    dry_run:             bool = False


class JobStatus(BaseModel):
    job_id:       str
    status:       str   # pending | running | done | error
    started_at:   Optional[str]
    finished_at:  Optional[str]
    communes_done: int
    communes_total: int
    parcelles_upserted: int
    errors:       list[str]
    log:          list[str]


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def get_engine():
    return create_engine(
        DB_URL,
        poolclass=NullPool,
        connect_args={"sslmode": "require"},
    )


def fetch_commune_geojson(insee: str) -> list[dict] | None:
    try:
        r = requests.get(ETALAB_COMMUNE.format(insee=insee), timeout=120)
        r.raise_for_status()
        return r.json().get("features", [])
    except Exception as e:
        return None


def fetch_dep_geojson(dep: str) -> list[dict] | None:
    try:
        r = requests.get(ETALAB_DEP.format(dep=dep), timeout=600)
        r.raise_for_status()
        return r.json().get("features", [])
    except Exception as e:
        return None


def features_to_rows(features: list[dict]) -> list[dict]:
    rows = []
    for f in features:
        p = f["properties"]
        insee = p.get("commune", "")
        try:
            geom_4326 = shape(f["geometry"])
            # Reprojection via GeoDataFrame (une seule feature)
            gdf = gpd.GeoDataFrame([{"geometry": geom_4326}], crs="EPSG:4326")
            geom_2154 = gdf.to_crs("EPSG:2154").geometry[0]
            geom_3857 = gdf.to_crs("EPSG:3857").geometry[0]
        except Exception:
            continue

        rows.append({
            "idu":        p.get("id"),
            "code_dep":   insee[:2] if len(insee) >= 2 else None,
            "code_insee": insee,
            "section":    p.get("section"),
            "numero":     p.get("numero"),
            "feuille":    None,
            "com_abs":    p.get("prefixe"),
            "contenance": p.get("contenance"),
            "arpente":    p.get("arpente"),
            "updated":    p.get("updated"),
            "geom_2154":  geom_2154.wkt if geom_2154 else None,
            "geom_3857":  geom_3857.wkt if geom_3857 else None,
        })
    return rows


UPSERT_SQL = text(f"""
    INSERT INTO {TARGET_TABLE}
        (idu, code_dep, code_insee, section, numero, feuille, com_abs,
         contenance, arpente, updated, geom_2154, geom_3857)
    VALUES
        (:idu, :code_dep, :code_insee, :section, :numero, :feuille, :com_abs,
         :contenance, :arpente, :updated,
         ST_GeomFromText(:geom_2154, 2154),
         ST_GeomFromText(:geom_3857, 3857))
    ON CONFLICT (idu, code_dep) DO UPDATE SET
        section    = EXCLUDED.section,
        numero     = EXCLUDED.numero,
        com_abs    = EXCLUDED.com_abs,
        contenance = EXCLUDED.contenance,
        arpente    = EXCLUDED.arpente,
        updated    = EXCLUDED.updated,
        geom_2154  = EXCLUDED.geom_2154,
        geom_3857  = EXCLUDED.geom_3857,
        ingere_le  = now()
""")


def upsert_rows(rows: list[dict], engine, dry_run: bool) -> tuple[int, int]:
    """Retourne (upserted, errors)"""
    if dry_run or not rows:
        return len(rows), 0

    upserted = 0
    errors   = 0
    with engine.begin() as conn:
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i:i + BATCH_SIZE]
            try:
                conn.execute(UPSERT_SQL, batch)
                upserted += len(batch)
            except Exception as e:
                errors += len(batch)
    return upserted, errors


# ─── JOB BACKGROUND ───────────────────────────────────────────────────────────

def resolve_communes(req: IngestRequest) -> tuple[list[str], str]:
    """
    Retourne (liste_insee, mode)
    mode: 'commune' | 'departement'
    """
    if req.nouvelle_aquitaine:
        return NOUVELLE_AQUITAINE, "departement"
    if req.departements:
        return req.departements, "departement"
    if req.communes:
        return req.communes, "commune"
    raise ValueError("Aucune cible spécifiée")


def run_ingest_job(job_id: str, req: IngestRequest):
    job = JOBS[job_id]
    job["status"]     = "running"
    job["started_at"] = datetime.utcnow().isoformat()

    engine = get_engine()

    try:
        targets, mode = resolve_communes(req)
    except ValueError as e:
        job["status"] = "error"
        job["errors"].append(str(e))
        return

    job["communes_total"] = len(targets)
    job["log"].append(f"Mode: {mode} — {len(targets)} cibles — dry_run={req.dry_run}")

    for i, target in enumerate(targets):
        label = f"[{i+1}/{len(targets)}] {mode} {target}"
        job["log"].append(f"{label} — fetch...")

        if mode == "departement":
            features = fetch_dep_geojson(target)
        else:
            features = fetch_commune_geojson(target)

        if features is None:
            msg = f"{label} — ❌ fetch échoué"
            job["log"].append(msg)
            job["errors"].append(msg)
            job["communes_done"] += 1
            continue

        job["log"].append(f"{label} — {len(features)} features, conversion...")
        rows = features_to_rows(features)

        upserted, errs = upsert_rows(rows, engine, req.dry_run)
        job["parcelles_upserted"] += upserted
        job["communes_done"]      += 1

        suffix = "(dry run)" if req.dry_run else f"→ {upserted} upsertées"
        job["log"].append(f"{label} — ✅ {len(rows)} parcelles {suffix}")

        if mode == "departement" and i < len(targets) - 1:
            time.sleep(PAUSE_BETWEEN_DEP)

    job["status"]      = "done"
    job["finished_at"] = datetime.utcnow().isoformat()
    job["log"].append(f"✅ Terminé — {job['parcelles_upserted']} parcelles upsertées")


# ─── ENDPOINTS ────────────────────────────────────────────────────────────────

@router.post("/parcelles/ingest")
async def ingest_parcelles(req: IngestRequest, background_tasks: BackgroundTasks):
    """
    Lance une ingestion en background.
    Exemples de body :

    # Une ou plusieurs communes
    {"communes": ["33234", "33063"]}

    # Un ou plusieurs départements
    {"departements": ["33", "17"]}

    # Toute la Nouvelle-Aquitaine
    {"nouvelle_aquitaine": true}

    # Dry run (pas d'insert)
    {"departements": ["33"], "dry_run": true}
    """
    if not any([req.communes, req.departements, req.nouvelle_aquitaine]):
        raise HTTPException(400, "Spécifier communes, departements ou nouvelle_aquitaine=true")

    job_id = str(uuid.uuid4())[:8]
    JOBS[job_id] = {
        "job_id":             job_id,
        "status":             "pending",
        "started_at":         None,
        "finished_at":        None,
        "communes_done":      0,
        "communes_total":     0,
        "parcelles_upserted": 0,
        "errors":             [],
        "log":                [],
    }

    background_tasks.add_task(run_ingest_job, job_id, req)

    return {
        "job_id":  job_id,
        "message": "Ingestion lancée en background",
        "status_url": f"/admin/parcelles/status/{job_id}",
    }


@router.get("/parcelles/status/{job_id}", response_model=JobStatus)
async def get_job_status(job_id: str):
    """Suivre l'avancement d'un job d'ingestion."""
    if job_id not in JOBS:
        raise HTTPException(404, f"Job {job_id} introuvable")
    return JOBS[job_id]


@router.get("/parcelles/jobs")
async def list_jobs():
    """Lister tous les jobs en cours ou terminés."""
    return [
        {k: v for k, v in job.items() if k != "log"}
        for job in JOBS.values()
    ]