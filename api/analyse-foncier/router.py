"""POST /api/analyse-foncier — qualifier un foncier déjà connu, sans lancer une recherche."""

from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from db import get_engine
from api.recherche_de_foncier.routers.foncier_router import _load_geodataframe_from_upload

# Le dossier s'appelle analyse-foncier : ce n'est pas un paquet Python importable.
_service_path = Path(__file__).resolve().parent / "service.py"
_spec = importlib.util.spec_from_file_location("analyse_foncier_service", _service_path)
_service = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(_service)
run_analyse_foncier = _service.run_analyse_foncier

router = APIRouter(prefix="/api/analyse-foncier", tags=["analyse-foncier"])
engine = get_engine()
logger = logging.getLogger(__name__)


def _flag(raw: str | None, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "oui", "yes", "on"}


def _species(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "fauna_species n'est pas un JSON valide.") from exc
    if not isinstance(data, list):
        raise HTTPException(400, "fauna_species doit être une liste.")
    return [str(item).strip() for item in data if str(item).strip()]


def _aliases(raw: str | None) -> dict[str, list[str]]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "fauna_aliases n'est pas un JSON valide.") from exc
    if not isinstance(data, dict):
        raise HTTPException(400, "fauna_aliases doit être un objet.")
    out: dict[str, list[str]] = {}
    for key, value in data.items():
        species = str(key).strip()
        if not species:
            continue
        names = value if isinstance(value, list) else []
        out[species] = [str(item).strip() for item in names if str(item).strip()]
    return out


def _flora(raw: str | None) -> list[dict[str, str]]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "flora_criteria n'est pas un JSON valide.") from exc
    if not isinstance(data, list):
        raise HTTPException(400, "flora_criteria doit être une liste.")
    out: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        key = str(item.get("taxon_key") or "").strip()
        label = str(item.get("label") or key).strip()
        if key and label:
            out.append({"taxon_key": key, "label": label})
    return out


@router.post("")
def analyse_foncier(
    projet: UploadFile = File(...),
    foncier: UploadFile = File(...),
    name: str = Form("Analyse de foncier"),
    occupation_sol: str = Form("true"),
    hydro: str = Form("true"),
    hydro_troncons: str = Form(""),
    hydro_surfaces: str = Form(""),
    fauna_species: str = Form("[]"),
    fauna_aliases: str = Form("{}"),
    flora_criteria: str = Form("[]"),
):
    projet_gdf = _load_geodataframe_from_upload(projet)
    foncier_gdf = _load_geodataframe_from_upload(foncier)
    try:
        return run_analyse_foncier(
            engine,
            name=(name or "Analyse de foncier").strip()[:120],
            projet_gdf=projet_gdf,
            foncier_gdf=foncier_gdf,
            occupation_sol=_flag(occupation_sol, True),
            hydro_troncons=_flag(hydro_troncons, _flag(hydro, True)),
            hydro_surfaces=_flag(hydro_surfaces, _flag(hydro, True)),
            fauna_species=_species(fauna_species),
            fauna_aliases=_aliases(fauna_aliases),
            flora=_flora(flora_criteria),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        logger.exception("analyse-foncier")
        raise HTTPException(500, "La qualification du foncier a échoué.") from exc
