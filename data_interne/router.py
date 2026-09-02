"""
API Web SIG — données internes.

Monté dans main.py :
    from data_interne.router import router as data_interne_router
    app.include_router(data_interne_router, prefix="/api/data-interne", tags=["data-interne"])

Géométries :
  - intersections métier → geom_2154 (EPSG:2154)
  - tuiles / web        → geom_3857 si présent, sinon ST_Transform à la volée
  - GeoJSON MapLibre    → EPSG:4326 via ST_Transform(geom_2154, 4326)
  - couches moyennes    → MVT live (ST_AsMVT)
  - couches lourdes     → MBTiles (Storage → cache local → tuiles FastAPI)
"""

from __future__ import annotations

import gzip
import logging
import sqlite3
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text

from db import get_engine

from .catalog import InternalLayer, LAYERS, get_layer
from .storage_mbtiles import ensure_local_mbtiles

logger = logging.getLogger(__name__)

router = APIRouter()
engine = get_engine()

EMPTY_FC: dict[str, Any] = {"type": "FeatureCollection", "features": []}
MVT_SOURCE_LAYER = "default"
LIST_CACHE_TTL_S = 90.0
TILE_CACHE_MAX = 768

_list_cache: tuple[float, list[dict[str, Any]]] | None = None


class _TileLRUCache:
    def __init__(self, maxsize: int = TILE_CACHE_MAX):
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
            return
        if len(self._cache) >= self._maxsize:
            self._cache.popitem(last=False)
        self._cache[key] = value


_tile_cache = _TileLRUCache()
_has_3857_cache: dict[str, bool] = {}
_mbtiles_conn: dict[str, sqlite3.Connection] = {}


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _fqn(layer: InternalLayer) -> str:
    return f"{_ident(layer.schema)}.{_ident(layer.table)}"


def _table_exists(conn, layer: InternalLayer) -> bool:
    return bool(
        conn.execute(
            text("SELECT to_regclass(:r) IS NOT NULL"),
            {"r": f"{layer.schema}.{layer.table}"},
        ).scalar()
    )


