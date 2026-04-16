from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from durete_fonciere.V3_POOL.durete_service import (
    run_durete_for_siren,
    run_durete_batch_in_memory,
)


router = APIRouter(prefix="/api/durete", tags=["durete_fonciere"])


class DureteUnitaireRequest(BaseModel):
    """Requête pour lancer le pipeline sur une seule personne morale."""

    siren: str = Field(..., min_length=9, max_length=9, description="SIREN de la PM")
    idus: List[str] = Field(
        default_factory=list,
        description="Liste d'IDU ; si vide, résolution via Supabase (si possible)",
    )
    denomination: str = Field(
        default="",
        description="Dénomination connue (optionnel, sinon récupérée via Annuaire/Supabase)",
    )
    forme_juridique: str = Field(
        default="",
        description="Code/label de forme juridique si déjà connu (optionnel)",
    )
    avec_rpg: bool = Field(
        default=True,
        description="Activer la collecte RPG (plus lent mais plus complet)",
    )
    model: str = Field(
        default="gemini-3.1-flash-lite-preview",
        description="Nom du modèle Gemini à utiliser",
    )


class DuretePoolItem(BaseModel):
    """
    Élément d'entrée pour le batch.

    On reste proche du format de `lire_input` :
      - siren (obligatoire pour les PM)
      - idus : liste d'IDU ; si vide, tenter résolution Supabase
      - denomination / forme_juridique : éventuels méta déjà connus
    """

    siren: str = Field(..., min_length=9, max_length=9)
    idus: List[str] = Field(default_factory=list)
    denomination: str = Field(default="")
    forme_juridique: str = Field(default="")


class DuretePoolRequest(BaseModel):
    """
    Requête batch pour un pool de personnes morales.

    Important : si certaines entrées ne sont pas des personnes morales
    (ou n'ont pas de SIREN valide), elles doivent être filtrées en amont
    ou seront ignorées ici.
    """

    items: List[DuretePoolItem] = Field(
        ...,
        min_length=1,
        description="Liste de personnes morales à évaluer",
    )
    avec_rpg: bool = Field(
        default=True,
        description="Activer la collecte RPG (plus lent mais plus complet)",
    )
    model: str = Field(
        default="gemini-3.1-flash-lite-preview",
        description="Nom du modèle Gemini à utiliser",
    )


@router.post("/uf", summary="Dureté foncière pour une seule personne morale")
def durete_unitaire(body: DureteUnitaireRequest) -> dict:
    """
    Lance le pipeline de dureté foncière sur une seule personne morale.

    - Si `idus` est vide, on tentera de les déduire depuis Supabase.
    - Si la résolution des parcelles échoue, on renvoie un statut `erreur`.
    """
    res = run_durete_for_siren(
        siren=body.siren,
        idus=body.idus,
        avec_rpg=body.avec_rpg,
        model=body.model,
        denomination=body.denomination,
        forme_juridique=body.forme_juridique,
    )
    if not res:
        raise HTTPException(500, "Pipeline dureté foncière : résultat vide")
    return res


@router.post(
    "/batch",
    summary="Dureté foncière pour un pool de personnes morales (batch in-memory)",
)
def durete_batch(body: DuretePoolRequest) -> dict:
    """
    Lance le pipeline sur un pool de personnes morales.

    - Les entrées sans SIREN valide (ou vides) sont ignorées.
    - Les parcelles non-PM doivent idéalement être filtrées en amont ;
      à défaut, elles ne sont simplement pas incluses ici.
    """
    valid_items: list[dict[str, Any]] = []
    for item in body.items:
        s = (item.siren or "").strip()
        if len(s) != 9 or not s.isdigit():
            # On ignore silencieusement les entrées qui ne semblent pas être des SIREN de PM
            continue
        valid_items.append(
            {
                "siren": s,
                "idus": [idu.strip() for idu in item.idus if idu and idu.strip()],
                "denomination": item.denomination.strip(),
                "forme_juridique": item.forme_juridique.strip(),
            }
        )

    if not valid_items:
        raise HTTPException(
            400,
            "Aucune personne morale valide (SIREN) dans la requête.",
        )

    results = run_durete_batch_in_memory(
        valid_items,
        avec_rpg=body.avec_rpg,
        model=body.model,
        workers=1,
    )

    return {
        "count": len(results),
        "results": results,
    }

