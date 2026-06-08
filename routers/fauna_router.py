"""
Router FastAPI pour la cartographie faune (recherche d'espèces + GeoJSON observations).

Monté dans main.py :
    app.include_router(fauna_router, prefix="/api/fauna", tags=["fauna"])

Les routes /api/fauna/taxa déjà définies dans main.py restent inchangées.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import bindparam, text

from db import get_engine

router = APIRouter()
_engine = get_engine()


# =========================================================================
# Modèles
# =========================================================================


class SpeciesItem(BaseModel):
    """Réponse agrégée depuis ``ecocompensation.fauna`` (ex. route /search)."""

    cd_ref: int
    nom_vernaculaire: str
    nom_taxref: Optional[str] = None
    classe: Optional[str] = None
    nb_obs: int


class FaunaTaxonRefItem(BaseModel):
    """Ligne de ``ecocompensation.fauna_taxa_ref`` (référentiel espèces, léger)."""

    tax: str
    protection_nationale: Optional[str] = None
    niveau_patrimonialite: Optional[str] = None


class ObservationsRequest(BaseModel):
    taxa: list[str] = Field(
        ...,
        min_length=1,
        description="Noms vernaculaires = colonne tax de fauna_taxa_ref (= nom_vernaculaire dans fauna)",
    )
    buffer_m: float = Field(0, ge=0, le=50000, description="Rayon du buffer en mètres (0 = pas de buffer)")
    bbox: Optional[list[float]] = Field(
        None,
        min_length=4,
        max_length=4,
        description="Optionnel : [minLon, minLat, maxLon, maxLat] en WGS84 pour filtrer sur la vue carte",
    )
    date_min: Optional[str] = None
    date_max: Optional[str] = None
    limit: int = Field(20000, ge=1, le=100000)

    @field_validator("taxa", mode="before")
    @classmethod
    def normalize_taxa(cls, v: object) -> list[str]:
        if not isinstance(v, list):
            raise TypeError("taxa doit être une liste de chaînes")
        cleaned = [str(t).strip() for t in v if t is not None and str(t).strip()]
        if not cleaned:
            raise ValueError("Au moins un taxon non vide est requis")
        return cleaned

    @model_validator(mode="after")
    def validate_bbox(self) -> ObservationsRequest:
        if self.bbox is None:
            return self
        if len(self.bbox) != 4:
            raise ValueError("bbox doit contenir exactement 4 nombres [minLon, minLat, maxLon, maxLat]")
        w, s, e, n = (float(x) for x in self.bbox)
        if w >= e or s >= n:
            raise ValueError("bbox invalide : minLon < maxLon et minLat < maxLat requis")
        if not (-180.0 <= w <= 180.0 and -180.0 <= e <= 180.0 and -90.0 <= s <= 90.0 and -90.0 <= n <= 90.0):
            raise ValueError("bbox hors plage lon/lat autorisée")
        return self


def _no_prep(t: Any) -> Any:
    return t.execution_options(no_prepare=True)


def _build_base_where_tax_geom_dates(req: ObservationsRequest) -> tuple[list[str], dict[str, Any]]:
    """Prédicats communs : espèces + géométrie + dates (sans bbox)."""
    parts = ["nom_vernaculaire IN :taxa", "geometry IS NOT NULL"]
    params: dict[str, Any] = {"taxa": list(req.taxa)}

    if req.date_min:
        parts.append("date_debut >= CAST(:date_min AS date)")
        params["date_min"] = req.date_min

    if req.date_max:
        parts.append("date_debut <= CAST(:date_max AS date)")
        params["date_max"] = req.date_max

    return parts, params


def _bbox_predicate_sql() -> str:
    return (
        "ST_Transform(geometry, 4326) && ST_MakeEnvelope("
        ":bbox_minx, :bbox_miny, :bbox_maxx, :bbox_maxy, 4326)"
    )


def _bbox_params(req: ObservationsRequest) -> dict[str, Any]:
    assert req.bbox is not None and len(req.bbox) == 4
    return {
        "bbox_minx": req.bbox[0],
        "bbox_miny": req.bbox[1],
        "bbox_maxx": req.bbox[2],
        "bbox_maxy": req.bbox[3],
    }


# =========================================================================
# /species  →  référentiel ``fauna_taxa_ref`` (colonne tax, instantané)
# =========================================================================


@router.get("/species", response_model=list[FaunaTaxonRefItem])
def list_species_catalog(
    limit: int = Query(
        100_000,
        ge=1,
        le=200_000,
        description="Nombre max de lignes (table de référence, déjà une ligne par taxon)",
    ),
):
    """
    Liste les valeurs ``tax`` de ``ecocompensation.fauna_taxa_ref`` (sans agrégation sur fauna).
    """
    with _engine.connect() as conn:
        exists = conn.execute(
            text("SELECT to_regclass(:r) IS NOT NULL").execution_options(no_prepare=True),
            {"r": "ecocompensation.fauna_taxa_ref"},
        ).scalar_one()
        if not exists:
            return []

        sql = _no_prep(
            text(
                """
            SELECT
                btrim(tax::text) AS tax,
                protection_nationale,
                niveau_patrimonialite
            FROM ecocompensation.fauna_taxa_ref
            WHERE tax IS NOT NULL
              AND btrim(tax::text) <> ''
            ORDER BY lower(btrim(tax::text))
            LIMIT :lim
            """
            )
        )
        rows = conn.execute(sql, {"lim": limit}).mappings().all()
    return [FaunaTaxonRefItem.model_validate(dict(r)) for r in rows]


# =========================================================================
# /search  →  autocomplete nom_vernaculaire
# =========================================================================


@router.get("/search", response_model=list[SpeciesItem])
def search_species(
    q: str = Query(..., min_length=1, description="Texte saisi"),
    limit: int = Query(20, ge=1, le=100),
):
    """Recherche par nom_vernaculaire (insensible à la casse).

    N'utilise pas ``unaccent()`` : l'extension PostgreSQL ``unaccent`` n'est pas
    toujours activée (ex. Supabase). Pour une recherche insensible aux accents,
    exécuter en base : ``CREATE EXTENSION IF NOT EXISTS unaccent;`` puis rétablir
    ``unaccent(lower(...))`` dans la clause LIKE si besoin.
    """
    pattern = f"%{q}%"
    sql = _no_prep(
        text(
            """
        SELECT
            cd_ref,
            nom_vernaculaire,
            MAX(nom_taxref)  AS nom_taxref,
            MAX(classe)      AS classe,
            COUNT(*)::int    AS nb_obs
        FROM ecocompensation.fauna
        WHERE nom_vernaculaire IS NOT NULL
          AND cd_ref IS NOT NULL
          AND lower(nom_vernaculaire) LIKE lower(:pattern)
        GROUP BY cd_ref, nom_vernaculaire
        ORDER BY nb_obs DESC, nom_vernaculaire
        LIMIT :lim
        """
        )
    )
    with _engine.connect() as conn:
        rows = conn.execute(sql, {"pattern": pattern, "lim": limit}).mappings().all()
    return [SpeciesItem.model_validate(dict(r)) for r in rows]


# =========================================================================
# /observations  →  points (+ buffers optionnels) en GeoJSON
# =========================================================================


@router.post("/observations")
def get_observations(req: ObservationsRequest):
    """
    Renvoie un objet :
    {
        "points":  FeatureCollection (geometry points),
        "buffers": FeatureCollection | null  (un polygone par espèce, dissous)
    }

    Géométries source en **EPSG:2154** ; sortie GeoJSON en **4326**.

    Si ``bbox`` est fournie : filtre **d'abord par taxon** (CTE ``MATERIALIZED``), puis bbox
    sur ce sous-ensemble — évite un parcours GiST large (~centaines de k lignes) puis filtre nom.
    """
    base_parts, base_params = _build_base_where_tax_geom_dates(req)
    base_where_sql = " AND ".join(base_parts)
    has_bbox = req.bbox is not None and len(req.bbox) == 4

    if has_bbox:
        bbox_sql = _bbox_predicate_sql()
        bbox_p = _bbox_params(req)
        params_points = {**base_params, **bbox_p, "obs_limit": req.limit}
        points_sql = _no_prep(
            text(
                f"""
        WITH base AS MATERIALIZED (
            SELECT
                id_obs,
                id_releve,
                cd_ref,
                nom_vernaculaire,
                nom_taxref,
                classe,
                ordre,
                famille,
                date_debut,
                date_fin,
                annee_obs,
                geometry
            FROM ecocompensation.fauna
            WHERE {base_where_sql}
        ),
        src AS (
            SELECT
                id_obs,
                id_releve,
                cd_ref,
                nom_vernaculaire,
                nom_taxref,
                classe,
                ordre,
                famille,
                date_debut,
                date_fin,
                annee_obs,
                ST_Transform(
                    ST_PointOnSurface(ST_MakeValid(geometry)),
                    4326
                ) AS geom
            FROM base
            WHERE {bbox_sql}
            LIMIT :obs_limit
        )
        SELECT jsonb_build_object(
            'type', 'FeatureCollection',
            'features', COALESCE(jsonb_agg(
                jsonb_build_object(
                    'type', 'Feature',
                    'geometry', ST_AsGeoJSON(geom)::jsonb,
                    'properties', jsonb_build_object(
                        'id_obs', id_obs,
                        'id_releve', id_releve,
                        'cd_ref', cd_ref,
                        'nom_vernaculaire', nom_vernaculaire,
                        'nom_taxref', nom_taxref,
                        'classe', classe,
                        'ordre', ordre,
                        'famille', famille,
                        'date_debut', date_debut,
                        'date_fin', date_fin,
                        'annee_obs', annee_obs
                    )
                )
            ), '[]'::jsonb)
        ) AS fc
        FROM src
        """
            ).bindparams(bindparam("taxa", expanding=True))
        )
    else:
        params_points = {**base_params, "obs_limit": req.limit}
        points_sql = _no_prep(
            text(
                f"""
        WITH src AS (
            SELECT
                id_obs,
                id_releve,
                cd_ref,
                nom_vernaculaire,
                nom_taxref,
                classe,
                ordre,
                famille,
                date_debut,
                date_fin,
                annee_obs,
                ST_Transform(
                    ST_PointOnSurface(ST_MakeValid(geometry)),
                    4326
                ) AS geom
            FROM ecocompensation.fauna
            WHERE {base_where_sql}
            LIMIT :obs_limit
        )
        SELECT jsonb_build_object(
            'type', 'FeatureCollection',
            'features', COALESCE(jsonb_agg(
                jsonb_build_object(
                    'type', 'Feature',
                    'geometry', ST_AsGeoJSON(geom)::jsonb,
                    'properties', jsonb_build_object(
                        'id_obs', id_obs,
                        'id_releve', id_releve,
                        'cd_ref', cd_ref,
                        'nom_vernaculaire', nom_vernaculaire,
                        'nom_taxref', nom_taxref,
                        'classe', classe,
                        'ordre', ordre,
                        'famille', famille,
                        'date_debut', date_debut,
                        'date_fin', date_fin,
                        'annee_obs', annee_obs
                    )
                )
            ), '[]'::jsonb)
        ) AS fc
        FROM src
        """
            ).bindparams(bindparam("taxa", expanding=True))
        )

    buffers_fc = None
    buffers_sql = None
    params_buf: dict[str, Any] | None = None
    if req.buffer_m and req.buffer_m > 0:
        if has_bbox:
            params_buf = {**base_params, **bbox_p, "buf_m": req.buffer_m}
            buffers_sql = _no_prep(
                text(
                    f"""
            WITH base AS MATERIALIZED (
                SELECT nom_vernaculaire, geometry
                FROM ecocompensation.fauna
                WHERE {base_where_sql}
            ),
            filt AS (
                SELECT nom_vernaculaire, geometry
                FROM base
                WHERE {bbox_sql}
            ),
            src AS (
                SELECT
                    nom_vernaculaire,
                    ST_Union(ST_MakeValid(geometry)) AS geom
                FROM filt
                GROUP BY nom_vernaculaire
            )
            SELECT jsonb_build_object(
                'type', 'FeatureCollection',
                'features', COALESCE(jsonb_agg(
                    jsonb_build_object(
                        'type', 'Feature',
                        'geometry', ST_AsGeoJSON(
                            ST_Buffer(
                                ST_Transform(geom, 4326)::geography,
                                :buf_m
                            )::geometry
                        )::jsonb,
                        'properties', jsonb_build_object(
                            'nom_vernaculaire', nom_vernaculaire,
                            'buffer_m', CAST(:buf_m AS float)
                        )
                    )
                ), '[]'::jsonb)
            ) AS fc
            FROM src
            """
                ).bindparams(bindparam("taxa", expanding=True))
            )
        else:
            params_buf = {**base_params, "buf_m": req.buffer_m}
            buffers_sql = _no_prep(
                text(
                    f"""
            WITH src AS (
                SELECT
                    nom_vernaculaire,
                    ST_Union(ST_MakeValid(geometry)) AS geom
                FROM ecocompensation.fauna
                WHERE {base_where_sql}
                GROUP BY nom_vernaculaire
            )
            SELECT jsonb_build_object(
                'type', 'FeatureCollection',
                'features', COALESCE(jsonb_agg(
                    jsonb_build_object(
                        'type', 'Feature',
                        'geometry', ST_AsGeoJSON(
                            ST_Buffer(
                                ST_Transform(geom, 4326)::geography,
                                :buf_m
                            )::geometry
                        )::jsonb,
                        'properties', jsonb_build_object(
                            'nom_vernaculaire', nom_vernaculaire,
                            'buffer_m', CAST(:buf_m AS float)
                        )
                    )
                ), '[]'::jsonb)
            ) AS fc
            FROM src
            """
                ).bindparams(bindparam("taxa", expanding=True))
            )

    try:
        with _engine.connect() as conn:
            row_points = conn.execute(points_sql, params_points).mappings().one_or_none()
            points_fc = row_points["fc"] if row_points else {"type": "FeatureCollection", "features": []}

            if buffers_sql is not None and params_buf is not None:
                row_buf = conn.execute(buffers_sql, params_buf).mappings().one_or_none()
                buffers_fc = row_buf["fc"] if row_buf else None
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}") from e

    if isinstance(points_fc, str):
        points_fc = json.loads(points_fc)
    if isinstance(buffers_fc, str):
        buffers_fc = json.loads(buffers_fc)

    return {"points": points_fc, "buffers": buffers_fc}
