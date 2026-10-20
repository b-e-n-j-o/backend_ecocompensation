from __future__ import annotations

import logging
import math
import time
from collections import OrderedDict
from datetime import date, datetime
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from db import get_engine_ppm

logger = logging.getLogger(__name__)
router = APIRouter(tags=["cadastre"])
# Cadastre parcellaire: utiliser la base PPM (SUPABASE_PPM_*),
# qui peut être différente de la base principale ecocompensation.
engine = get_engine_ppm()
COMMUNE_GEOJSON_THRESHOLD = 5000


class _TileLRUCache:
    def __init__(self, maxsize: int = 512):
        self._cache: OrderedDict[tuple, bytes] = OrderedDict()
        self._maxsize = maxsize

    def get(self, key: tuple) -> bytes | None:
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def set(self, key: tuple, value: bytes) -> None:
        if key in self._cache:
            self._cache.move_to_end(key)
        else:
            if len(self._cache) >= self._maxsize:
                self._cache.popitem(last=False)
            self._cache[key] = value

    def __len__(self) -> int:
        return len(self._cache)


_tile_cache = _TileLRUCache(maxsize=512)
_communes_3857_ready: set[str] = set()
_communes_3857_backfilling: set[str] = set()


@lru_cache(maxsize=1)
def _resolve_cadastre_source() -> tuple[str, set[str]]:
    """
    Retourne la source cadastre officielle : parcelles.parcelles.
    Cette table est partitionnée par code_dep (dep_XX), PostgreSQL prune
    automatiquement les partitions via le filtre sur code_dep.
    """
    schema, table = "parcelles", "parcelles"
    with engine.begin() as conn:
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
            raise RuntimeError("Table cadastre manquante: parcelles.parcelles")

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


@lru_cache(maxsize=128)
def _resolve_cadastre_source_for_insee(insee: str | None) -> tuple[str, set[str]]:
    _ = insee
    table_name, colset = _resolve_cadastre_source()
    if "code_dep" not in colset:
        raise RuntimeError("La table parcelles.parcelles doit exposer la colonne code_dep")
    if "code_insee" not in colset:
        raise RuntimeError("La table parcelles.parcelles doit exposer la colonne code_insee")
    return table_name, colset


def _bbox_wgs84(lng: float, lat: float, half_m: float) -> tuple[float, float, float, float]:
    d_lat = half_m / 111320.0
    cos_lat = max(0.01, math.cos((lat * math.pi) / 180.0))
    d_lng = half_m / (111320.0 * cos_lat)
    return (lng - d_lng, lat - d_lat, lng + d_lng, lat + d_lat)


