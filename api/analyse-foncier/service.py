"""Analyse de foncier : le fichier déposé devient le pool, puis les profilers habituels tournent.

Le filtrage (recherche) n'est pas appelé. Le projet créé est isolé : les tables
``parcelles`` / ``parcelles_pool`` existantes d'une recherche ne sont pas réécrites.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from sqlalchemy import text

from api.recherche_de_foncier.enrichir.pool import pool_service, profiling_service
from api.recherche_de_foncier.enrichir.pool.profilers.hydros import HydrosProfiler
from api.recherche_de_foncier.filtrage.layers.compensation_zone_humide import (
    aoi_to_surfaces_hydros,
    aoi_to_troncons_hydros,
)
from api.recherche_de_foncier.filtrage.layers.faune import criterion_names
from api.recherche_de_foncier.filtrage.layers.filter_pipeline import _enrich_survivors, ensure_columns

logger = logging.getLogger(__name__)

MAX_FEATURES = 800

_SQL_NEAREST_FLORA = """
UPDATE ecocompensation_results.parcelles p
SET flora_distances = flora_distances || jsonb_build_object(
    CAST(:label AS text),
    COALESCE(
        (
            SELECT ROUND(ST_Distance(p.geom_2154, o.geom))::int
            FROM ecocompensation.flore_observation o
            WHERE o.taxon_key = :taxon_key
              AND o.geom IS NOT NULL
            ORDER BY p.geom_2154 <-> o.geom
            LIMIT 1
        ),
        -1
    )
)
WHERE p.project_id = CAST(:pid AS uuid)
  AND p.idu = ANY(:idus)
