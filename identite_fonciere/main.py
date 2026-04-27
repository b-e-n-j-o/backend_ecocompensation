"""
main.py
FastAPI V0 — Identité Foncière France entière.

Endpoint unique : POST /rapport
  Corps : { "parcelles": [...], "options": {...} }
  Réponse : PDF en streaming (application/pdf)
"""
from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
import requests
import psycopg2

from .core.parcelle import ParcelleRef, fetch_parcelles
from .core.unites_foncieres import build_uf, parcelles_detail, uf_geojson, uf_surface_m2
from .core.intersections import compute_intersections
from .core.document_urba.plu_only.reglement_qualite import analyser_qualite_reglement
from .visuels.carte_plu import compute_plu_result, render_plu_map
from .visuels.carte_servitudes import render_servitudes_map
from .visuels.carte_prescriptions import render_prescriptions_map
from .visuels.carte_dpu import compute_dpu_result, render_dpu_map
from .visuels.carte_subdivision import render_subdivision_map
from .visuels.carte_intro import render_intro_map
from .core.subdivision_fiscale.subdivision import compute_subdivision_result
from .pdf.sections.section_servitudes import compute_servitudes_result
from .pdf.sections.section_prescriptions import compute_prescriptions_result
from .pdf.rapport import generate_rapport_pdf

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)
MAX_EMPRISE_SIDE_M = 2000.0
GPU_API = "https://www.geoportail-urbanisme.gouv.fr/api"
WFS_BASE = "https://data.geopf.fr/wfs/ows"
KEYWORDS_OK = ["reglement", "règlement", "regl", "regt"]
KEYWORDS_NOK = ["graphique", "plan", "zonage", "legende", "carte"]
BATCH_REGLEMENT_JOBS: dict[str, dict] = {}