def _mbtiles_metadata(path: Path) -> dict[str, str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT name, value FROM metadata").fetchall()
    finally:
        conn.close()
    return {str(k): str(v) for k, v in rows if k is not None}


def _open_mbtiles(path: Path) -> sqlite3.Connection:
    key = str(path)
    conn = _mbtiles_conn.get(key)
    if conn is None:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        _mbtiles_conn[key] = conn
    return conn


def _read_mbtiles_tile(path: Path, z: int, x: int, y: int) -> bytes | None:
    """XYZ MapLibre → TMS MBTiles. None si fichier absent ; b'' si tuile vide."""
    if not path.is_file():
        return None
    tms_y = (1 << z) - 1 - y
    conn = _open_mbtiles(path)
    row = conn.execute(
        "SELECT tile_data FROM tiles WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?",
        (z, x, tms_y),
    ).fetchone()
    if not row or row[0] is None:
        return b""
    data = bytes(row[0])
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    return data


def _has_column(conn, layer: InternalLayer, column: str) -> bool:
    return bool(
        conn.execute(
            text(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = :s AND table_name = :t AND column_name = :c
                LIMIT 1
                """
            ),
            {"s": layer.schema, "t": layer.table, "c": column},
        ).scalar()
    )


def _parse_bbox(raw: str | None) -> tuple[float, float, float, float] | None:
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 4:
        raise HTTPException(status_code=400, detail="bbox attendu : west,south,east,north")
    try:
        w, s, e, n = (float(p) for p in parts)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="bbox : nombres invalides") from exc
    if w >= e or s >= n:
        raise HTTPException(status_code=400, detail="bbox : emprise invalide")
    return w, s, e, n


def _json_safe(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, float) and value != value:
        return None
    return value


def _extra_where(layer: InternalLayer) -> str:
    return f" AND ({layer.where_sql})" if layer.where_sql else ""


def _clip_sql(layer: InternalLayer, geom: str) -> tuple[str, dict[str, Any]]:
    if not layer.clip_bbox:
        return "", {}
    sql = f"""
          AND t.{geom} && ST_Transform(ST_MakeEnvelope(:w, :s, :e, :n, 4326), 2154)
          AND ST_Intersects(
                t.{geom},
                ST_Transform(ST_MakeEnvelope(:w, :s, :e, :n, 4326), 2154)
              )
        """
    params = {
        "w": layer.clip_bbox[0],
        "s": layer.clip_bbox[1],
        "e": layer.clip_bbox[2],
        "n": layer.clip_bbox[3],
    }
    return sql, params


def _layer_count(conn, layer: InternalLayer) -> int:
    fqn = _fqn(layer)
    geom = _ident(layer.geom_2154)
    extra = _extra_where(layer)
    clip_sql, params = _clip_sql(layer, geom)
    if layer.clip_bbox or layer.where_sql or layer.compute_bounds:
        n = conn.execute(
            text(
                f"""
                SELECT count(*)::int FROM {fqn} AS t
                WHERE t.{geom} IS NOT NULL
                {extra}
                {clip_sql}
                """
            ),
            params,
        ).scalar()
        return int(n or 0)
    n = conn.execute(
        text("SELECT GREATEST(reltuples, 0)::bigint FROM pg_class WHERE oid = to_regclass(:r)"),
        {"r": f"{layer.schema}.{layer.table}"},
    ).scalar()
    return int(n or 0)


def _layer_bounds_and_count(conn, layer: InternalLayer) -> tuple[list[float] | None, int]:
    fqn = _fqn(layer)
    geom = _ident(layer.geom_2154)
    row = conn.execute(
        text(
            f"""
            SELECT
                (SELECT count(*)::int FROM {fqn}) AS n,
                ST_XMin(ext) AS w,
                ST_YMin(ext) AS s,
                ST_XMax(ext) AS e,
                ST_YMax(ext) AS north
            FROM (
                SELECT ST_Transform(ST_SetSRID(ST_Extent({geom})::geometry, 2154), 4326) AS ext
                FROM {fqn}
                WHERE {geom} IS NOT NULL
            ) t
            """
        )
    ).mappings().one()
    n = int(row["n"] or 0)
    if n == 0 or row["w"] is None:
        return None, n
    return [float(row["w"]), float(row["s"]), float(row["e"]), float(row["north"])], n


def _list_layers_uncached() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    with engine.connect() as conn:
        for layer in LAYERS:
            mb_path = layer.mbtiles_path()
            mb_ok = bool(mb_path and mb_path.is_file())
            table_ok = _table_exists(conn, layer)
            if not table_ok and not mb_ok:
                items.append(
                    {
                        **layer.public_dict(),
                        "count": 0,
                        "bounds": list(layer.bounds_4326 or layer.clip_bbox) if (layer.bounds_4326 or layer.clip_bbox) else None,
                        "available": False,
                    }
                )
                continue

            bounds: list[float] | None = (
                list(layer.bounds_4326)
                if layer.bounds_4326
                else (list(layer.clip_bbox) if layer.clip_bbox else None)
            )
            count = 0
            if mb_ok and mb_path is not None:
                meta = _mbtiles_metadata(mb_path)
                if meta.get("feature_count"):
                    try:
                        count = int(meta["feature_count"])
                    except ValueError:
                        count = 0
                if bounds is None and meta.get("bounds"):
                    parts = [p.strip() for p in meta["bounds"].split(",")]
                    if len(parts) == 4:
                        bounds = [float(p) for p in parts]
            if count == 0 and table_ok:
                if layer.compute_bounds and bounds is None:
                    bounds, count = _layer_bounds_and_count(conn, layer)
                else:
                    count = _layer_count(conn, layer)
            items.append(
                {
                    **layer.public_dict(),
                    "count": count,
                    "bounds": bounds,
                    "available": True,
                }
            )
    return items


@router.get("/layers")
def list_layers() -> JSONResponse:
    global _list_cache
    now = time.monotonic()
    if _list_cache is not None and now - _list_cache[0] < LIST_CACHE_TTL_S:
        return JSONResponse({"layers": _list_cache[1]})
    items = _list_layers_uncached()
    _list_cache = (now, items)
    return JSONResponse({"layers": items})


@router.get("/layers/{layer_key}/geojson")
def layer_geojson(
    layer_key: str,
    bbox: str | None = Query(
        default=None,
        description="Emprise WGS84 west,south,east,north — filtre via geom_3857",
    ),
    limit: int = Query(default=20_000, ge=1, le=50_000),
) -> JSONResponse:
    layer = get_layer(layer_key)
    if layer is None:
        raise HTTPException(status_code=404, detail=f"Couche inconnue : {layer_key}")
    if layer.delivery in ("mvt", "mbtiles"):
        raise HTTPException(
            status_code=400,
            detail="Couche trop lourde pour un GeoJSON : utiliser /tiles/{z}/{x}/{y}.mvt",
        )

    parsed_bbox = _parse_bbox(bbox)
    fqn = _fqn(layer)
    geom_2154 = _ident(layer.geom_2154)
    geom_3857 = _ident(layer.geom_3857)
    id_col = _ident(layer.id_column)
    prop_cols = ", ".join(f"t.{_ident(c)}" for c in layer.properties)

    where = [f"t.{geom_2154} IS NOT NULL"]
    params: dict[str, Any] = {"lim": limit}
    if layer.where_sql:
        where.append(f"({layer.where_sql})")
    if parsed_bbox:
        where.append(
            f"t.{geom_3857} && ST_Transform(ST_MakeEnvelope(:w, :s, :e, :n, 4326), 3857)"
        )
        params.update(
            {"w": parsed_bbox[0], "s": parsed_bbox[1], "e": parsed_bbox[2], "n": parsed_bbox[3]}
        )

    sql = f"""
        SELECT
            t.{id_col} AS id,
            {prop_cols},
            ST_AsGeoJSON(ST_Transform(ST_Force2D(t.{geom_2154}), 4326), 6)::json AS geometry
        FROM {fqn} AS t
        WHERE {' AND '.join(where)}
        ORDER BY t.{id_col}
        LIMIT :lim
    """

    try:
        with engine.connect() as conn:
            if not _table_exists(conn, layer):
                return JSONResponse(EMPTY_FC)
            rows = conn.execute(text(sql), params).mappings().all()
    except Exception as exc:
        logger.exception("GeoJSON couche %s", layer_key)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    features: list[dict[str, Any]] = []
    for row in rows:
        geom = row.get("geometry")
        if geom is None:
            continue
        props: dict[str, Any] = {"id": _json_safe(row.get("id"))}
        for col in layer.properties:
            props[col] = _json_safe(row.get(col))
        features.append({"type": "Feature", "id": row.get("id"), "geometry": geom, "properties": props})

    return JSONResponse(
        {"type": "FeatureCollection", "features": features},
        headers={"Cache-Control": "private, max-age=120"},
    )


_centroid_cache: dict[str, tuple[float, dict[str, Any]]] = {}
CENTROID_CACHE_TTL_S = 300.0


@router.get("/layers/{layer_key}/centroids")
def layer_centroids(layer_key: str) -> JSONResponse:
    """Points (ST_PointOnSurface) pour halos / clusters aux zooms inférieurs à min_zoom."""
    layer = get_layer(layer_key)
    if layer is None:
        raise HTTPException(status_code=404, detail=f"Couche inconnue : {layer_key}")
    if not layer.style.cluster:
        raise HTTPException(status_code=400, detail="Cette couche n'a pas de clusters")

    now = time.monotonic()
    cached = _centroid_cache.get(layer_key)
    if cached is not None and now - cached[0] < CENTROID_CACHE_TTL_S:
        return JSONResponse(cached[1], headers={"Cache-Control": "private, max-age=120"})

    fqn = _fqn(layer)
    geom = _ident(layer.geom_2154)
    fid_sql = layer.feature_id_sql or f"t.{_ident(layer.id_column)}"
    extra = _extra_where(layer)
    clip_sql, params = _clip_sql(layer, geom)
    # Attributs légers pour identify sur un point isolé avant le zoom géométrie.
    keep = [c for c in layer.properties if c in ("identifiant", "type", "classe")]
    prop_sql = ", ".join(f"t.{_ident(c)}" for c in keep)
    prop_select = f", {prop_sql}" if keep else ""

    sql = f"""
        SELECT
            {fid_sql} AS id
            {prop_select},
            ST_AsGeoJSON(
                ST_Transform(ST_PointOnSurface(ST_MakeValid(ST_Force2D(t.{geom}))), 4326),
                6
            )::json AS geometry
        FROM {fqn} AS t
        WHERE t.{geom} IS NOT NULL
        {extra}
        {clip_sql}
    """
    try:
        with engine.connect() as conn:
            if not _table_exists(conn, layer):
                return JSONResponse(EMPTY_FC)
            rows = conn.execute(text(sql), params).mappings().all()
    except Exception as exc:
        logger.exception("Centroïdes couche %s", layer_key)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    features: list[dict[str, Any]] = []
    for row in rows:
        geom_json = row.get("geometry")
        if geom_json is None:
            continue
        props: dict[str, Any] = {"id": _json_safe(row.get("id")), "fid": _json_safe(row.get("id"))}
        for col in keep:
            props[col] = _json_safe(row.get(col))
        features.append(
            {"type": "Feature", "id": row.get("id"), "geometry": geom_json, "properties": props}
        )

    body: dict[str, Any] = {"type": "FeatureCollection", "features": features}
    _centroid_cache[layer_key] = (now, body)
    return JSONResponse(body, headers={"Cache-Control": "private, max-age=120"})


def _simplify_expr(geom_sql: str, z: int) -> str:
    # z14+ : ST_AsMVTGeom simplifie déjà à la résolution tuile.
    # ST_SimplifyPreserveTopology sur des centaines de multipolygones est trop coûteux.
    tol = {12: 2.0, 13: 0.8}.get(z)
    if tol is None:
        return geom_sql
    return f"ST_SimplifyPreserveTopology({geom_sql}, {tol})"


def _fetch_tile_sync(layer: InternalLayer, z: int, x: int, y: int) -> bytes:
    fqn = _fqn(layer)
    geom_2154 = _ident(layer.geom_2154)
    geom_3857 = _ident(layer.geom_3857)
    fid_sql = layer.feature_id_sql or f"t.{_ident(layer.id_column)}"
    prop_sql = ", ".join(f"t.{_ident(c)}" for c in layer.properties)
    geom_src = _simplify_expr(f"ST_Force2D(t.{geom_2154})", z)

    with engine.connect() as conn:
        use_3857 = _has_3857_cache.get(layer.key)
        if use_3857 is None:
            use_3857 = _has_column(conn, layer, layer.geom_3857)
            _has_3857_cache[layer.key] = use_3857
        if use_3857:
            bounds_cte = "SELECT ST_TileEnvelope(:z, :x, :y) AS g3857"
            spatial = f"t.{geom_3857} && b.g3857"
            geom_mvt = f"ST_AsMVTGeom(t.{geom_3857}, b.g3857, 4096, 256, TRUE)"
        else:
            bounds_cte = (
                "SELECT ST_TileEnvelope(:z, :x, :y) AS g3857, "
                "ST_Transform(ST_TileEnvelope(:z, :x, :y), 2154) AS g2154"
            )
            spatial = f"t.{geom_2154} && b.g2154"
            geom_mvt = f"ST_AsMVTGeom(ST_Transform({geom_src}, 3857), b.g3857, 4096, 256, TRUE)"

        order_sql = f"ORDER BY {layer.mvt_order_sql}" if layer.mvt_order_sql else ""
        clip_sql, clip_params = _clip_sql(layer, geom_2154)
        sql = text(
            f"""
            WITH bounds AS ({bounds_cte}),
            src AS (
                SELECT
                    {fid_sql} AS fid,
                    {prop_sql},
                    {geom_mvt} AS geom
                FROM {fqn} AS t
                CROSS JOIN bounds b
                WHERE t.{geom_2154} IS NOT NULL
                  AND {spatial}
                  {_extra_where(layer)}
                  {clip_sql}
                {order_sql}
            )
            SELECT ST_AsMVT(src, '{MVT_SOURCE_LAYER}', 4096, 'geom', 'fid') AS tile
            FROM src
            WHERE geom IS NOT NULL
            """
        )
        tile = conn.execute(sql, {"z": z, "x": x, "y": y, **clip_params}).scalar_one_or_none()
    return bytes(tile) if tile else b""


@router.get("/layers/{layer_key}/tiles/{z}/{x}/{y}.mvt")
async def layer_tile(layer_key: str, z: int, x: int, y: int) -> Response:
    layer = get_layer(layer_key)
    if layer is None:
        raise HTTPException(status_code=404, detail=f"Couche inconnue : {layer_key}")
    if layer.delivery not in ("mvt", "mbtiles"):
        raise HTTPException(status_code=400, detail="Cette couche n'est pas servie en tuiles")
    if z < 0 or z > 22 or x < 0 or y < 0:
        raise HTTPException(status_code=400, detail="coordonnées de tuile invalides")
    n = 1 << z
    if x >= n or y >= n:
        raise HTTPException(status_code=400, detail="coordonnées de tuile hors zoom")

    min_z = layer.min_zoom or 0
    headers = {"Cache-Control": "private, max-age=120"}
    if z < min_z:
        return Response(content=b"", media_type="application/vnd.mapbox-vector-tile", headers=headers)

    cache_key = (layer.key, z, x, y)
    cached = _tile_cache.get(cache_key)
    if cached is not None:
        return Response(
            content=cached,
            media_type="application/vnd.mapbox-vector-tile",
            headers={**headers, "X-Cache": "HIT"},
        )

    t0 = time.perf_counter()
    source = "live"
    try:
        tile_bytes: bytes | None = None
        if layer.delivery == "mbtiles":
            mb_path = await run_in_threadpool(ensure_local_mbtiles, layer)
            if mb_path is not None:
                tile_bytes = _read_mbtiles_tile(mb_path, z, x, y)
                if tile_bytes is not None:
                    source = "mbtiles"
        if tile_bytes is None:
            tile_bytes = await run_in_threadpool(_fetch_tile_sync, layer, z, x, y)
            source = "live"
    except Exception as exc:
        logger.exception("MVT couche %s z=%s x=%s y=%s", layer_key, z, x, y)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    _tile_cache.set(cache_key, tile_bytes)
    logger.info(
        "MVT %s src=%s z=%d x=%d y=%d size=%d ms=%.0f",
        layer_key,
        source,
        z,
        x,
        y,
        len(tile_bytes),
        (time.perf_counter() - t0) * 1000,
    )
    return Response(
        content=tile_bytes,
        media_type="application/vnd.mapbox-vector-tile",
        headers={**headers, "X-Cache": "MISS"},
    )
