"""
Router FastAPI pour la cartographie faune (recherche d'espèces + GeoJSON observations).

Monté dans main.py :
    app.include_router(fauna_router, prefix="/api/fauna", tags=["fauna"])

Les routes /api/fauna/taxa déjà définies dans main.py restent inchangées.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Optional

import geopandas as gpd
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from shapely.geometry import shape
from sqlalchemy import bindparam, text

from db import get_engine
from exports.qgis_encoding import write_geodataframe_shapefile_qgis

router = APIRouter()
_engine = get_engine()

_FAUNA_POINT_COLS = """
    id_obs,
    id_releve,
    cd_ref,
    nom_vernaculaire,
    nom_cite,
    nom_taxref,
    classe,
    ordre,
    famille,
    date_debut,
    date_fin,
    annee_obs,
    geom_id,
    geom_type,
    lon,
    lat,
    geometry
"""

_FAUNA_POINT_PROPS = """
    'id_obs', id_obs,
    'id_releve', id_releve,
    'cd_ref', cd_ref,
    'nom_vernaculaire', nom_vernaculaire,
    'nom_cite', nom_cite,
    'nom_taxref', nom_taxref,
    'classe', classe,
    'ordre', ordre,
    'famille', famille,
    'date_debut', date_debut,
    'date_fin', date_fin,
    'annee_obs', annee_obs,
    'geom_id', geom_id,
    'geom_type', geom_type,
    'lon', lon,
    'lat', lat