app = FastAPI(
    title="Identité Foncière V0",
    description="Rapport PDF d'identité foncière à partir de références cadastrales (France entière, WFS).",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


# ---------------------------------------------------------------------------
# Schémas Pydantic
# ---------------------------------------------------------------------------

class ParcelleInput(BaseModel):
    section: str = Field(..., example="AC", description="Section cadastrale (ex: AC, AL, B)")
    numero: str = Field(..., example="0042", description="Numéro de parcelle (ex: 42 ou 0042)")
    insee: str = Field(..., example="33522", description="Code INSEE de la commune")
    commune: str = Field(..., example="Latresne", description="Nom de la commune")


class OptionsInput(BaseModel):
    buffer_wfs_m: float = Field(300.0, description="Buffer (m) pour les requêtes WFS GPU")
    generer_carte_plu: bool = Field(True, description="Générer la carte PLU satellite")
    dpi_carte: int = Field(150, description="DPI des cartes PNG")
    layers: Optional[List[str]] = Field(
        None,
        description="Restreindre aux couches GPU (table names). None = toutes.",
    )


class RapportRequest(BaseModel):
    parcelles: List[ParcelleInput] = Field(..., min_length=1, max_length=50)
    options: OptionsInput = Field(default_factory=OptionsInput)


class UrbanDocFile(BaseModel):
    name: str
    url: str
    score_reglement: int


class UrbanDocsResponse(BaseModel):
    insee: str
    commune: str
    idurba: str
    gpu_doc_id: str
    typedoc: str
    files: List[UrbanDocFile]
    reglement_name: Optional[str] = None
    reglement_url: Optional[str] = None
    reglement_qualite_verdict: Optional[str] = None
    reglement_qualite_utilisable: Optional[bool] = None
    reglement_qualite_detail: Optional[str] = None
    reglement_qualite_tokens_estimes: Optional[int] = None


class ReglementExtractibiliteResponse(BaseModel):
    insee: str
    commune: str
    gpu_doc_id: str
    reglement_name: Optional[str] = None
    reglement_url: Optional[str] = None
    reglement_trouve: bool
    extractible: bool
    verdict: Optional[str] = None
    detail: Optional[str] = None
    tokens_estimes: Optional[int] = None


class ReglementExtractibiliteBatchRequest(BaseModel):
    insees: List[str] = Field(..., min_length=1, max_length=2000)


class ReglementExtractibiliteBatchItem(BaseModel):
    insee: str
    commune: Optional[str] = None
    gpu_doc_id: Optional[str] = None
    reglement_name: Optional[str] = None
    reglement_url: Optional[str] = None
    reglement_trouve: bool
    extractible: bool
    verdict: Optional[str] = None
    detail: Optional[str] = None
    tokens_estimes: Optional[int] = None
    erreur: Optional[str] = None
    status_code: Optional[int] = None


class ReglementExtractibiliteBatchResponse(BaseModel):
    total: int
    processed: int
    results: List[ReglementExtractibiliteBatchItem]


class ReglementExtractibiliteBatchJobStartResponse(BaseModel):
    job_id: str
    status: str
    total: int


class ReglementExtractibiliteBatchJobStatusResponse(BaseModel):
    job_id: str
    status: str
    total: int
    processed: int
    started_at: str
    finished_at: Optional[str] = None
    current_insee: Optional[str] = None
    results: List[ReglementExtractibiliteBatchItem]


class CommuneEnBaseItem(BaseModel):
    code_insee: str
    code_dep: str
    nom_commune: Optional[str] = None
    nb_parcelles: int


def _fetch_doc_urba_com_prod(insee: str) -> Optional[dict]:
    resp = requests.get(
        WFS_BASE,
        params={
            "SERVICE": "WFS",
            "VERSION": "2.0.0",
            "REQUEST": "GetFeature",
            "typeNames": "wfs_du:doc_urba_com",
            "outputFormat": "application/json",
            "CQL_FILTER": f"insee='{insee}'",
            "count": "20",
        },
        timeout=20,
    )
    resp.raise_for_status()
    features = resp.json().get("features", [])
    if not features:
        return None
    props = [f.get("properties", {}) for f in features]
    prod = [p for p in props if p.get("gpu_status") == "production"]
    return prod[0] if prod else props[0]


def _score_reglement_filename(filename: str) -> int:
    name = filename.lower()
    score = 0
    for kw in KEYWORDS_OK:
        if kw in name:
            score += 10
    for kw in KEYWORDS_NOK:
        if kw in name:
            score -= 8
    if name.endswith(".pdf"):
        score += 2
    return score


def _identify_reglement(files: dict) -> tuple[Optional[str], Optional[str], list[UrbanDocFile]]:
    scored_files: list[UrbanDocFile] = []
    for name, url in files.items():
        scored_files.append(
            UrbanDocFile(name=name, url=url, score_reglement=_score_reglement_filename(name))
        )
    scored_files.sort(key=lambda x: x.score_reglement, reverse=True)

    if not scored_files or scored_files[0].score_reglement <= 0:
        return None, None, scored_files
    best = scored_files[0]
    return best.name, best.url, scored_files


def _analyze_reglement_from_url(reglement_url: str) -> Optional[dict]:
    """
    Télécharge le PDF règlement et retourne un résumé qualité.
    L'analyse est volontairement best-effort : en cas d'échec,
    on ne bloque pas l'endpoint /urban-documents.
    """
    if not reglement_url:
        return None
    try:
        resp = requests.get(reglement_url, timeout=60)
        resp.raise_for_status()
        pdf_bytes = resp.content
        if pdf_bytes[:4] != b"%PDF":
            return {
                "verdict": "INVALIDE",
                "utilisable": False,
                "detail": "Fichier téléchargé non reconnu comme PDF",
                "tokens_estimes": 0,
            }
        q = analyser_qualite_reglement(pdf_bytes)
        return {
            "verdict": q.verdict,
            "utilisable": q.utilisable,
            "detail": q.detail,
            "tokens_estimes": q.tokens_estimes,
        }
    except Exception as exc:
        logger.warning("Analyse qualité règlement impossible (%s): %s", reglement_url, exc)
        return {
            "verdict": "ERREUR_ANALYSE",
            "utilisable": False,
            "detail": f"Analyse impossible: {exc}",
            "tokens_estimes": 0,
        }


def _get_reglement_analysis_for_insee(insee: str) -> dict:
    """
    Récupère le règlement élu d'une commune et son analyse d'extractibilité.
    """
    props = _fetch_doc_urba_com_prod(insee)
    if not props:
        raise HTTPException(status_code=404, detail=f"Aucun document GPU pour INSEE {insee}")

    gpu_doc_id = str(props.get("gpu_doc_id") or "")
    if not gpu_doc_id:
        raise HTTPException(
            status_code=404,
            detail=f"gpu_doc_id absent pour INSEE {insee}",
        )

    details_resp = requests.get(f"{GPU_API}/document/{gpu_doc_id}/details", timeout=20)
    details_resp.raise_for_status()
    details = details_resp.json()
    writing_materials = details.get("writingMaterials", {}) or {}
    reglement_name, reglement_url, files = _identify_reglement(writing_materials)
    reglement_qualite = _analyze_reglement_from_url(reglement_url) if reglement_url else None

    return {
        "props": props,
        "details": details,
        "gpu_doc_id": gpu_doc_id,
        "files": files,
        "reglement_name": reglement_name,
        "reglement_url": reglement_url,
        "reglement_qualite": reglement_qualite,
    }


# ---------------------------------------------------------------------------
# Endpoint principal
# ---------------------------------------------------------------------------

@app.post(
    "/rapport",
    response_class=FileResponse,
    summary="Générer un rapport PDF d'identité foncière",
    responses={
        200: {"content": {"application/pdf": {}}, "description": "Rapport PDF généré"},
        400: {"description": "Paramètres invalides ou parcelles introuvables"},
        500: {"description": "Erreur serveur"},
    },
)
async def generer_rapport(body: RapportRequest):
    """
    Génère un rapport PDF d'identité foncière pour une ou plusieurs parcelles.

    Le rapport contient :
    - Page de garde (commune, références, surface UF, zonage PLU principal)
    - Page cartographique PLU (fond satellite + zonages colorés)
    - Corps réglementaire par article (servitudes, prescriptions, préemption…)
    """
    refs = [
        ParcelleRef(
            section=p.section,
            numero=p.numero,
            insee=p.insee,
            commune=p.commune,
        )
        for p in body.parcelles
    ]
    opts = body.options

    # 1. Récupération des géométries cadastrales (base interne)
    logger.info("🚀 Pipeline — %d parcelle(s)", len(refs))
    parc_results = fetch_parcelles(refs)

    ok = [r for r in parc_results if r.ok]
    if not ok:
        errors = [r.error for r in parc_results if r.error]
        raise HTTPException(
            status_code=400,
            detail=f"Aucune parcelle récupérée. Erreurs : {errors}",
        )

    if len(ok) < len(refs):
        failed = [r.ref.label for r in parc_results if not r.ok]
        logger.warning("⚠️  Parcelles non récupérées : %s", failed)

    # 2. Construction UF
    try:
        uf_gdf = build_uf(parc_results)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Garde-fou performance : l'emprise des parcelles ne doit pas dépasser 2 km x 2 km.
    # On mesure la bbox de l'UF en mètres (Web Mercator).
    if len(ok) > 1:
        minx, miny, maxx, maxy = uf_gdf.to_crs(3857).total_bounds
        width_m = float(maxx - minx)
        height_m = float(maxy - miny)
        if width_m > MAX_EMPRISE_SIDE_M or height_m > MAX_EMPRISE_SIDE_M:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Les parcelles sélectionnées couvrent une emprise trop grande "
                    f"({width_m:.0f} m x {height_m:.0f} m). "
                    "La limite est de 2000 m x 2000 m."
                ),
            )

    surface = uf_surface_m2(uf_gdf)
    geom = uf_geojson(uf_gdf)
    pd_list = parcelles_detail(parc_results)

    # 3. Intersections WFS GPU
    try:
        intersections, plu_pct_stats = compute_intersections(
            uf_gdf,
            buffer_m=opts.buffer_wfs_m,
            layers=opts.layers,
        )
    except Exception as e:
        logger.error("Erreur intersections WFS : %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Erreur WFS : {e}")

    commune = refs[0].commune
    insee = refs[0].insee

    result = {
        "parcelle": ", ".join(r.ref.label for r in ok),
        "commune": commune,
        "insee": insee,
        "nb_intersections": len(intersections),
        "intersections": intersections,
        "surface_uf_m2": round(surface, 2),
        "geometry": geom,
        "parcelles_cadastrales": [
            {"section": r.ref.section, "numero": r.ref.numero}
            for r in ok
        ],
        "parcelles_uf_detail": pd_list,
    }

    # 4. Carte PLU
    with tempfile.TemporaryDirectory() as tmpdir:
        plu_map_png: Optional[str] = None
        plu_result: Optional[dict] = None
        servitudes_map_png: Optional[str] = None
        servitudes_result: Optional[dict] = None
        prescriptions_map_png: Optional[str] = None
        prescriptions_result: Optional[dict] = None
        dpu_map_png: Optional[str] = None
        dpu_result: Optional[dict] = None
        subdivision_map_png: Optional[str] = None
        subdivision_result: Optional[dict] = None
        intro_map_png: Optional[str] = None

        # 4a. Carte de garde (UF + limites/numéros de parcelles)
        try:
            intro_map_png = str(Path(tmpdir) / "intro_map.png")
            render_intro_map(
                uf_gdf=uf_gdf,
                parcelle_results=ok,
                out_path=intro_map_png,
                dpi=opts.dpi_carte,
            )
        except Exception as e:
            logger.warning("⚠️  Carte intro non générée : %s", e)
            intro_map_png = None

        # 4. Données + carte PLU (section dédiée toujours présente)
        try:
            from .core.gpu_wfs import GPU_LAYERS_BY_TABLE, _fetch_layer
            from .utils.geo import gdf_bbox_4326
            import geopandas as gpd

            plu_cfg = GPU_LAYERS_BY_TABLE.get("zone_urba", {})
            bbox = gdf_bbox_4326(uf_gdf, buffer_m=50.0)
            plu_lr = _fetch_layer(plu_cfg, bbox, timeout=30)
            plu_gdf = plu_lr.gdf if plu_lr.ok else gpd.GeoDataFrame()

            plu_result = compute_plu_result(
                uf_gdf=uf_gdf,
                plu_gdf=plu_gdf,
                parcelle_results=ok,
            )

            if opts.generer_carte_plu and bool(plu_result.get("intersecte", False)):
                plu_png_path = str(Path(tmpdir) / "plu_map.png")
                render_plu_map(
                    uf_gdf=uf_gdf,
                    plu_gdf=plu_gdf,
                    pct_stats=plu_pct_stats,
                    out_path=plu_png_path,
                    parcelle_results=ok,
                    dpi=opts.dpi_carte,
                )
                plu_map_png = plu_png_path
        except Exception as e:
            logger.warning("⚠️  Données/carte PLU non générées : %s", e)
            plu_result = {
                "intersecte": False,
                "zonages": [],
                "uf_repartition": [],
                "parcelles_repartition": [],
            }
            plu_map_png = None

        # 4bis. Données + carte servitudes (section dédiée toujours présente)
        try:
            from .core.gpu_wfs import GPU_LAYERS, _fetch_layer
            from .utils.geo import gdf_bbox_4326
            import geopandas as gpd

            bbox_sup = gdf_bbox_4326(uf_gdf, buffer_m=opts.buffer_wfs_m)
            sup_gdfs: dict[str, gpd.GeoDataFrame] = {}
            for cfg in GPU_LAYERS:
                if str(cfg.get("article", "")) != "4":
                    continue
                lr = _fetch_layer(cfg, bbox_sup, timeout=30)
                sup_gdfs[cfg["table"]] = lr.gdf if lr.ok else gpd.GeoDataFrame()

            servitudes_result = compute_servitudes_result(
                uf_gdf=uf_gdf,
                parcelle_results=ok,
                sup_gdfs=sup_gdfs,
                intersections=intersections,
            )

            if bool(servitudes_result.get("intersecte", False)):
                servitudes_map_png = str(Path(tmpdir) / "servitudes_map.png")
                render_servitudes_map(
                    uf_gdf=uf_gdf,
                    sup_gdfs=sup_gdfs,
                    out_path=servitudes_map_png,
                    parcelle_results=ok,
                    buffer_m=300.0,
                    dpi=opts.dpi_carte,
                )
        except Exception as e:
            logger.warning("⚠️  Données/carte servitudes non générées : %s", e)
            servitudes_map_png = None
            servitudes_result = {
                "intersecte": False,
                "attributs": [],
                "uf_repartition": [],
                "parcelles_repartition": [],
            }

        # 4bis2. Prescriptions PLU (surf + lin + pct) — section dédiée + carte unique si intersection
        try:
            from .core.gpu_wfs import GPU_LAYERS, _fetch_layer
            from .utils.geo import gdf_bbox_4326, intersects_gdf
            import geopandas as gpd

            bbox_psc = gdf_bbox_4326(uf_gdf, buffer_m=opts.buffer_wfs_m)
            pres_gdfs: dict[str, gpd.GeoDataFrame] = {}
            for cfg in GPU_LAYERS:
                tbl = str(cfg.get("table") or "")
                if tbl not in ("prescription_surf", "prescription_lin", "prescription_pct"):
                    continue
                lr = _fetch_layer(cfg, bbox_psc, timeout=30)
                raw = lr.gdf if lr.ok else gpd.GeoDataFrame()
                pres_gdfs[tbl] = intersects_gdf(uf_gdf, raw) if not raw.empty else gpd.GeoDataFrame()

            prescriptions_result = compute_prescriptions_result(
                intersections=intersections,
                pres_gdfs=pres_gdfs,
            )
            if bool(prescriptions_result.get("intersecte", False)):
                prescriptions_map_png = str(Path(tmpdir) / "prescriptions_map.png")
                render_prescriptions_map(
                    uf_gdf=uf_gdf,
                    pres_gdfs=pres_gdfs,
                    out_path=prescriptions_map_png,
                    parcelle_results=ok,
                    buffer_m=300.0,
                    dpi=opts.dpi_carte,
                )
        except Exception as e:
            logger.warning("⚠️  Données/carte prescriptions non générées : %s", e)
            prescriptions_map_png = None
            prescriptions_result = {"intersecte": False, "attributs": []}

        # 4ter. Carte DPU (générée seulement si soumise)
        try:
            dpu_result = compute_dpu_result(uf_gdf, buffer_m=300.0, intersections=intersections)
            if bool(dpu_result.get("intersecte", False)):
                dpu_map_png = str(Path(tmpdir) / "dpu_map.png")
                render_dpu_map(
                    uf_gdf=uf_gdf,
                    dpu_gdf=dpu_result["dpu_gdf"],
                    out_path=dpu_map_png,
                    intersecte=dpu_result["intersecte"],
                    parcelle_results=ok,
                    dpi=opts.dpi_carte,
                )
        except Exception as e:
            logger.warning("⚠️  Carte DPU non générée : %s", e)
            dpu_map_png = None
            dpu_result = None

        # 4quater. Carte subdivision fiscale (générée seulement si subdivisée)
        try:
            subdivision_result = compute_subdivision_result(uf_gdf, ok)
            if bool(subdivision_result.get("subdivisee", False)):
                subdivision_map_png = str(Path(tmpdir) / "subdivision_map.png")
                render_subdivision_map(
                    uf_gdf=uf_gdf,
                    subdivisions_gdf=subdivision_result["subdivisions_gdf"],
                    out_path=subdivision_map_png,
                    subdivisee=subdivision_result["subdivisee"],
                    parcelle_results=ok,
                    dpi=opts.dpi_carte,
                )
        except Exception as e:
            logger.warning("⚠️  Carte subdivision non générée : %s", e)
            subdivision_map_png = None
            subdivision_result = None

        # 5. Génération PDF
        try:
            pdf_path = generate_rapport_pdf(
                result,
                output_dir=tmpdir,
                plu_map_png=plu_map_png,
                plu_result=plu_result,
                servitudes_map_png=servitudes_map_png,
                servitudes_result=servitudes_result,
                prescriptions_map_png=prescriptions_map_png,
                prescriptions_result=prescriptions_result,
                dpu_map_png=dpu_map_png,
                dpu_result=dpu_result,
                subdivision_map_png=subdivision_map_png,
                subdivision_result=subdivision_result,
                intro_map_png=intro_map_png,
            )
        except Exception as e:
            logger.error("Erreur génération PDF : %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Erreur PDF : {e}")

        # Copie dans un fichier persistant (tmpdir sera supprimé)
        safe_commune = commune.replace(" ", "_").lower()
        from datetime import datetime
        date_file = datetime.now().strftime("%Y%m%d_%H%M%S")
        final_name = f"identite_fonciere_{safe_commune}_{date_file}.pdf"
        final_path = Path(tempfile.gettempdir()) / final_name
        import shutil
        shutil.copy2(pdf_path, final_path)

    logger.info("✅ Rapport prêt : %s", final_path)
    return FileResponse(
        path=str(final_path),
        media_type="application/pdf",
        filename=final_name,
        headers={"Content-Disposition": f'attachment; filename="{final_name}"'},
    )