def _json_safe_value(value: Any) -> Any:
    """Rend une valeur compatible JSON strict (pas de NaN/Infinity)."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _json_safe_dict(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: _json_safe_value(v) for k, v in payload.items()}


_SIMPLIFY_TOL_M: dict[int, float] = {
    10: 5.0,
    11: 2.0,
    12: 1.0,
    13: 0.5,
}


def _geom_expr_2154(z: int) -> str:
    tol = _SIMPLIFY_TOL_M.get(z)
    if tol is None:
        return "p.geom_2154"
    return f"ST_SimplifyPreserveTopology(p.geom_2154, {tol})"


def _backfill_geom_3857_sync(insee_value: str, table_name: str, deps: list[str]) -> None:
    if insee_value in _communes_3857_ready:
        return
    if insee_value in _communes_3857_backfilling:
        logger.debug("Backfill déjà en cours pour commune %s, skip", insee_value)
        return

    _communes_3857_backfilling.add(insee_value)
    try:
        dep_filter = "code_dep = ANY(:deps)" if deps else "TRUE"
        with engine.begin() as conn:
            has_col = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = 'parcelles' AND table_name = 'parcelles' "
                    "AND column_name = 'geom_3857' LIMIT 1"
                )
            ).scalar_one_or_none()
            if not has_col:
                logger.debug("Colonne geom_3857 absente, backfill ignoré")
                return

            missing = conn.execute(
                text(
                    f"SELECT COUNT(*) FROM {table_name} "
                    f"WHERE {dep_filter} AND code_insee = :insee AND geom_3857 IS NULL"
                ),
                {"deps": deps, "insee": insee_value},
            ).scalar_one()

            if missing == 0:
                _communes_3857_ready.add(insee_value)
                return

            logger.info("Backfill geom_3857 pour commune %s (%d parcelles)…", insee_value, missing)
            t0 = time.perf_counter()
            conn.execute(
                text(
                    f"UPDATE {table_name} "
                    f"SET geom_3857 = ST_Transform(geom_2154, 3857) "
                    f"WHERE {dep_filter} AND code_insee = :insee AND geom_3857 IS NULL"
                ),
                {"deps": deps, "insee": insee_value},
            )
            elapsed = time.perf_counter() - t0
            logger.info("Backfill geom_3857 commune %s terminé en %.1fs", insee_value, elapsed)
            _communes_3857_ready.add(insee_value)
    except Exception:
        logger.exception("Erreur backfill geom_3857 commune %s", insee_value)
    finally:
        _communes_3857_backfilling.discard(insee_value)


def _fetch_tile_sync(
    z: int,
    x: int,
    y: int,
    insee_value: str,
    table_name: str,
    colset: set[str],
    deps: list[str],
) -> bytes:
    has_code_dep = "code_dep" in colset
    has_geom_3857 = "geom_3857" in colset
    commune_3857_ready = insee_value in _communes_3857_ready

    dep_filter = "(:dep_count = 0 OR p.code_dep = ANY(:deps))" if has_code_dep else "TRUE"
    code_dep_sel = "p.code_dep" if has_code_dep else "LEFT(p.code_insee, 2)"

    use_3857 = has_geom_3857 and commune_3857_ready
    if use_3857:
        geom_for_mvt = "ST_AsMVTGeom(p.geom_3857, b.g3857, 4096, 256, TRUE)"
        spatial_filter = "p.geom_3857 && b.g3857 AND ST_Intersects(p.geom_3857, b.g3857)"
        bounds_cte = "SELECT ST_TileEnvelope(:z, :x, :y) AS g3857"
    else:
        geom_2154_expr = _geom_expr_2154(z)
        geom_for_mvt = (
            f"ST_AsMVTGeom(ST_Transform({geom_2154_expr}, 3857), b.g3857, 4096, 256, TRUE)"
        )
        spatial_filter = "p.geom_2154 && b.g2154 AND ST_Intersects(p.geom_2154, b.g2154)"
        bounds_cte = (
            "SELECT ST_TileEnvelope(:z, :x, :y) AS g3857, "
            "ST_Transform(ST_TileEnvelope(:z, :x, :y), 2154) AS g2154"
        )

    sql = text(
        f"""
        WITH bounds AS ({bounds_cte}),
        src AS (
            SELECT
                p.idu,
                {code_dep_sel} AS code_dep,
                p.code_insee,
                p.section,
                p.numero,
                {geom_for_mvt} AS geom
            FROM {table_name} p
            CROSS JOIN bounds b
            WHERE {dep_filter}
              AND p.code_insee = :insee
              AND {spatial_filter}
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
    return bytes(tile) if tile else b""


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
    table_name, colset = _resolve_cadastre_source_for_insee(insee)
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

        props = _json_safe_dict(props)
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
async def get_cadastre_commune_meta(
    insee: str = Query(..., min_length=2, max_length=10),
    threshold: int = Query(COMMUNE_GEOJSON_THRESHOLD, ge=100, le=50000),
) -> JSONResponse:
    table_name, colset = _resolve_cadastre_source_for_insee(insee)
    has_code_dep = "code_dep" in colset
    insee_value = insee.strip().upper()
    deps = _deps_from_insee(insee_value)
    dep_filter = "(:dep_count = 0 OR p.code_dep = ANY(:deps))" if has_code_dep else "TRUE"

    sql = text(
        f"""
        SELECT
            s.cnt AS nb_parcelles,
            ST_XMin(ext_4326)::double precision AS min_lon,
            ST_YMin(ext_4326)::double precision AS min_lat,
            ST_XMax(ext_4326)::double precision AS max_lon,
            ST_YMax(ext_4326)::double precision AS max_lat
        FROM (
            SELECT
                COUNT(*)::bigint AS cnt,
                ST_Transform(ST_SetSRID(ST_Extent(p.geom_2154)::geometry, 2154), 4326) AS ext_4326
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

    def _run():
        with engine.begin() as conn:
            return conn.execute(sql, params).mappings().one()

    row = await run_in_threadpool(_run)

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
    table_name, colset = _resolve_cadastre_source_for_insee(insee)
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
        props = _json_safe_dict(props)
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
async def get_cadastre_commune_tile(
    z: int,
    x: int,
    y: int,
    insee: str = Query(..., min_length=2, max_length=10),
    background_tasks: BackgroundTasks = None,
) -> Response:
    insee_value = insee.strip().upper()
    cache_key = (z, x, y, insee_value)
    cached = _tile_cache.get(cache_key)
    if cached is not None:
        return Response(
            content=cached,
            media_type="application/vnd.mapbox-vector-tile",
            headers={"Cache-Control": "public, max-age=3600", "X-Cache": "HIT"},
        )

    deps = _deps_from_insee(insee_value)
    table_name, colset = _resolve_cadastre_source_for_insee(insee)

    if (
        background_tasks is not None
        and "geom_3857" in colset
        and insee_value not in _communes_3857_ready
        and insee_value not in _communes_3857_backfilling
    ):
        background_tasks.add_task(
            run_in_threadpool,
            _backfill_geom_3857_sync,
            insee_value,
            table_name,
            deps,
        )

    t0 = time.perf_counter()
    try:
        tile_bytes = await run_in_threadpool(
            _fetch_tile_sync,
            z,
            x,
            y,
            insee_value,
            table_name,
            colset,
            deps,
        )
    except SQLAlchemyError:
        logger.exception("Erreur MVT z=%d x=%d y=%d insee=%s", z, x, y, insee_value)
        raise

    elapsed_ms = (time.perf_counter() - t0) * 1000
    use_3857 = "geom_3857" in colset and insee_value in _communes_3857_ready
    logger.info(
        "MVT z=%d x=%d y=%d insee=%s use_3857=%s size=%d ms=%.0f",
        z,
        x,
        y,
        insee_value,
        use_3857,
        len(tile_bytes),
        elapsed_ms,
    )
    if elapsed_ms > 800:
        logger.warning("MVT SLOW z=%d x=%d y=%d insee=%s ms=%.0f", z, x, y, insee_value, elapsed_ms)

    _tile_cache.set(cache_key, tile_bytes)
    return Response(
        content=tile_bytes,
        media_type="application/vnd.mapbox-vector-tile",
        headers={
            "Cache-Control": "public, max-age=3600",
            "X-Cache": "MISS",
            "X-Tile-Ms": f"{elapsed_ms:.0f}",
            "X-Geom-3857": "1" if use_3857 else "0",
        },
    )