"""


def _as_multi(geom: BaseGeometry) -> BaseGeometry | None:
    if geom is None or geom.is_empty:
        return None
    if isinstance(geom, Polygon):
        geom = MultiPolygon([geom])
    if not geom.is_valid:
        geom = geom.buffer(0)
    if geom is None or geom.is_empty:
        return None
    return geom


def _feature_idu(props: dict[str, Any], index: int, seen: set[str]) -> str:
    raw = props.get("idu") or props.get("IDU") or props.get("fid") or props.get("FID")
    base = str(raw).strip() if raw not in (None, "") else f"af-{index:04d}"
    base = base[:64] or f"af-{index:04d}"
    candidate = base
    n = 2
    while candidate in seen:
        candidate = f"{base[:58]}-{n}"
        n += 1
    seen.add(candidate)
    return candidate


def _create_project(conn, name: str, projet_wkt: str) -> tuple[str, str]:
    project_id = str(uuid.uuid4())
    aoi_id = str(
        conn.execute(
            text(
                """
                INSERT INTO ecocompensation.aoi (code_insee, buffer_m, geom_2154)
                VALUES ('USER', 0, ST_Multi(ST_MakeValid(ST_GeomFromText(:wkt, 2154))))
                RETURNING id
                """
            ),
            {"wkt": projet_wkt},
        ).scalar_one()
    )
    conn.execute(
        text(
            """
            INSERT INTO ecocompensation.projects (id, name, aoi_id, status, study_type)
            VALUES (CAST(:pid AS uuid), :name, CAST(:aoi AS uuid), 'created', 'analyse')
            """
        ),
        {"pid": project_id, "name": name, "aoi": aoi_id},
    )
    return project_id, aoi_id


def _insert_foncier(conn, project_id: str, features: list[tuple[str, BaseGeometry]]) -> None:
    for idu, geom in features:
        conn.execute(
            text(
                """
                INSERT INTO ecocompensation_results.parcelles (idu, geom_2154, project_id)
                VALUES (
                    :idu,
                    ST_Multi(ST_MakeValid(ST_GeomFromText(:wkt, 2154))),
                    CAST(:pid AS uuid)
                )
                """
            ),
            {"idu": idu, "wkt": geom.wkt, "pid": project_id},
        )


def _pool_rows(conn, project_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            """
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
                p.dist_hydro_m
            FROM ecocompensation_results.parcelles p
            JOIN ecocompensation.projects pr ON pr.id = p.project_id
            JOIN ecocompensation.aoi a ON a.id = pr.aoi_id
            WHERE p.project_id = CAST(:pid AS uuid)
            ORDER BY ST_Area(p.geom_2154) DESC
            """
        ),
        {"pid": project_id},
    ).mappings().all()
    out = []
    for i, row in enumerate(rows, 1):
        out.append(
            {
                "idu": row["idu"],
                "rank": i,
                "surface_ha": float(row["surface_ha"] or 0),
                "miller": float(row["miller"] or 0),
                "distance_km": float(row["distance_km"] or 0),
                "dist_hydro_m": (
                    float(row["dist_hydro_m"]) if row.get("dist_hydro_m") is not None else None
                ),
            }
        )
    return out


def _nearest_flora(conn, project_id: str, idus: list[str], flora: list[dict[str, str]]) -> None:
    for taxon in flora:
        conn.execute(
            text(_SQL_NEAREST_FLORA),
            {
                "pid": project_id,
                "idus": idus,
                "taxon_key": taxon["taxon_key"],
                "label": taxon["label"],
            },
        )


def _write_filter_enrich(conn, project_id: str, run_id: str) -> None:
    rows = conn.execute(
        text(
            """
            SELECT
                idu,
                veg_libelles,
                fauna_distances,
                flora_distances,
                dist_hydro_m,
                troncons_hydro_info,
                dist_surface_hydro_m,
                surface_hydro_ha,
                surfaces_hydro_info
            FROM ecocompensation_results.parcelles
            WHERE project_id = CAST(:pid AS uuid)
            """
        ),
        {"pid": project_id},
    ).mappings().all()
    for row in rows:
        payload: dict[str, Any] = {
            "veg_libelles": list(row["veg_libelles"] or []),
            "fauna_distances": dict(row["fauna_distances"] or {}),
            "flora_distances": dict(row["flora_distances"] or {}),
        }
        if row.get("dist_hydro_m") is not None:
            payload["dist_hydro_m"] = float(row["dist_hydro_m"])
        if row.get("troncons_hydro_info"):
            payload["troncons_hydro_info"] = list(row["troncons_hydro_info"] or [])
        if row.get("dist_surface_hydro_m") is not None:
            payload["dist_surface_hydro_m"] = float(row["dist_surface_hydro_m"])
        if row.get("surface_hydro_ha") is not None:
            payload["surface_hydro_ha"] = float(row["surface_hydro_ha"] or 0)
        if row.get("surfaces_hydro_info"):
            payload["surfaces_hydro_info"] = list(row["surfaces_hydro_info"] or [])
        pool_service.upsert_metric(
            conn,
            project_id=project_id,
            run_id=run_id,
            idu=str(row["idu"]),
            metric_key="filter_enrich",
            metric_value=payload,
        )


def _write_flore_metric(conn, project_id: str, run_id: str, flora: list[dict[str, str]]) -> None:
    if not flora:
        return
    rows = conn.execute(
        text(
            """
            SELECT idu, flora_distances
            FROM ecocompensation_results.parcelles
            WHERE project_id = CAST(:pid AS uuid)
            """
        ),
        {"pid": project_id},
    ).mappings().all()
    labels = {item["label"] for item in flora}
    keys = {item["taxon_key"]: item["label"] for item in flora}
    for row in rows:
        distances = dict(row["flora_distances"] or {})
        taxa = []
        for taxon_key, label in keys.items():
            raw = distances.get(label)
            try:
                dist = int(raw) if raw is not None else -1
            except (TypeError, ValueError):
                dist = -1
            taxa.append(
                {
                    "taxon_key": taxon_key,
                    "label": label,
                    "dist_m": dist,
                    "radius_m": None,
                    "within_radius": dist == 0,
                }
            )
        if not taxa and not labels:
            continue
        pool_service.upsert_metric(
            conn,
            project_id=project_id,
            run_id=run_id,
            idu=str(row["idu"]),
            metric_key="especes_flore",
            metric_value={
                "taxa": taxa,
                "intersects_any": any(t["dist_m"] == 0 for t in taxa),
                "mode": "nearest",
            },
        )


def run_analyse_foncier(
    engine,
    *,
    name: str,
    projet_gdf,
    foncier_gdf,
    occupation_sol: bool,
    hydro_troncons: bool,
    hydro_surfaces: bool,
    fauna_species: list[str],
    fauna_aliases: dict[str, list[str]] | None = None,
    flora: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    flora = flora or []
    fauna_aliases = fauna_aliases or {}
    hydro = hydro_troncons or hydro_surfaces
    projet = _as_multi(projet_gdf.union_all())
    if projet is None:
        raise ValueError("Le fichier projet ne contient aucune géométrie exploitable.")

    features: list[tuple[str, BaseGeometry]] = []
    seen: set[str] = set()
    for index, (_, row) in enumerate(foncier_gdf.iterrows(), 1):
        geom = _as_multi(row.geometry)
        if geom is None:
            continue
        props = {str(k): row[k] for k in foncier_gdf.columns if k != "geometry"}
        features.append((_feature_idu(props, index, seen), geom))
        if len(features) > MAX_FEATURES:
            raise ValueError(f"Le foncier dépasse {MAX_FEATURES} géométries.")
    if not features:
        raise ValueError("Le fichier foncier ne contient aucune géométrie exploitable.")

    with engine.begin() as conn:
        project_id, aoi_id = _create_project(conn, name, projet.wkt)
        _insert_foncier(conn, project_id, features)

    ensure_columns(engine)
    idus = [idu for idu, _geom in features]

    if hydro_troncons:
        aoi_to_troncons_hydros.run(engine, project_id, aoi_id, logger.info)
    if hydro_surfaces:
        aoi_to_surfaces_hydros.run(engine, project_id, aoi_id, logger.info)

    _enrich_survivors(
        engine,
        project_id,
        idus,
        fauna_species,
        logger.info,
        enrich_troncons_hydro=hydro_troncons,
        troncons_max_dist_m=50_000.0,
        enrich_surfaces_hydro=hydro_surfaces,
        surfaces_max_dist_m=50_000.0,
        fauna_names={
            name: criterion_names(name, fauna_aliases.get(name))
            for name in fauna_species
        },
    )
    if flora:
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL statement_timeout = '15min'"))
            _nearest_flora(conn, project_id, idus, flora)

    with engine.begin() as conn:
        pool_rows = _pool_rows(conn, project_id)

    options = {
        "pipeline": "analyse_foncier",
        "study_type": "analyse",
        "occupation_sol": occupation_sol,
        "hydro": hydro,
        "hydro_troncons": hydro_troncons,
        "hydro_surfaces": hydro_surfaces,
        "fauna_criteria": [
            {"species": name, "dist_m": None, "aliases": fauna_aliases.get(name) or []}
            for name in fauna_species
        ],
        "flora_criteria": [
            {"taxon_key": item["taxon_key"], "label": item["label"], "dist_m": None}
            for item in flora
        ],
    }
    run_id = pool_service.persist_parcelles_pool_run(
        engine,
        project_id=project_id,
        options_json=options,
        parcelles=pool_rows,
        scope="parcelles",
        keep_last=20,
        result_summary={
            "pipeline": "analyse_foncier",
            "total": len(pool_rows),
            "n_after_filter": len(pool_rows),
            "funnel": [
                {
                    "step": 0,
                    "key": "analyse_foncier",
                    "label": "Foncier déposé",
                    "count": len(pool_rows),
                }
            ],
            "filter_config": options,
        },
    )

    with engine.begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = '15min'"))
        _write_filter_enrich(conn, project_id, run_id)
        if occupation_sol or hydro or fauna_species:
            profiling_service.compute_metrics_for_run(conn, project_id, run_id)
        if hydro:
            payload = HydrosProfiler().compute_for_run(conn, project_id, run_id)
            for idu, metric in payload.items():
                pool_service.upsert_metric(
                    conn,
                    project_id=project_id,
                    run_id=run_id,
                    idu=idu,
                    metric_key=HydrosProfiler.metric_key,
                    metric_value=metric,
                )
                if metric.get("nearest_troncon_m") is not None:
                    conn.execute(
                        text(
                            """
                            UPDATE ecocompensation_results.parcelles_pool
                            SET dist_hydro_m = :dist
                            WHERE project_id = CAST(:pid AS uuid)
                              AND run_id = CAST(:rid AS uuid)
                              AND idu = :idu
                            """
                        ),
                        {
                            "dist": metric["nearest_troncon_m"],
                            "pid": project_id,
                            "rid": run_id,
                            "idu": idu,
                        },
                    )
        _write_flore_metric(conn, project_id, run_id, flora)

    return {
        "project_id": project_id,
        "aoi_id": aoi_id,
        "run_id": run_id,
        "count": len(pool_rows),
    }