@app.get(
    "/communes-en-base",
    response_model=List[CommuneEnBaseItem],
    summary="Lister les communes cadastrales déjà en base",
)
async def get_communes_en_base(
    q: str | None = None,
    limit: int = 2000,
):
    """
    Retourne la liste des communes présentes dans `parcelles.communes`.
    - `q` : filtre optionnel sur code INSEE / nom commune
    - `limit` : borne max (1..10000)
    """
    if limit < 1:
        raise HTTPException(status_code=400, detail="limit doit être >= 1")
    limit = min(limit, 10000)

    direct_url = (os.getenv("SUPABASE_DIRECT_URL") or "").strip()
    if not direct_url:
        raise HTTPException(
            status_code=500,
            detail="SUPABASE_DIRECT_URL manquant pour lire parcelles.communes",
        )

    try:
        with psycopg2.connect(direct_url, sslmode="require") as conn:
            with conn.cursor() as cur:
                if q and q.strip():
                    pattern = f"%{q.strip()}%"
                    cur.execute(
                        """
                        SELECT code_insee, code_dep, nom_commune, nb_parcelles
                        FROM parcelles.communes
                        WHERE code_insee ILIKE %s
                           OR COALESCE(nom_commune, '') ILIKE %s
                        ORDER BY code_dep, nom_commune NULLS LAST, code_insee
                        LIMIT %s
                        """,
                        (pattern, pattern, limit),
                    )
                else:
                    cur.execute(
                        """
                        SELECT code_insee, code_dep, nom_commune, nb_parcelles
                        FROM parcelles.communes
                        ORDER BY code_dep, nom_commune NULLS LAST, code_insee
                        LIMIT %s
                        """,
                        (limit,),
                    )
                rows = cur.fetchall()
    except Exception as e:
        logger.error("Erreur GET /communes-en-base: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Erreur communes-en-base: {e}")

    return [
        CommuneEnBaseItem(
            code_insee=r[0],
            code_dep=r[1],
            nom_commune=r[2],
            nb_parcelles=int(r[3]),
        )
        for r in rows
    ]


