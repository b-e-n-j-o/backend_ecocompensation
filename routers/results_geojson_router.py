"""
results_geojson_router.py
─────────────────────────
Endpoint générique pour servir le GeoJSON des couches de résultats
thématiques (ecocompensation_results.*) filtrées par project_id.

Usage : inclure ce router dans le FastAPI principal :
    from results_geojson_router import router as results_geojson_router
    app.include_router(results_geojson_router)
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import text

from db import get_engine
from map_layers import (
    get_cesbio_aoi_geojson,
    get_fauna_aoi_geojson,
    get_fauna_buffer_aoi_geojson,
    load_filter_map_config,
)

logger = logging.getLogger(__name__)

# Couches nationales clippées à l'AOI (filter_v2) — pas ecocompensation_results.*
NATIONAL_MAP_LAYER_KEYS = frozenset({"cesbio", "fauna", "fauna_buffer"})

router = APIRouter()
engine = get_engine()

# ─── Registry des couches disponibles ────────────────────────────────────────
# Clé frontend  →  table dans le schema ecocompensation_results
# Ajouter ici chaque nouvelle couche à exposer.
LAYER_TABLE_MAP: dict[str, str] = {
    "zone_vegetation":   "zone_de_vegetation",
    "fauna":             "fauna",
    "vegetation_hybride": "bd_topo_et_cesbio",
    "zone_humide":       "zone_humide",
    "carhab":            "carhab",
    "ebc":               "ebc",
    # Ajouter d'autres couches ici au fur et à mesure
}

# Propriétés à exposer par couche (whitelist).
# None = toutes les colonnes non-géométriques.
LAYER_PROPERTIES: dict[str, list[str] | None] = {
    "zone_vegetation": ["id_src", "nature", "source", "date_maj", "acqu_plani"],
    "fauna": ["id_obs", "nom_vernaculaire", "nom_taxref", "cd_ref", "niveau_patrimonialite", "protection_nationale", "geom_type", "date_debut", "date_fin"],
    "vegetation_hybride": ["libelle_prio", "nature", "libelle", "source"],
    "zone_humide": ["source", "libelle", "inv_nom"],
    "carhab": [
        "nom_eunis",
        "code_eunis",
        "code_biotope",
        "code_physio",
        "code_hab_carhab",
        "code_niv2",
        "surface",
        "rang",
    ],
    "ebc": ["libelle"],
}


def _build_select_cols(layer_key: str, table_alias: str = "r") -> str:
    """Construit la liste SELECT des propriétés scalaires à retourner."""
    cols = LAYER_PROPERTIES.get(layer_key)
    if not cols:
        return f"{table_alias}.id::text AS id"
    parts: list[str] = [f"{table_alias}.id::text AS id"]
    for c in cols:
        if c == "id":
            continue
        if layer_key == "fauna" and c in ("niveau_patrimonialite", "protection_nationale"):
            parts.append(
                f"COALESCE(NULLIF(btrim({table_alias}.{c}::text), ''), 'Inconnu') AS {c}"
            )
        else:
            parts.append(f"{table_alias}.{c}")
    return ", ".join(parts)


@router.get(
    "/api/projects/{project_id}/geojson/results/{layer_key}",
    summary="GeoJSON d'une couche de résultats thématiques pour un projet",
    tags=["geojson"],
)
def get_results_layer_geojson(
    project_id: str,
    layer_key: str,
    run_id: str | None = None,
) -> JSONResponse:
    """
    Retourne un FeatureCollection GeoJSON (EPSG:4326) pour la couche
    `layer_key` filtrée sur `project_id`.

    Les géométries sont **découpées à l'emprise de l'AOI** du projet
    (``ST_Intersection`` avec ``ecocompensation.aoi``) pour n'afficher que la
    partie utile à l'écran (ex. polygone départemental réduit au buffer d'étude).
    Si le projet n'a pas d'AOI liée, les géométries brutes sont conservées.

    Exemple : GET /api/projects/abc123/geojson/results/cesbio?run_id=…
    """
    if layer_key in NATIONAL_MAP_LAYER_KEYS:
        try:
            with engine.begin() as conn:
                cfg = load_filter_map_config(conn, project_id, run_id)
                if layer_key == "cesbio":
                    fc = get_cesbio_aoi_geojson(conn, project_id, cfg.cesbio_libelles)
                elif layer_key == "fauna":
                    fc = get_fauna_aoi_geojson(conn, project_id, cfg.fauna_criteria)
                else:
                    fc = get_fauna_buffer_aoi_geojson(conn, project_id, cfg.fauna_criteria)
        except Exception as exc:
            logger.exception(
                "Erreur GeoJSON couche nationale %s projet %s", layer_key, project_id
            )
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return JSONResponse(
            content=fc,
            headers={"Cache-Control": "private, max-age=300"},
        )

    table = LAYER_TABLE_MAP.get(layer_key)
    if not table:
        raise HTTPException(
            status_code=404,
            detail=f"Couche inconnue : '{layer_key}'. "
                   f"Valeurs acceptées : {sorted(NATIONAL_MAP_LAYER_KEYS | set(LAYER_TABLE_MAP.keys()))}",
        )

    select_cols = _build_select_cols(layer_key)

    # Filtrage "persistence projet" :
    # pour vegetation_hybride, ne renvoyer que les valeurs choisies dans le dernier filtre.
    vegetation_values: list[str] = []
    if layer_key == "vegetation_hybride":
        try:
            with engine.begin() as conn:
                raw_last_filter = conn.execute(
                    text(
                        """
                        SELECT last_filter
                        FROM ecocompensation.projects
                        WHERE id = CAST(:pid AS uuid)
                        """
                    ),
                    {"pid": project_id},
                ).scalar_one_or_none()
            parsed: dict[str, Any] | None = None
            if isinstance(raw_last_filter, dict):
                parsed = raw_last_filter
            elif isinstance(raw_last_filter, str) and raw_last_filter.strip():
                parsed = json.loads(raw_last_filter)

            # Compat :
            # - format direct (actuel) : last_filter = { ...options... }
            # - format enveloppé (ancien) : last_filter = { "options": { ... } }
            options_obj = parsed or {}
            options = options_obj.get("options") if isinstance(options_obj.get("options"), dict) else options_obj
            if isinstance(options, dict):
                vh = options.get("vegetation_hybride")
                if isinstance(vh, dict):
                    zdv = vh.get("zdv_natures") or []
                    ces = vh.get("cesbio_libelles") or []
                    values = [str(v).strip() for v in [*zdv, *ces] if str(v).strip()]
                    # conserve l'ordre et retire les doublons
                    vegetation_values = list(dict.fromkeys(values))
        except Exception:
            logger.exception("Impossible de lire last_filter pour projet %s", project_id)

    # Les tables ecocompensation_results.* sont déjà matérialisées pour le projet :
    # on ne re-clippe pas à l'AOI ici pour éviter le coût ST_Intersection/ST_MakeValid
    # à chaque appel de la carte.
    extra_where = ""
    query_params: dict[str, Any] = {"pid": project_id}
    if layer_key == "vegetation_hybride" and vegetation_values:
        extra_where = """
          AND COALESCE(r.libelle_prio, r.nature, r.libelle) = ANY(:vegetation_values)
        """
        query_params["vegetation_values"] = vegetation_values

    query = f"""
        SELECT
            {select_cols},
            ST_AsGeoJSON(ST_Transform(r.geom_2154, 4326), 6)::json AS geometry
        FROM ecocompensation_results.{table} AS r
        WHERE r.project_id = :pid
          AND r.geom_2154 IS NOT NULL
          {extra_where}
        ORDER BY r.id
    """

    try:
        with engine.begin() as conn:
            rows = conn.execute(text(query), query_params).mappings().all()
    except Exception as exc:
        logger.exception("Erreur GeoJSON results layer %s projet %s", layer_key, project_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # ── Construction du FeatureCollection ────────────────────────────────────
    features: list[dict[str, Any]] = []
    for row in rows:
        geom = row.get("geometry")
        if geom is None:
            continue
        props: dict[str, Any] = {}
        for k, v in dict(row).items():
            if k == "geometry":
                continue
            # Convertir les types non-JSON-sérialisables (date, Decimal…)
            if hasattr(v, "isoformat"):
                props[k] = v.isoformat()
            elif v is None:
                props[k] = None
            else:
                props[k] = v

        features.append(
            {
                "type": "Feature",
                "geometry": geom,  # dict GeoJSON via ::json
                "properties": props,
            }
        )

    feature_collection = {
        "type": "FeatureCollection",
        "features": features,
    }

    return JSONResponse(
        content=feature_collection,
        headers={
            # Cache côté client 5 min — les données ne changent pas entre deux fetches
            "Cache-Control": "private, max-age=300",
        },
    )