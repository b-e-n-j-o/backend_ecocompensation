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
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .core.parcelle import ParcelleRef, fetch_parcelles
from .core.unites_foncieres import build_uf, parcelles_detail, uf_geojson, uf_surface_m2
from .core.intersections import compute_intersections
from .visuels.carte_plu import render_plu_map
from .pdf.rapport import generate_rapport_pdf

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

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

    # 1. Récupération des géométries IGN
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

        if opts.generer_carte_plu and plu_pct_stats:
            # Récupère le GeoDataFrame PLU depuis les intersections
            from .core.gpu_wfs import GPU_LAYERS_BY_TABLE
            import geopandas as gpd
            import io
            import requests

            plu_cfg = GPU_LAYERS_BY_TABLE.get("zone_urba", {})
            # Re-fetch PLU pour avoir la géométrie (intersections ne la stocke pas)
            from .utils.geo import gdf_bbox_4326
            from .core.gpu_wfs import _fetch_layer
            bbox = gdf_bbox_4326(uf_gdf, buffer_m=50.0)
            plu_lr = _fetch_layer(plu_cfg, bbox, timeout=30)

            plu_gdf = plu_lr.gdf if plu_lr.ok else gpd.GeoDataFrame()

            try:
                plu_png_path = str(Path(tmpdir) / "plu_map.png")
                render_plu_map(
                    uf_gdf, plu_gdf, plu_pct_stats,
                    plu_png_path, dpi=opts.dpi_carte,
                )
                plu_map_png = plu_png_path
            except Exception as e:
                logger.warning("⚠️  Carte PLU non générée : %s", e)

        # 5. Génération PDF
        try:
            pdf_path = generate_rapport_pdf(
                result,
                output_dir=tmpdir,
                plu_map_png=plu_map_png,
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
