"""
router.py — Exemple de branchement FastAPI (à adapter à votre app / auth).

    from urba_profiler.router import router as urba_router
    app.include_router(urba_router, prefix="/api/foncier")

POST /api/foncier/urba/profil   body = UrbaProfilRequest -> UrbaProfilResponse
GET  /api/foncier/urba/couches  -> catalogue des couches disponibles
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .config import LAYERS
from .schemas import UrbaProfilRequest, UrbaProfilResponse
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
