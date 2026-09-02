"""Ajout organique de parcelles à un run pool déjà calculé.

Le filtrage n'est pas rejoué : la parcelle est copiée (cadastre local, sinon WFS
IGN), enrichie comme les survivantes du run, puis profilée (PM, score éco, etc.).
La dureté foncière reste à la demande.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import requests
from shapely.geometry import shape
from sqlalchemy import text

from layers.filter_pipeline import _enrich_survivors
from pool import pool_service, profiling_service

logger = logging.getLogger(__name__)

_IDU_RE = re.compile(r"^[A-Z0-9]{12,15}$")
_SAFE_CQL = re.compile(r"^[A-Z0-9]+$")
_WFS_URL = "https://data.geopf.fr/wfs/ows"
_WFS_TYPENAME = "CADASTRALPARCELS.PARCELLAIRE_EXPRESS:parcelle"

_SQL_EXISTS_RESULTS = """
SELECT 1
FROM ecocompensation_results.parcelles
WHERE project_id = CAST(:pid AS uuid)
  AND idu = :idu
LIMIT 1
"""

_SQL_COPY_CADASTRE = """
INSERT INTO ecocompensation_results.parcelles (
    id, gid, numero, feuille, section, code_dep, nom_com,
    code_com, com_abs, code_arr, idu, contenance, code_insee,
    geom_2154, project_id
)
SELECT DISTINCT ON (p.idu)
    p.id, p.gid, p.numero, p.feuille, p.section, p.code_dep,
    p.nom_com, p.code_com, p.com_abs, p.code_arr, p.idu,
    p.contenance, p.code_insee,
    ST_Multi(ST_MakeValid(p.geom_2154)),
    CAST(:pid AS uuid)
FROM ecocompensation.parcelles p
WHERE p.idu = :idu
  AND p.geom_2154 IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM ecocompensation_results.parcelles r
      WHERE r.project_id = CAST(:pid AS uuid)
        AND r.idu = :idu
  )
ORDER BY p.idu, ST_Area(p.geom_2154) DESC NULLS LAST
"""

_SQL_INSERT_WFS = """
INSERT INTO ecocompensation_results.parcelles (
    idu, numero, section, code_insee, nom_com, contenance,
    geom_2154, project_id
)
SELECT
    :idu, :numero, :section, :code_insee, :nom_com,
    ST_Area(ST_Multi(ST_MakeValid(ST_GeomFromText(:wkt, 2154)))),
    ST_Multi(ST_MakeValid(ST_GeomFromText(:wkt, 2154))),
    CAST(:pid AS uuid)
WHERE NOT EXISTS (
    SELECT 1
    FROM ecocompensation_results.parcelles r
    WHERE r.project_id = CAST(:pid AS uuid)
      AND r.idu = :idu
)
"""

_SQL_POOL_ATTRS = """
SELECT
    p.idu,
    ROUND((ST_Area(p.geom_2154) / 10000.0)::numeric, 4) AS surface_ha,
    ROUND((
        (4.0 * PI() * ST_Area(p.geom_2154))
        / NULLIF(ST_Perimeter(p.geom_2154)^2, 0)
    )::numeric, 4) AS miller,
    ROUND((
        ST_Distance(ST_Centroid(p.geom_2154), ST_Centroid(a.geom_2154)) / 1000.0
    )::numeric, 3) AS distance_km,
    p.veg_libelles,
    p.fauna_distances,
    COALESCE(p.zone_humide_ha, 0) AS zone_humide_ha,
    p.dist_hydro_m,
    COALESCE(p.troncons_hydro_info, '[]'::jsonb) AS troncons_hydro_info,
    p.dist_surface_hydro_m,
    COALESCE(p.surface_hydro_ha, 0) AS surface_hydro_ha,
    COALESCE(p.surfaces_hydro_info, '[]'::jsonb) AS surfaces_hydro_info
FROM ecocompensation_results.parcelles p
LEFT JOIN ecocompensation.aoi a ON a.id = CAST(:aid AS uuid)
WHERE p.project_id = CAST(:pid AS uuid)
  AND p.idu = :idu
