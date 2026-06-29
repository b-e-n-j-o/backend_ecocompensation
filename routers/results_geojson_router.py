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
from layers.layer_runner import LAYER_REGISTRY
from map_layers import (
    get_cesbio_aoi_geojson,
    get_fauna_aoi_geojson,
    get_fauna_buffer_aoi_geojson,
    load_filter_map_config,
)

logger = logging.getLogger(__name__)

EMPTY_FEATURE_COLLECTION: dict[str, Any] = {"type": "FeatureCollection", "features": []}

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
    "zones_humides_probables": "zones_humides_probables",
    "espaces_naturels_sensibles_ens": "espaces_naturels_sensibles_ens",
    "preemption_ens": "preemption_espaces_naturels_sensibles",
    "troncons_hydros": "troncons_hydros",
    "surfaces_hydros": "surfaces_hydros",
    "carhab":            "carhab",
    "ebc":               "ebc",
}

# Colonne géométrie par couche (défaut geom_2154).
LAYER_GEOM_COLUMN: dict[str, str] = {
    "zones_humides_probables": "geom",
}

# Propriétés à exposer par couche (whitelist).
# None = toutes les colonnes non-géométriques.
LAYER_PROPERTIES: dict[str, list[str] | None] = {
    "zone_vegetation": ["id_src", "nature", "source", "date_maj", "acqu_plani"],
    "fauna": ["id_obs", "nom_vernaculaire", "nom_taxref", "cd_ref", "niveau_patrimonialite", "protection_nationale", "geom_type", "date_debut", "date_fin"],
    "vegetation_hybride": ["libelle_prio", "nature", "libelle", "source"],
    "zone_humide": ["source", "libelle", "inv_nom"],
    "zones_humides_probables": ["rid", "value"],
    "espaces_naturels_sensibles_ens": ["nom_site", "commune", "texte", "idu"],
    "preemption_ens": ["nom_zpens", "commune", "texte", "idu"],
    "troncons_hydros": [
        "cleabs",
        "nom",
        "nature",
        "classe_de_largeur",
        "numero_d_ordre",
        "code_hydrographique",
        "type_de_bras",
    ],
    "surfaces_hydros": [
        "cleabs",
        "nom",
        "nature",
        "position_par_rapport_au_sol",
        "statut",
        "code_hydrographique",
        "persistance",
    ],
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
    id_col = "cleabs" if layer_key in ("troncons_hydros", "surfaces_hydros") else "id"
    if not cols:
        return f"{table_alias}.{id_col}::text AS id"
    parts: list[str] = [f"{table_alias}.{id_col}::text AS id"]
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


def _results_table_exists(conn, table: str) -> bool:
    fqn = f"ecocompensation_results.{table}"
    return bool(
        conn.execute(
            text("SELECT to_regclass(:r) IS NOT NULL"),
            {"r": fqn},
        ).scalar_one()
    )


def _materialize_results_layer_if_missing(project_id: str, layer_key: str, table: str) -> None:
    """Crée / peuple la table projet si le prefetch filtrage ne l'a pas encore fait."""
    with engine.connect() as conn:
        if _results_table_exists(conn, table):
            return
        row = conn.execute(
            text(
                """
                SELECT aoi_id::text AS aoi_id
                FROM ecocompensation.projects
                WHERE id = CAST(:pid AS uuid)
                """
            ),
            {"pid": project_id},
        ).mappings().one_or_none()
    if not row or not row.get("aoi_id"):
        return

    index = {cfg["key"]: cfg for cfg in LAYER_REGISTRY}
    cfg = index.get(layer_key)
    if not cfg:
        return

    try:
        cfg["fn"](engine, project_id, str(row["aoi_id"]), None)
    except Exception:
        logger.exception(
            "Materialisation couche %s échouée pour project_id=%s",
            layer_key,
            project_id,
        )


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
    geom_col = LAYER_GEOM_COLUMN.get(layer_key, "geom_2154")
    order_col = "cleabs" if layer_key in ("troncons_hydros", "surfaces_hydros") else "id"

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

    _materialize_results_layer_if_missing(project_id, layer_key, table)

    query = f"""
        SELECT
            {select_cols},
            ST_AsGeoJSON(ST_Transform(r.{geom_col}, 4326), 6)::json AS geometry
        FROM ecocompensation_results.{table} AS r
        WHERE r.project_id = :pid
          AND r.{geom_col} IS NOT NULL
          {extra_where}
        ORDER BY r.{order_col}
    """

    try:
        with engine.begin() as conn:
            rows = conn.execute(text(query), query_params).mappings().all()
    except Exception as exc:
        err = str(exc)
        if "UndefinedTable" in err or "does not exist" in err:
            logger.warning(
                "Table results absente pour couche %s projet %s — GeoJSON vide",
                layer_key,
                project_id,
            )
            return JSONResponse(
                content=EMPTY_FEATURE_COLLECTION,
                headers={"Cache-Control": "private, max-age=60"},
            )
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