"""


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
    taxa: Optional[list[str]] = Field(
        None,
        description="Noms vernaculaires (= tax dans fauna_taxa_ref). Omis ou vide = toutes espèces (bbox requise).",
    )
    buffer_m: float = Field(
        0,
        ge=0,
        le=50000,
        description="Buffer par défaut (m) si buffer_by_taxon ne précise pas l'espèce",
    )
    buffer_by_taxon: Optional[dict[str, float]] = Field(
        None,
        description="Buffer en mètres par nom vernaculaire (prioritaire sur buffer_m)",
    )
    bbox: Optional[list[float]] = Field(
        None,
        min_length=4,
        max_length=4,
        description="[minLon, minLat, maxLon, maxLat] en WGS84",
    )
    center: Optional[list[float]] = Field(
        None,
        min_length=2,
        max_length=2,
        description="[lon, lat] WGS84 — centre d'un rayon de recherche (exclusif avec bbox)",
    )
    radius_m: Optional[float] = Field(
        None,
        gt=0,
        le=100_000,
        description="Rayon en mètres autour de center (max 100 km)",
    )
    date_min: Optional[str] = None
    date_max: Optional[str] = None
    limit: int = Field(20000, ge=1, le=100000)

    @field_validator("taxa", mode="before")
    @classmethod
    def normalize_taxa(cls, v: object) -> Optional[list[str]]:
        if v is None:
            return None
        if not isinstance(v, list):
            raise TypeError("taxa doit être une liste de chaînes ou null")
        cleaned = [str(t).strip() for t in v if t is not None and str(t).strip()]
        return cleaned or None

    @field_validator("buffer_by_taxon", mode="before")
    @classmethod
    def normalize_buffer_by_taxon(cls, v: object) -> Optional[dict[str, float]]:
        if v is None:
            return None
        if not isinstance(v, dict):
            raise TypeError("buffer_by_taxon doit être un objet {espèce: mètres}")
        out: dict[str, float] = {}
        for k, val in v.items():
            key = str(k).strip()
            if not key:
                continue
            try:
                out[key] = float(val)
            except (TypeError, ValueError) as e:
                raise ValueError(f"Buffer invalide pour {key!r}") from e
        return out or None

    @model_validator(mode="after")
    def validate_request(self) -> ObservationsRequest:
        if self.bbox is not None:
            if len(self.bbox) != 4:
                raise ValueError("bbox doit contenir exactement 4 nombres [minLon, minLat, maxLon, maxLat]")
            w, s, e, n = (float(x) for x in self.bbox)
            if w >= e or s >= n:
                raise ValueError("bbox invalide : minLon < maxLon et minLat < maxLat requis")
            if not (
                -180.0 <= w <= 180.0
                and -180.0 <= e <= 180.0
                and -90.0 <= s <= 90.0
                and -90.0 <= n <= 90.0
            ):
                raise ValueError("bbox hors plage lon/lat autorisée")
        if self.center is not None:
            if len(self.center) != 2:
                raise ValueError("center doit contenir [lon, lat]")
            lon, lat = (float(x) for x in self.center)
            if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
                raise ValueError("center hors plage lon/lat autorisée")
            if self.radius_m is None or self.radius_m <= 0:
                raise ValueError("radius_m est requis avec center")
        elif self.radius_m is not None:
            raise ValueError("center est requis avec radius_m")
        has_taxa = bool(self.taxa)
        has_bbox = self.bbox is not None and len(self.bbox) == 4
        has_radius = self.center is not None and self.radius_m is not None
        if has_bbox and has_radius:
            raise ValueError("bbox et center/radius_m sont mutuellement exclusifs")
        if not has_taxa and not has_bbox and not has_radius:
            raise ValueError("Au moins taxa, bbox ou center+radius_m est requis")
        return self


def _no_prep(t: Any) -> Any:
    return t.execution_options(no_prepare=True)


def _has_taxa(req: ObservationsRequest) -> bool:
    return bool(req.taxa)


def _build_base_where_tax_geom_dates(
    req: ObservationsRequest, *, alias: str = ""
) -> tuple[list[str], dict[str, Any]]:
    """Prédicats communs : espèces (optionnel) + géométrie + dates (sans bbox)."""
    prefix = f"{alias}." if alias else ""
    parts = [f"{prefix}geometry IS NOT NULL"]
    params: dict[str, Any] = {}

    if _has_taxa(req):
        parts.append(f"{prefix}nom_vernaculaire IN :taxa")
        params["taxa"] = list(req.taxa or [])

    if req.date_min:
        parts.append(f"{prefix}date_debut >= CAST(:date_min AS date)")
        params["date_min"] = req.date_min

    if req.date_max:
        parts.append(f"{prefix}date_debut <= CAST(:date_max AS date)")
        params["date_max"] = req.date_max

    return parts, params


def _has_bbox(req: ObservationsRequest) -> bool:
    return req.bbox is not None and len(req.bbox) == 4


def _has_radius(req: ObservationsRequest) -> bool:
    return req.center is not None and len(req.center) == 2 and req.radius_m is not None and req.radius_m > 0


def _bbox_params(req: ObservationsRequest) -> dict[str, Any]:
    assert req.bbox is not None and len(req.bbox) == 4
    return {
        "bbox_minx": req.bbox[0],
        "bbox_miny": req.bbox[1],
        "bbox_maxx": req.bbox[2],
        "bbox_maxy": req.bbox[3],
    }


def _spatial_predicate_sql(
    req: ObservationsRequest, *, geom: str = "geometry"
) -> tuple[str | None, dict[str, Any]]:
    """Filtre spatial : bbox (&& envelope) ou disque (ST_DWithin geography)."""
    if _has_radius(req):
        assert req.center is not None and req.radius_m is not None
        params = {
            "center_lon": float(req.center[0]),
            "center_lat": float(req.center[1]),
            "radius_m": float(req.radius_m),
        }
        sql = (
            f"ST_Transform({geom}, 4326) && ("
            f"ST_Buffer(ST_SetSRID(ST_MakePoint(:center_lon, :center_lat), 4326)::geography, :radius_m)::geometry"
            f") AND ST_DWithin("
            f"ST_Transform({geom}, 4326)::geography, "
            f"ST_SetSRID(ST_MakePoint(:center_lon, :center_lat), 4326)::geography, "
            f":radius_m)"
        )
        return sql, params
    if _has_bbox(req):
        sql = (
            f"ST_Transform({geom}, 4326) && ST_MakeEnvelope("
            ":bbox_minx, :bbox_miny, :bbox_maxx, :bbox_maxy, 4326)"
        )
        return sql, _bbox_params(req)
    return None, {}


def _bind_taxa_if_needed(stmt: Any, req: ObservationsRequest) -> Any:
    if _has_taxa(req):
        return stmt.bindparams(bindparam("taxa", expanding=True))
    return stmt


def _resolve_taxon_buffers(req: ObservationsRequest) -> dict[str, float]:
    """Espèce → buffer (m) ; ignoré si all-species (pas de taxa)."""
    if not _has_taxa(req):
        return {}
    by_taxon = req.buffer_by_taxon or {}
    out: dict[str, float] = {}
    for tax in req.taxa or []:
        buf = by_taxon.get(tax, req.buffer_m)
        if buf and buf > 0:
            out[tax] = float(buf)
    return out


def _points_geojson_sql(req: ObservationsRequest) -> tuple[Any, dict[str, Any]]:
    """Construit la requête points (FeatureCollection GeoJSON)."""
    base_parts, base_params = _build_base_where_tax_geom_dates(req)
    base_where_sql = " AND ".join(base_parts)
    spatial_sql, spatial_params = _spatial_predicate_sql(req)
    has_spatial = spatial_sql is not None

    geom_expr = "ST_Transform(ST_PointOnSurface(ST_MakeValid(geometry)), 4326)"

    if has_spatial:
        params = {**base_params, **spatial_params, "obs_limit": req.limit}

        # Sans taxa : filtre spatial d'abord (index GiST 4326) sur toute la table.
        if not _has_taxa(req):
            sql = _no_prep(
                text(
                    f"""
        WITH base AS MATERIALIZED (
            SELECT {_FAUNA_POINT_COLS}
            FROM ecocompensation.fauna
            WHERE {base_where_sql}
              AND {spatial_sql}
        ),
        src AS (
            SELECT
                id_obs, id_releve, cd_ref, nom_vernaculaire, nom_cite, nom_taxref,
                classe, ordre, famille, date_debut, date_fin, annee_obs,
                geom_id, geom_type, lon, lat,
                {geom_expr} AS geom
            FROM base
            LIMIT :obs_limit
        )
        SELECT jsonb_build_object(
            'type', 'FeatureCollection',
            'features', COALESCE(jsonb_agg(
                jsonb_build_object(
                    'type', 'Feature',
                    'geometry', ST_AsGeoJSON(geom)::jsonb,
                    'properties', jsonb_build_object({_FAUNA_POINT_PROPS})
                )
            ), '[]'::jsonb)
        ) AS fc
        FROM src
        """
                )
            )
            return sql, params

        sql = _bind_taxa_if_needed(
            _no_prep(
                text(
                    f"""
        WITH base AS MATERIALIZED (
            SELECT {_FAUNA_POINT_COLS}
            FROM ecocompensation.fauna
            WHERE {base_where_sql}
        ),
        src AS (
            SELECT
                id_obs, id_releve, cd_ref, nom_vernaculaire, nom_cite, nom_taxref,
                classe, ordre, famille, date_debut, date_fin, annee_obs,
                geom_id, geom_type, lon, lat,
                {geom_expr} AS geom
            FROM base
            WHERE {spatial_sql}
            LIMIT :obs_limit
        )
        SELECT jsonb_build_object(
            'type', 'FeatureCollection',
            'features', COALESCE(jsonb_agg(
                jsonb_build_object(
                    'type', 'Feature',
                    'geometry', ST_AsGeoJSON(geom)::jsonb,
                    'properties', jsonb_build_object({_FAUNA_POINT_PROPS})
                )
            ), '[]'::jsonb)
        ) AS fc
        FROM src
        """
                )
            ),
            req,
        )
        return sql, params

    params = {**base_params, "obs_limit": req.limit}
    sql = _bind_taxa_if_needed(
        _no_prep(
            text(
                f"""
        WITH src AS (
            SELECT
                id_obs, id_releve, cd_ref, nom_vernaculaire, nom_cite, nom_taxref,
                classe, ordre, famille, date_debut, date_fin, annee_obs,
                geom_id, geom_type, lon, lat,
                {geom_expr} AS geom
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
                    'properties', jsonb_build_object({_FAUNA_POINT_PROPS})
                )
            ), '[]'::jsonb)
        ) AS fc
        FROM src
        """
            )
        ),
        req,
    )
    return sql, params


def _buffers_geojson_sql(
    req: ObservationsRequest,
    taxon_buffers: dict[str, float],
) -> tuple[Any, dict[str, Any]] | tuple[None, None]:
    if not taxon_buffers:
        return None, None

    # Alias `f` obligatoire : taxon_cfg expose aussi nom_vernaculaire.
    base_parts, base_params = _build_base_where_tax_geom_dates(req, alias="f")
    base_where_sql = " AND ".join(base_parts)
    taxa_list = list(taxon_buffers.keys())
    buf_list = [taxon_buffers[t] for t in taxa_list]
    spatial_sql, spatial_params = _spatial_predicate_sql(req)

    params: dict[str, Any] = {
        **base_params,
        "taxa_list": taxa_list,
        "buf_list": buf_list,
    }

    taxon_cfg_cte = """
        taxon_cfg AS (
            SELECT t.tax::text AS nom_vernaculaire, t.buf::float8 AS buf_m
            FROM unnest(CAST(:taxa_list AS text[]), CAST(:buf_list AS float8[])) AS t(tax, buf)
        )"""

    if spatial_sql is not None:
        params.update(spatial_params)
        sql = _no_prep(
            text(
                f"""
        WITH {taxon_cfg_cte},
        base AS MATERIALIZED (
            SELECT f.nom_vernaculaire, f.geometry, tc.buf_m
            FROM ecocompensation.fauna f
            INNER JOIN taxon_cfg tc ON f.nom_vernaculaire = tc.nom_vernaculaire
            WHERE {base_where_sql}
        ),
        filt AS (
            SELECT nom_vernaculaire, geometry, buf_m
            FROM base
            WHERE {spatial_sql}
        ),
        src AS (
            SELECT
                nom_vernaculaire,
                buf_m,
                ST_Union(ST_MakeValid(geometry)) AS geom
            FROM filt
            GROUP BY nom_vernaculaire, buf_m
        )
        SELECT jsonb_build_object(
            'type', 'FeatureCollection',
            'features', COALESCE(jsonb_agg(
                jsonb_build_object(
                    'type', 'Feature',
                    'geometry', ST_AsGeoJSON(
                        ST_Buffer(
                            ST_Transform(geom, 4326)::geography,
                            buf_m
                        )::geometry
                    )::jsonb,
                    'properties', jsonb_build_object(
                        'nom_vernaculaire', nom_vernaculaire,
                        'buffer_m', buf_m
                    )
                )
            ), '[]'::jsonb)
        ) AS fc
        FROM src
        """
            )
        )
        if _has_taxa(req):
            sql = sql.bindparams(bindparam("taxa", expanding=True))
        return sql, params

    sql = _no_prep(
        text(
            f"""
        WITH {taxon_cfg_cte},
        src AS (
            SELECT
                f.nom_vernaculaire,
                tc.buf_m,
                ST_Union(ST_MakeValid(f.geometry)) AS geom
            FROM ecocompensation.fauna f
            INNER JOIN taxon_cfg tc ON f.nom_vernaculaire = tc.nom_vernaculaire
            WHERE {base_where_sql}
            GROUP BY f.nom_vernaculaire, tc.buf_m
        )
        SELECT jsonb_build_object(
            'type', 'FeatureCollection',
            'features', COALESCE(jsonb_agg(
                jsonb_build_object(
                    'type', 'Feature',
                    'geometry', ST_AsGeoJSON(
                        ST_Buffer(
                            ST_Transform(geom, 4326)::geography,
                            buf_m
                        )::geometry
                    )::jsonb,
                    'properties', jsonb_build_object(
                        'nom_vernaculaire', nom_vernaculaire,
                        'buffer_m', buf_m
                    )
                )
            ), '[]'::jsonb)
        ) AS fc
        FROM src
        """
        )
    )
    if _has_taxa(req):
        sql = sql.bindparams(bindparam("taxa", expanding=True))
    return sql, params


def _fetch_observations(req: ObservationsRequest) -> dict[str, Any]:
    points_sql, params_points = _points_geojson_sql(req)
    taxon_buffers = _resolve_taxon_buffers(req)
    buffers_sql, params_buf = _buffers_geojson_sql(req, taxon_buffers)

    try:
        with _engine.connect() as conn:
            row_points = conn.execute(points_sql, params_points).mappings().one_or_none()
            points_fc = row_points["fc"] if row_points else {"type": "FeatureCollection", "features": []}

            buffers_fc = None
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


def _geojson_fc_to_gdf(fc: dict[str, Any]) -> gpd.GeoDataFrame:
    features = fc.get("features") or []
    if not features:
        raise ValueError("Aucune observation à exporter")
    rows: list[dict[str, Any]] = []
    geoms = []
    for f in features:
        props = dict(f.get("properties") or {})
        props.pop("_color", None)
        geoms.append(shape(f["geometry"]))
        rows.append(props)
    return gpd.GeoDataFrame(rows, geometry=geoms, crs="EPSG:4326")


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
    """Recherche par nom_vernaculaire (insensible à la casse)."""
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
    Renvoie ``{ points: FeatureCollection, buffers: FeatureCollection | null }``.

    - ``taxa`` optionnel : si absent, ``bbox`` **ou** ``center``+``radius_m`` est obligatoire.
    - ``center`` + ``radius_m`` : disque géographique (exclusif avec ``bbox``).
    - ``buffer_by_taxon`` : buffer distinct par espèce (sinon ``buffer_m`` par défaut).
    - Attributs : colonnes complètes de ``ecocompensation.fauna`` (hors geometry brute).
    """
    return _fetch_observations(req)


@router.post("/observations/export/shp")
def export_observations_shp(req: ObservationsRequest, background_tasks: BackgroundTasks):
    """Exporte les observations (même filtre que /observations) en shapefile zippé (UTF-8 + .cpg)."""
    data = _fetch_observations(req)
    points_fc = data.get("points") or {"type": "FeatureCollection", "features": []}
    try:
        gdf = _geojson_fc_to_gdf(points_fc)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    with tempfile.TemporaryDirectory() as tmpdir:
        shp_path = Path(tmpdir) / "fauna_observations.shp"
        write_geodataframe_shapefile_qgis(gdf, shp_path)
        zip_path = Path(tmpdir) / "fauna_observations.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
                f = shp_path.with_suffix(ext)
                if f.exists():
                    zf.write(f, f.name)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as nf:
            shutil.copy2(zip_path, nf.name)
            final_path = nf.name

    background_tasks.add_task(os.remove, final_path)
    return FileResponse(
        final_path,
        media_type="application/zip",
        filename="fauna_observations.zip",
    )
