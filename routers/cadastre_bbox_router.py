from __future__ import annotations

import logging
import math
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from db import get_engine

logger = logging.getLogger(__name__)
router = APIRouter(tags=["cadastre"])
engine = get_engine()
COMMUNE_GEOJSON_THRESHOLD = 5000


def _resolve_cadastre_source() -> tuple[str, set[str]]:
    """
    Retourne (table_qualifiee, colonnes_disponibles).
    Priorité à parcelles.parcelles puis fallback ecocompensation_results.parcelles.
    """
    candidates = [("parcelles", "parcelles"), ("ecocompensation_results", "parcelles")]
    with engine.begin() as conn:
        for schema, table in candidates:
            exists = conn.execute(
                text(
                    """
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = :schema
                      AND table_name = :table
                    LIMIT 1
                    """
                ),
                {"schema": schema, "table": table},
            ).scalar_one_or_none()
            if not exists:
                continue

            cols = conn.execute(
                text(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = :schema
                      AND table_name = :table
                    """
                ),
                {"schema": schema, "table": table},
            ).scalars().all()
            return f"{schema}.{table}", set(cols)
    raise RuntimeError(
        "Aucune table cadastre trouvée (attendu: parcelles.parcelles ou ecocompensation_results.parcelles)"
    )


def _bbox_wgs84(lng: float, lat: float, half_m: float) -> tuple[float, float, float, float]:
    d_lat = half_m / 111320.0
    cos_lat = max(0.01, math.cos((lat * math.pi) / 180.0))
    d_lng = half_m / (111320.0 * cos_lat)
    return (lng - d_lng, lat - d_lat, lng + d_lng, lat + d_lat)


def _deps_from_insee(insee: str) -> list[str]:
    code = (insee or "").strip().upper()
    if len(code) >= 3 and code[:2] in {"97", "98"}:
        return [code[:3]]
    if len(code) >= 2:
        return [code[:2]]
    return []


@router.get("/api/cadastre/parcelles")
def get_cadastre_parcelles_bbox(
    lng: float = Query(..., ge=-180.0, le=180.0),
    lat: float = Query(..., ge=-90.0, le=90.0),
    half_m: float = Query(250.0, ge=25.0, le=2000.0),
    insee: str | None = Query(None, min_length=2, max_length=10),
    count: int = Query(300, ge=1, le=2000),
) -> JSONResponse:
    """
    Retourne les parcelles cadastrales autour d'un point (bbox en mètres).
    Réponse en GeoJSON EPSG:4326.
    """
    min_lon, min_lat, max_lon, max_lat = _bbox_wgs84(lng, lat, half_m)
    deps = _deps_from_insee(insee or "")
    table_name, colset = _resolve_cadastre_source()
    has_code_dep = "code_dep" in colset
    has_com_abs = "com_abs" in colset
    has_arpente = "arpente" in colset
    has_contenance = "contenance" in colset
    has_updated = "updated" in colset

    dep_filter = "(:dep_count = 0 OR p.code_dep = ANY(:deps))" if has_code_dep else "TRUE"
    code_dep_select = "p.code_dep" if has_code_dep else "LEFT(p.code_insee, 2)"
    prefixe_select = "p.com_abs AS prefixe" if has_com_abs else "NULL::text AS prefixe"
    contenance_select = "p.contenance" if has_contenance else "NULL::double precision"
    arpente_select = "p.arpente" if has_arpente else "NULL::boolean"
    updated_select = "p.updated" if has_updated else "NULL::date"
    insee_value = insee.strip() if insee and insee.strip() else None
    insee_filter = "p.code_insee = :insee" if insee_value else "TRUE"

    sql = text(
        f"""
        WITH env AS (
            SELECT ST_Transform(
                ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326),
                2154
            ) AS g2154
        )
        SELECT
            p.idu,
            {code_dep_select} AS code_dep,
            p.code_insee,
            p.section,
            p.numero,
            {prefixe_select},
            {contenance_select} AS contenance,
            {arpente_select} AS arpente,
            {updated_select} AS updated,
            ST_AsGeoJSON(ST_Transform(p.geom_2154, 4326), 6)::json AS geometry
        FROM {table_name} p, env
        WHERE {dep_filter}
          AND {insee_filter}
          AND p.geom_2154 && env.g2154
          AND ST_Intersects(p.geom_2154, env.g2154)
        LIMIT :count
        """
    )

    params: dict[str, Any] = {
        "min_lon": min_lon,
        "min_lat": min_lat,
        "max_lon": max_lon,
        "max_lat": max_lat,
        "dep_count": len(deps),
        "deps": deps,
        "insee": insee_value,
        "count": count,
    }

    try:
        with engine.begin() as conn:
            rows = conn.execute(sql, params).mappings().all()
    except SQLAlchemyError:
        logger.exception("Erreur requête cadastre bbox (table=%s)", table_name)
        raise

    features: list[dict[str, Any]] = []
    for row in rows:
        props: dict[str, Any] = {
            "idu": row["idu"],
            "code_dep": row["code_dep"],
            "code_insee": row["code_insee"],
            "section": row["section"],
            "numero": row["numero"],
            "prefixe": row["prefixe"],
            "contenance": row["contenance"],
            "arpente": row["arpente"],
            "commune": row["code_insee"],
        }
        upd = row.get("updated")
        if isinstance(upd, (datetime, date)):
            props["updated"] = upd.isoformat()
        else:
            props["updated"] = upd

        features.append(
            {
                "type": "Feature",
                "id": f"{row['code_insee']}-{row['section']}-{row['numero']}",
                "geometry": row["geometry"],
                "properties": props,
            }
        )

    logger.info(
        "Cadastre bbox: table=%s insee=%s deps=%s count=%d returned=%d",
        table_name,
        insee,
        deps,
        count,
        len(features),
    )
    return JSONResponse({"type": "FeatureCollection", "features": features})


@router.get("/api/cadastre/commune-meta")
def get_cadastre_commune_meta(
    insee: str = Query(..., min_length=2, max_length=10),
    threshold: int = Query(COMMUNE_GEOJSON_THRESHOLD, ge=100, le=50000),
) -> JSONResponse:
    table_name, colset = _resolve_cadastre_source()
    has_code_dep = "code_dep" in colset
    insee_value = insee.strip().upper()
    deps = _deps_from_insee(insee_value)
    dep_filter = "(:dep_count = 0 OR p.code_dep = ANY(:deps))" if has_code_dep else "TRUE"

    sql = text(
        f"""
        SELECT
            s.cnt AS nb_parcelles,
            ST_XMin(ext)::double precision AS min_lon,
            ST_YMin(ext)::double precision AS min_lat,
            ST_XMax(ext)::double precision AS max_lon,
            ST_YMax(ext)::double precision AS max_lat
        FROM (
            SELECT
                COUNT(*)::bigint AS cnt,
                ST_Extent(ST_Transform(p.geom_2154, 4326)) AS ext
            FROM {table_name} p
            WHERE {dep_filter}
              AND p.code_insee = :insee
        ) s
        """
    )
    params = {
        "dep_count": len(deps),
        "deps": deps,
        "insee": insee_value,
    }

    with engine.begin() as conn:
        row = conn.execute(sql, params).mappings().one()

    nb_parcelles = int(row.get("nb_parcelles") or 0)
    bbox_wgs84: list[float] | None = None
    if (
        row.get("min_lon") is not None
        and row.get("min_lat") is not None
        and row.get("max_lon") is not None
        and row.get("max_lat") is not None
    ):
        bbox_wgs84 = [
            float(row["min_lon"]),
            float(row["min_lat"]),
            float(row["max_lon"]),
            float(row["max_lat"]),
        ]
    mode = "geojson" if nb_parcelles < threshold else "mvt"
    logger.info(
        "Cadastre commune-meta: table=%s insee=%s nb=%d mode=%s threshold=%d",
        table_name,
        insee_value,
        nb_parcelles,
        mode,
        threshold,
    )
    return JSONResponse(
        {
            "code_insee": insee_value,
            "nb_parcelles": nb_parcelles,
            "threshold": threshold,
            "mode": mode,
            "bbox_wgs84": bbox_wgs84,
        }
    )


@router.get("/api/cadastre/commune")
def get_cadastre_commune_geojson(
    insee: str = Query(..., min_length=2, max_length=10),
    limit: int = Query(COMMUNE_GEOJSON_THRESHOLD, ge=1, le=10000),
) -> JSONResponse:
    insee_value = insee.strip().upper()
    deps = _deps_from_insee(insee_value)
    table_name, colset = _resolve_cadastre_source()
    has_code_dep = "code_dep" in colset
    has_com_abs = "com_abs" in colset
    has_arpente = "arpente" in colset
    has_contenance = "contenance" in colset
    has_updated = "updated" in colset

    dep_filter = "(:dep_count = 0 OR p.code_dep = ANY(:deps))" if has_code_dep else "TRUE"
    code_dep_select = "p.code_dep" if has_code_dep else "LEFT(p.code_insee, 2)"
    prefixe_select = "p.com_abs AS prefixe" if has_com_abs else "NULL::text AS prefixe"
    contenance_select = "p.contenance" if has_contenance else "NULL::double precision"
    arpente_select = "p.arpente" if has_arpente else "NULL::boolean"
    updated_select = "p.updated" if has_updated else "NULL::date"

    sql = text(
        f"""
        SELECT
            p.idu,
            {code_dep_select} AS code_dep,
            p.code_insee,
            p.section,
            p.numero,
            {prefixe_select},
            {contenance_select} AS contenance,
            {arpente_select} AS arpente,
            {updated_select} AS updated,
            ST_AsGeoJSON(ST_Transform(p.geom_2154, 4326), 6)::json AS geometry
        FROM {table_name} p
        WHERE {dep_filter}
          AND p.code_insee = :insee
        LIMIT :limit
        """
    )
    params: dict[str, Any] = {
        "dep_count": len(deps),
        "deps": deps,
        "insee": insee_value,
        "limit": limit,
    }

    with engine.begin() as conn:
        rows = conn.execute(sql, params).mappings().all()

    features: list[dict[str, Any]] = []
    for row in rows:
        props: dict[str, Any] = {
            "idu": row["idu"],
            "code_dep": row["code_dep"],
            "code_insee": row["code_insee"],
            "section": row["section"],
            "numero": row["numero"],
            "prefixe": row["prefixe"],
            "contenance": row["contenance"],
            "arpente": row["arpente"],
            "commune": row["code_insee"],
        }
        upd = row.get("updated")
        props["updated"] = upd.isoformat() if isinstance(upd, (datetime, date)) else upd
        features.append(
            {
                "type": "Feature",
                "id": f"{row['code_insee']}-{row['section']}-{row['numero']}",
                "geometry": row["geometry"],
                "properties": props,
            }
        )

    logger.info(
        "Cadastre commune GeoJSON: table=%s insee=%s limit=%d returned=%d",
        table_name,
        insee_value,
        limit,
        len(features),
    )
    return JSONResponse({"type": "FeatureCollection", "features": features})


@router.get("/api/cadastre/tiles/{z}/{x}/{y}.mvt")
def get_cadastre_commune_tile(
    z: int,
    x: int,
    y: int,
    insee: str = Query(..., min_length=2, max_length=10),
) -> Response:
    insee_value = insee.strip().upper()
    deps = _deps_from_insee(insee_value)
    table_name, colset = _resolve_cadastre_source()
    has_code_dep = "code_dep" in colset
    has_com_abs = "com_abs" in colset
    has_arpente = "arpente" in colset
    has_contenance = "contenance" in colset
    has_updated = "updated" in colset

    dep_filter = "(:dep_count = 0 OR p.code_dep = ANY(:deps))" if has_code_dep else "TRUE"
    code_dep_select = "p.code_dep" if has_code_dep else "LEFT(p.code_insee, 2)"
    prefixe_select = "p.com_abs" if has_com_abs else "NULL::text"
    contenance_select = "p.contenance" if has_contenance else "NULL::double precision"
    arpente_select = "p.arpente" if has_arpente else "NULL::boolean"
    updated_select = "p.updated::text" if has_updated else "NULL::text"

    sql = text(
        f"""
        WITH bounds AS (
            SELECT
                ST_TileEnvelope(:z, :x, :y) AS g3857,
                ST_Transform(ST_TileEnvelope(:z, :x, :y), 2154) AS g2154
        ),
        src AS (
            SELECT
                p.idu,
                {code_dep_select} AS code_dep,
                p.code_insee,
                p.section,
                p.numero,
                {prefixe_select} AS prefixe,
                {contenance_select} AS contenance,
                {arpente_select} AS arpente,
                {updated_select} AS updated,
                ST_AsMVTGeom(
                    ST_Transform(p.geom_2154, 3857),
                    b.g3857,
                    4096,
                    256,
                    TRUE
                ) AS geom
            FROM {table_name} p
            CROSS JOIN bounds b
            WHERE {dep_filter}
              AND p.code_insee = :insee
              AND p.geom_2154 && b.g2154
              AND ST_Intersects(p.geom_2154, b.g2154)
        )
        SELECT ST_AsMVT(src, 'parcelles', 4096, 'geom') AS tile
        FROM src
        WHERE geom IS NOT NULL
        """
    )
    params = {
        "z": z,
        "x": x,
        "y": y,
        "dep_count": len(deps),
        "deps": deps,
        "insee": insee_value,
    }

    with engine.begin() as conn:
        tile = conn.execute(sql, params).scalar_one_or_none()

    if tile is None:
        tile = b""
    return Response(content=bytes(tile), media_type="application/vnd.mapbox-vector-tile")

