"""Endpoint Sitadel seul : pas de dureté, pas de score, pas de pool obligatoire."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from db import get_engine
from api.recherche_de_foncier.enrichir.durete_fonciere.AUTORISATIONS_URBA_SITADEL.recherche import (
    SitadelIndisponible,
    normaliser_idus,
)
from api.recherche_de_foncier.enrichir.durete_fonciere.AUTORISATIONS_URBA_SITADEL.service import lookup_for_idus

router = APIRouter(prefix="/api/autorisations-urba", tags=["autorisations_urba"])
engine = get_engine()


class AutorisationsParcellesBody(BaseModel):
    idus: list[str] = Field(..., min_length=1, max_length=500)
    avec_voisins: bool = Field(
        default=True,
        description="Chercher aussi les dossiers des parcelles qui touchent la cible (ST_Touches).",
    )


def _lookup(idus: list[str], avec_voisins: bool) -> dict:
    propres = normaliser_idus(idus)
    if not propres:
        raise HTTPException(400, "Aucun IDU exploitable (10 à 20 caractères alphanumériques).")
    try:
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL statement_timeout = '3min'"))
            payloads = lookup_for_idus(conn, propres, avec_voisins=avec_voisins)
    except SitadelIndisponible as exc:
        raise HTTPException(503, str(exc)) from exc
    return {
        "count": len(payloads),
        "avec_voisins": avec_voisins,
        "couverture": "sitadel_cadastre_actuel_nouvelle_aquitaine",
        "results": [payloads[idu] for idu in propres if idu in payloads],
    }


@router.post("/parcelles", summary="Autorisations Sitadel pour une liste d'IDU")
def autorisations_parcelles(body: AutorisationsParcellesBody) -> dict:
    """
    Recherche foncière Sitadel sur le cadastre actuel.

    Pour chaque IDU : dossiers sur la parcelle, puis sur les parcelles
    voisines (contour qui touche). Ne lance pas la dureté foncière.
    """
    return _lookup(body.idus, body.avec_voisins)


@router.get("/parcelles/{idu}", summary="Autorisations Sitadel pour un IDU")
def autorisations_une_parcelle(idu: str, avec_voisins: bool = True) -> dict:
    result = _lookup([idu], avec_voisins)
    if not result["results"]:
        raise HTTPException(400, "IDU inexploitable.")
    return result["results"][0]