LIMIT 1
"""


def normalize_idu(raw: str) -> str | None:
    s = re.sub(r"[^A-Za-z0-9]", "", str(raw or "")).upper()
    if _IDU_RE.fullmatch(s):
        return s
    return None


def parse_idu_parts(idu: str) -> tuple[str, str, str]:
    return idu[:5], idu[8:10] if len(idu) >= 10 else "", idu[-4:]


def _log(msg: str) -> None:
    logger.info("[add_parcelles] %s", msg)


def _fetch_parcelle_wfs(code_insee: str, section: str, numero: str) -> tuple[str, dict[str, Any]] | None:
    if not (_SAFE_CQL.fullmatch(code_insee) and _SAFE_CQL.fullmatch(section) and _SAFE_CQL.fullmatch(numero)):
        return None
    try:
        resp = requests.get(
            _WFS_URL,
            params={
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetFeature",
                "typeNames": _WFS_TYPENAME,
                "srsName": "EPSG:2154",
                "outputFormat": "application/json",
                "CQL_FILTER": (
                    f"code_insee='{code_insee}' AND section='{section}' AND numero='{numero}'"
                ),
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.exception("WFS parcelle %s/%s/%s", code_insee, section, numero)
        return None
    features = data.get("features") or []
    if not features:
        return None
    feat = features[0]
    geom_json = feat.get("geometry")
    if not geom_json:
        return None
    try:
        geom = shape(geom_json)
        if geom.is_empty:
            return None
        xmin, ymin, _, _ = geom.bounds
        if abs(xmin) <= 180 and abs(ymin) <= 90:
            import geopandas as gpd
            gdf = gpd.GeoDataFrame({"g": [geom]}, geometry="g", crs="EPSG:4326")
            geom = gdf.to_crs("EPSG:2154").geometry.iloc[0]
        wkt = geom.wkt
    except Exception:
        logger.exception("Géométrie WFS invalide %s/%s/%s", code_insee, section, numero)
        return None
    props = feat.get("properties") or {}
    return wkt, props if isinstance(props, dict) else {}


def _has_results_geom(conn, project_id: str, idu: str) -> bool:
    return bool(conn.execute(text(_SQL_EXISTS_RESULTS), {"pid": project_id, "idu": idu}).first())


def _copy_from_cadastre(conn, project_id: str, idu: str) -> bool:
    copied = conn.execute(text(_SQL_COPY_CADASTRE), {"pid": project_id, "idu": idu})
    return bool(copied.rowcount)


def _insert_wfs_geom(conn, project_id: str, idu: str, wkt: str, props: dict[str, Any]) -> bool:
    insee, section, numero = parse_idu_parts(idu)
    conn.execute(
        text(_SQL_INSERT_WFS),
        {
            "pid": project_id,
            "idu": idu,
            "numero": str(props.get("numero") or numero),
            "section": str(props.get("section") or section),
            "code_insee": str(props.get("code_insee") or insee),
            "nom_com": str(props.get("nom_com") or "") or None,
            "wkt": wkt,
        },
    )
    return _has_results_geom(conn, project_id, idu)


def _insert_pool_row(
    conn,
    *,
    project_id: str,
    run_id: str,
    aoi_id: str,
    idu: str,
    rank: int,
    source: str,
) -> dict[str, Any] | None:
    attrs = _pool_attrs(conn, project_id, aoi_id, idu)
    if not attrs:
        return None
    pool_service.insert_pool_parcelles(
        conn,
        project_id=project_id,
        run_id=run_id,
        parcelles=[
            {
                "idu": idu,
                "rank": rank,
                "surface_ha": float(attrs.get("surface_ha") or 0),
                "miller": float(attrs.get("miller") or 0),
                "distance_km": float(attrs.get("distance_km") or 0),
                "dist_hydro_m": (
                    float(attrs["dist_hydro_m"])
                    if attrs.get("dist_hydro_m") is not None
                    else None
                ),
            }
        ],
    )
    pool_service.upsert_metric(
        conn,
        project_id=project_id,
        run_id=run_id,
        idu=idu,
        metric_key="pool_origin",
        metric_value={
            "source": "manual_idu",
            "geom_source": source,
            "added_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return attrs


def _pool_attrs(conn, project_id: str, aoi_id: str, idu: str) -> dict[str, Any] | None:
    row = conn.execute(
        text(_SQL_POOL_ATTRS),
        {"pid": project_id, "aid": aoi_id or "", "idu": idu},
    ).mappings().first()
    if not row:
        return None
    return dict(row)


def _upsert_filter_enrich(conn, project_id: str, run_id: str, idu: str, attrs: dict[str, Any]) -> None:
    enrich: dict[str, Any] = {}
    veg = list(attrs.get("veg_libelles") or [])
    fauna = dict(attrs.get("fauna_distances") or {})
    if veg:
        enrich["veg_libelles"] = veg
    if fauna:
        enrich["fauna_distances"] = fauna
    if attrs.get("zone_humide_ha") is not None:
        enrich["zone_humide_ha"] = float(attrs["zone_humide_ha"] or 0)
    if attrs.get("dist_hydro_m") is not None:
        enrich["dist_hydro_m"] = float(attrs["dist_hydro_m"])
    hydro_info = list(attrs.get("troncons_hydro_info") or [])
    if hydro_info:
        enrich["troncons_hydro_info"] = hydro_info
    if attrs.get("dist_surface_hydro_m") is not None:
        enrich["dist_surface_hydro_m"] = float(attrs["dist_surface_hydro_m"])
    if attrs.get("surface_hydro_ha") is not None:
        enrich["surface_hydro_ha"] = float(attrs["surface_hydro_ha"] or 0)
    surf_info = list(attrs.get("surfaces_hydro_info") or [])
    if surf_info:
        enrich["surfaces_hydro_info"] = surf_info
    if not enrich:
        return
    pool_service.upsert_metric(
        conn,
        project_id=project_id,
        run_id=run_id,
        idu=idu,
        metric_key="filter_enrich",
        metric_value=enrich,
    )


def _update_run_total(conn, project_id: str, run_id: str) -> int:
    row = conn.execute(
        text(
            """
            UPDATE ecocompensation_results.parcelles_pool_runs
            SET total_count = (
                SELECT COUNT(*)
                FROM ecocompensation_results.parcelles_pool
                WHERE run_id = CAST(:run_id AS uuid)
            )
            WHERE id = CAST(:run_id AS uuid)
              AND project_id = CAST(:project_id AS uuid)
            RETURNING total_count
            """
        ),
        {"run_id": run_id, "project_id": project_id},
    ).mappings().first()
    return int(row["total_count"]) if row else 0


def _maybe_update_last_results(conn, project_id: str, run_id: str, aoi_id: str) -> None:
    row = conn.execute(
        text(
            """
            SELECT last_results
            FROM ecocompensation.projects
            WHERE id = CAST(:pid AS uuid)
            """
        ),
        {"pid": project_id},
    ).mappings().first()
    last = row.get("last_results") if row else None
    if isinstance(last, str):
        try:
            last = json.loads(last)
        except json.JSONDecodeError:
            last = None
    if not isinstance(last, dict):
        return
    if str(last.get("pool_run_id") or "") != str(run_id):
        return
    parcelles = pool_service.get_parcelles_for_run_results(conn, project_id, run_id, aoi_id)
    last["parcelles"] = parcelles
    last["total"] = len(parcelles)
    conn.execute(
        text(
            """
            UPDATE ecocompensation.projects
            SET last_results = CAST(:r AS jsonb), updated_at = now()
            WHERE id = CAST(:pid AS uuid)
            """
        ),
        {"pid": project_id, "r": json.dumps(last, ensure_ascii=False, default=str)},
    )


def add_idus_to_pool_run(
    engine,
    *,
    project_id: str,
    run_id: str,
    idus: list[str],
) -> dict[str, Any]:
    t0 = time.perf_counter()
    wanted: list[str] = []
    seen: set[str] = set()
    invalid: list[str] = []
    for raw in idus:
        nidu = normalize_idu(raw)
        if not nidu:
            if str(raw).strip():
                invalid.append(str(raw).strip())
            continue
        if nidu in seen:
            continue
        seen.add(nidu)
        wanted.append(nidu)

    added: list[str] = []
    already_in_pool: list[str] = []
    not_found: list[str] = []
    unstuck_indesirables: list[str] = []
    sources: dict[str, str] = {}
    to_enrich: list[str] = []
    options_json: dict[str, Any] = {}
    aoi_id = ""
    total_count = 0
    pending: list[str] = []
    next_rank = 0
    indesirable: set[str] = set()
    wfs_needed: list[str] = []

    with engine.begin() as conn:
        pool_service.ensure_tables(conn)
        meta = pool_service.get_run_meta(conn, project_id, run_id)
        if not meta or str(meta.get("scope") or "parcelles") != "parcelles":
            return {
                "ok": False,
                "error": "run_not_found",
                "added": [],
                "already_in_pool": [],
                "not_found": [],
                "invalid": invalid,
                "unstuck_indesirables": [],
                "total_count": 0,
                "duration_s": round(time.perf_counter() - t0, 2),
            }
        options_json = meta.get("options_json") or {}
        if not isinstance(options_json, dict):
            options_json = {}
        aoi_row = conn.execute(
            text("SELECT aoi_id FROM ecocompensation.projects WHERE id = CAST(:pid AS uuid)"),
            {"pid": project_id},
        ).mappings().first()
        aoi_id = str(aoi_row["aoi_id"]) if aoi_row and aoi_row.get("aoi_id") else ""

        pool_rows = pool_service.get_pool(conn, project_id=project_id, run_id=run_id)
        pool_idus = {str(r["idu"]) for r in pool_rows if r.get("idu")}
        next_rank = max((int(r["rank"] or 0) for r in pool_rows), default=0)
        indesirable = set(pool_service.list_project_indesirable_idus(conn, project_id))

        for idu in wanted:
            if idu in pool_idus:
                already_in_pool.append(idu)
                if idu in indesirable and pool_service.remove_project_indesirable(conn, project_id, idu):
                    unstuck_indesirables.append(idu)
                continue
            pending.append(idu)
            if _has_results_geom(conn, project_id, idu):
                sources[idu] = "results"
            elif _copy_from_cadastre(conn, project_id, idu):
                sources[idu] = "cadastre"
            else:
                wfs_needed.append(idu)

        total_count = _update_run_total(conn, project_id, run_id)

    wfs_payloads: dict[str, tuple[str, dict[str, Any]]] = {}
    for idu in wfs_needed:
        insee, section, numero = parse_idu_parts(idu)
        fetched = _fetch_parcelle_wfs(insee, section, numero)
        if fetched:
            wfs_payloads[idu] = fetched
        else:
            not_found.append(idu)

    with engine.begin() as conn:
        for idu, (wkt, props) in wfs_payloads.items():
            if _insert_wfs_geom(conn, project_id, idu, wkt, props):
                sources[idu] = "wfs"
            else:
                not_found.append(idu)
        for idu in pending:
            if idu in not_found:
                continue
            source = sources.get(idu)
            if not source or not _has_results_geom(conn, project_id, idu):
                not_found.append(idu)
                continue
            next_rank += 1
            attrs = _insert_pool_row(
                conn,
                project_id=project_id,
                run_id=run_id,
                aoi_id=aoi_id,
                idu=idu,
                rank=next_rank,
                source=source,
            )
            if not attrs:
                not_found.append(idu)
                continue
            if idu in indesirable and pool_service.remove_project_indesirable(conn, project_id, idu):
                unstuck_indesirables.append(idu)
            added.append(idu)
            to_enrich.append(idu)
        total_count = _update_run_total(conn, project_id, run_id)

    if to_enrich:
        fauna_criteria = options_json.get("fauna_criteria") or []
        species_list = [
            str(fc.get("species") or "").strip()
            for fc in fauna_criteria
            if isinstance(fc, dict) and str(fc.get("species") or "").strip()
        ]
        zh_mode = str(options_json.get("zone_humide_mode") or "ignore")
        troncons = options_json.get("troncons_hydros_max_dist_m")
        surfaces = options_json.get("surfaces_hydros_max_dist_m")
        try:
            troncons_m = float(troncons) if troncons is not None else None
        except (TypeError, ValueError):
            troncons_m = None
        try:
            surfaces_m = float(surfaces) if surfaces is not None else None
        except (TypeError, ValueError):
            surfaces_m = None
        _log(f"enrich start n={len(to_enrich)} species={species_list}")
        _enrich_survivors(
            engine,
            project_id,
            to_enrich,
            species_list,
            _log,
            enrich_zone_humide=zh_mode in ("intersect", "exclude"),
            enrich_troncons_hydro=troncons_m is not None,
            troncons_max_dist_m=max(troncons_m or 0.0, 0.0),
            enrich_surfaces_hydro=surfaces_m is not None,
            surfaces_max_dist_m=max(surfaces_m or 0.0, 0.0),
        )
        with engine.begin() as conn:
            for idu in to_enrich:
                attrs = _pool_attrs(conn, project_id, aoi_id, idu)
                if attrs:
                    _upsert_filter_enrich(conn, project_id, run_id, idu, attrs)
            profiling_service.compute_metrics_for_run(conn, project_id=project_id, run_id=run_id)
            _maybe_update_last_results(conn, project_id, run_id, aoi_id)
            total_count = _update_run_total(conn, project_id, run_id)
    elif unstuck_indesirables:
        with engine.begin() as conn:
            _maybe_update_last_results(conn, project_id, run_id, aoi_id)

    duration_s = round(time.perf_counter() - t0, 2)
    _log(
        f"done run={run_id} added={added} already={already_in_pool} "
        f"not_found={not_found} unstuck={unstuck_indesirables} {duration_s}s"
    )
    return {
        "ok": True,
        "added": added,
        "already_in_pool": already_in_pool,
        "not_found": not_found,
        "invalid": invalid,
        "unstuck_indesirables": unstuck_indesirables,
        "sources": sources,
        "total_count": total_count,
        "duration_s": duration_s,
    }
