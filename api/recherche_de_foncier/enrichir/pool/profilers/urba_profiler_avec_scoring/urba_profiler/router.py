"""
router.py — Exemple de branchement FastAPI (à adapter à votre app / auth).

    from urba_profiler.router import router as urba_router
    app.include_router(urba_router, prefix="/api/foncier")

POST /api/foncier/urba/profil   body = UrbaProfilRequest -> UrbaProfilResponse
GET  /api/foncier/urba/couches  -> catalogue des couches disponibles
POST /api/foncier/urba/score    -> (re)calcul du score à partir de cibles déjà profilées (sans WFS)
GET  /api/foncier/urba/scoring/regles -> référentiel de règles (version, profils, règles)
"""

from __future__ import annotations

from typing import Dict, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .config import LAYERS
from .schemas import Cible, UrbaProfilRequest, UrbaProfilResponse
from .scoring.schemas import ScoreUrba
from .service import run_urba_profile

router = APIRouter(tags=["urba"])


# `def` (et non `async def`) : le traitement est bloquant (réseau + shapely),
# FastAPI l'exécute dans son threadpool sans bloquer la boucle événementielle.
@router.post("/urba/profil", response_model=UrbaProfilResponse, response_model_exclude_none=True)
def post_urba_profil(req: UrbaProfilRequest) -> UrbaProfilResponse:
    try:
        return run_urba_profile(req)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/urba/couches")
def get_urba_couches():
    return [
        {
            "key": l.key,
            "typename": l.typename,
            "display_name": l.display_name,
            "famille": l.famille,
            "geom_kind": l.geom_kind,
            "enabled_par_defaut": l.enabled,
            "colonnes": l.fields,
        }
        for l in LAYERS
    ]


class RescoreRequest(BaseModel):
    cibles: List[Cible] = Field(..., min_length=1, description="Cibles telles que renvoyées / stockées par /urba/profil")
    profil: str = "generique"


@router.post("/urba/score", response_model=Dict[str, ScoreUrba])
def post_urba_score(req: RescoreRequest) -> Dict[str, ScoreUrba]:
    """Recalcule le score (nouveau profil, règles modifiées) sans refaire les appels WFS."""
    from .scoring.engine import score_cible

    try:
        return {c.id: score_cible(c, profil=req.profil) for c in req.cibles}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/urba/scoring/regles")
def get_scoring_regles():
    from .scoring.loader import get_rules

    rs = get_rules()
    return {
        "version": rs.version,
        "statut": rs.statut,
        "profils": rs.profils,
        "sous_scores": rs.sous_scores,
        "ponderation": rs.ponderation,
        "classes": rs.classes,
        "regles": [
            {"id": r.id, "label": r.label, "familles": r.familles, "codes": r.codes, "suptypes": r.suptypes,
             "effets": r.effets, "profils": r.profils, "justification": r.justification, "a_confirmer": r.a_confirmer}
            for r in rs.regles
        ],
        "secteurs": [{"id": s.id, "label": s.label, "effets": s.effets, "justification": s.justification}
                     for s in rs.secteurs],
    }