@app.get(
    "/urban-documents/{insee}",
    response_model=UrbanDocsResponse,
    summary="Lister les documents d'urbanisme d'une commune et identifier le règlement",
)
async def get_urban_documents(insee: str):
    try:
        analysis = _get_reglement_analysis_for_insee(insee)
        props = analysis["props"]
        details = analysis["details"]
        gpu_doc_id = analysis["gpu_doc_id"]
        files = analysis["files"]
        reglement_name = analysis["reglement_name"]
        reglement_url = analysis["reglement_url"]
        reglement_qualite = analysis["reglement_qualite"]

        return UrbanDocsResponse(
            insee=insee,
            commune=str(props.get("libelle") or details.get("title") or insee),
            idurba=str(props.get("idurba") or ""),
            gpu_doc_id=gpu_doc_id,
            typedoc=str(details.get("type") or props.get("typedoc") or ""),
            files=files,
            reglement_name=reglement_name,
            reglement_url=reglement_url,
            reglement_qualite_verdict=(reglement_qualite or {}).get("verdict"),
            reglement_qualite_utilisable=(reglement_qualite or {}).get("utilisable"),
            reglement_qualite_detail=(reglement_qualite or {}).get("detail"),
            reglement_qualite_tokens_estimes=(reglement_qualite or {}).get("tokens_estimes"),
        )
    except HTTPException:
        raise
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else 502
        raise HTTPException(status_code=status, detail=f"Erreur API GPU ({status})")
    except Exception as e:
        logger.error("Erreur urban-documents insee=%s: %s", insee, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Erreur urban-documents: {e}")


@app.get(
    "/urban-documents/{insee}/reglement-extractibilite",
    response_model=ReglementExtractibiliteResponse,
    summary="Déterminer si le règlement PLU élu est textuellement extractible",
)
async def get_reglement_extractibilite(insee: str):
    try:
        analysis = _get_reglement_analysis_for_insee(insee)
        props = analysis["props"]
        details = analysis["details"]
        reglement_name = analysis["reglement_name"]
        reglement_url = analysis["reglement_url"]
        reglement_qualite = analysis["reglement_qualite"] or {}
        verdict = reglement_qualite.get("verdict")
        utilisable = bool(reglement_qualite.get("utilisable"))

        return ReglementExtractibiliteResponse(
            insee=insee,
            commune=str(props.get("libelle") or details.get("title") or insee),
            gpu_doc_id=analysis["gpu_doc_id"],
            reglement_name=reglement_name,
            reglement_url=reglement_url,
            reglement_trouve=bool(reglement_url),
            extractible=utilisable,
            verdict=verdict,
            detail=reglement_qualite.get("detail"),
            tokens_estimes=reglement_qualite.get("tokens_estimes"),
        )
    except HTTPException:
        raise
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else 502
        raise HTTPException(status_code=status, detail=f"Erreur API GPU ({status})")
    except Exception as e:
        logger.error("Erreur reglement-extractibilite insee=%s: %s", insee, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Erreur reglement-extractibilite: {e}")


@app.post(
    "/urban-documents/reglement-extractibilite/batch",
    response_model=ReglementExtractibiliteBatchResponse,
    summary="Analyser l'extractibilité du règlement PLU pour une liste d'INSEE",
)
async def post_reglement_extractibilite_batch(body: ReglementExtractibiliteBatchRequest):
    results: list[ReglementExtractibiliteBatchItem] = []
    insees = [str(insee).strip() for insee in body.insees if str(insee).strip()]

    for insee in insees:
        try:
            analysis = _get_reglement_analysis_for_insee(insee)
            props = analysis["props"]
            details = analysis["details"]
            reglement_name = analysis["reglement_name"]
            reglement_url = analysis["reglement_url"]
            reglement_qualite = analysis["reglement_qualite"] or {}
            verdict = reglement_qualite.get("verdict")
            utilisable = bool(reglement_qualite.get("utilisable"))

            results.append(
                ReglementExtractibiliteBatchItem(
                    insee=insee,
                    commune=str(props.get("libelle") or details.get("title") or insee),
                    gpu_doc_id=analysis["gpu_doc_id"],
                    reglement_name=reglement_name,
                    reglement_url=reglement_url,
                    reglement_trouve=bool(reglement_url),
                    extractible=utilisable,
                    verdict=verdict,
                    detail=reglement_qualite.get("detail"),
                    tokens_estimes=reglement_qualite.get("tokens_estimes"),
                )
            )
        except HTTPException as e:
            results.append(
                ReglementExtractibiliteBatchItem(
                    insee=insee,
                    reglement_trouve=False,
                    extractible=False,
                    verdict="ERREUR_ENDPOINT",
                    detail=str(e.detail),
                    erreur=str(e.detail),
                    status_code=e.status_code,
                )
            )
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else 502
            msg = f"Erreur API GPU ({status})"
            results.append(
                ReglementExtractibiliteBatchItem(
                    insee=insee,
                    reglement_trouve=False,
                    extractible=False,
                    verdict="ERREUR_ENDPOINT",
                    detail=msg,
                    erreur=msg,
                    status_code=status,
                )
            )
        except Exception as e:
            logger.error("Erreur batch reglement-extractibilite insee=%s: %s", insee, e, exc_info=True)
            msg = f"Erreur reglement-extractibilite: {e}"
            results.append(
                ReglementExtractibiliteBatchItem(
                    insee=insee,
                    reglement_trouve=False,
                    extractible=False,
                    verdict="ERREUR_ENDPOINT",
                    detail=msg,
                    erreur=msg,
                    status_code=500,
                )
            )

    return ReglementExtractibiliteBatchResponse(
        total=len(insees),
        processed=len(results),
        results=results,
    )


def _run_reglement_extractibilite_batch_job(job_id: str, insees: list[str]) -> None:
    job = BATCH_REGLEMENT_JOBS[job_id]
    job["status"] = "running"
    for insee in insees:
        job["current_insee"] = insee
        try:
            analysis = _get_reglement_analysis_for_insee(insee)
            props = analysis["props"]
            details = analysis["details"]
            reglement_name = analysis["reglement_name"]
            reglement_url = analysis["reglement_url"]
            reglement_qualite = analysis["reglement_qualite"] or {}
            verdict = reglement_qualite.get("verdict")
            utilisable = bool(reglement_qualite.get("utilisable"))

            item = ReglementExtractibiliteBatchItem(
                insee=insee,
                commune=str(props.get("libelle") or details.get("title") or insee),
                gpu_doc_id=analysis["gpu_doc_id"],
                reglement_name=reglement_name,
                reglement_url=reglement_url,
                reglement_trouve=bool(reglement_url),
                extractible=utilisable,
                verdict=verdict,
                detail=reglement_qualite.get("detail"),
                tokens_estimes=reglement_qualite.get("tokens_estimes"),
            )
        except HTTPException as e:
            item = ReglementExtractibiliteBatchItem(
                insee=insee,
                reglement_trouve=False,
                extractible=False,
                verdict="ERREUR_ENDPOINT",
                detail=str(e.detail),
                erreur=str(e.detail),
                status_code=e.status_code,
            )
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else 502
            msg = f"Erreur API GPU ({status})"
            item = ReglementExtractibiliteBatchItem(
                insee=insee,
                reglement_trouve=False,
                extractible=False,
                verdict="ERREUR_ENDPOINT",
                detail=msg,
                erreur=msg,
                status_code=status,
            )
        except Exception as e:
            logger.error("Erreur batch job reglement-extractibilite insee=%s: %s", insee, e, exc_info=True)
            msg = f"Erreur reglement-extractibilite: {e}"
            item = ReglementExtractibiliteBatchItem(
                insee=insee,
                reglement_trouve=False,
                extractible=False,
                verdict="ERREUR_ENDPOINT",
                detail=msg,
                erreur=msg,
                status_code=500,
            )

        job["results"].append(item)
        job["processed"] += 1
        logger.info(
            "Batch job %s: %d/%d - INSEE %s - verdict=%s",
            job_id,
            job["processed"],
            job["total"],
            insee,
            item.verdict,
        )

    job["status"] = "done"
    job["current_insee"] = None
    job["finished_at"] = datetime.utcnow().isoformat() + "Z"


@app.post(
    "/urban-documents/reglement-extractibilite/batch/jobs",
    response_model=ReglementExtractibiliteBatchJobStartResponse,
    summary="Lancer un batch asynchrone d'extractibilité des règlements PLU",
)
async def start_reglement_extractibilite_batch_job(
    body: ReglementExtractibiliteBatchRequest,
    background_tasks: BackgroundTasks,
):
    insees = [str(insee).strip() for insee in body.insees if str(insee).strip()]
    if not insees:
        raise HTTPException(status_code=400, detail="Aucun code INSEE fourni")

    job_id = uuid4().hex
    BATCH_REGLEMENT_JOBS[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "total": len(insees),
        "processed": 0,
        "started_at": datetime.utcnow().isoformat() + "Z",
        "finished_at": None,
        "current_insee": None,
        "results": [],
    }
    background_tasks.add_task(_run_reglement_extractibilite_batch_job, job_id, insees)
    return ReglementExtractibiliteBatchJobStartResponse(
        job_id=job_id,
        status="queued",
        total=len(insees),
    )


@app.get(
    "/urban-documents/reglement-extractibilite/batch/jobs/{job_id}",
    response_model=ReglementExtractibiliteBatchJobStatusResponse,
    summary="Consulter l'avancement d'un batch asynchrone d'extractibilité",
)
async def get_reglement_extractibilite_batch_job(job_id: str):
    job = BATCH_REGLEMENT_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"job_id inconnu: {job_id}")
    return ReglementExtractibiliteBatchJobStatusResponse(
        job_id=job["job_id"],
        status=job["status"],
        total=job["total"],
        processed=job["processed"],
        started_at=job["started_at"],
        finished_at=job["finished_at"],
        current_insee=job["current_insee"],
        results=job["results"],
    )


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "version": app.version}


@app.get("/")
async def root():
    return {
        "service": "Identité Foncière V0",
        "docs": "/docs",
        "endpoint": "POST /rapport",
    }
