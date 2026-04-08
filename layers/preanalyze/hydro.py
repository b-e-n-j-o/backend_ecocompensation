"""
preanalyze/hydro.py
===================
Analyse hydrologique pour la parcelle cible.

Tronçons hydro (BD TOPO — table geo ou résultats) :
  - intersecte la parcelle ?
  - si non : distance au tronçon le plus proche (m)

Surfaces hydro (BD TOPO) :
  - intersecte la parcelle ?
  - si non : distance à la surface la plus proche (m)

On interroge d'abord geo.* (données sources), sinon fallback sur
ecocompensation_results.* si dispo — avec le même OR aoi_id/project_id.
"""
from __future__ import annotations
from sqlalchemy import text
from sqlalchemy.engine import Engine

# Tables sources (hors projet)
_TRONCON_SRC  = "geo.troncons_hydro"
_SURFACE_SRC  = "geo.surfaces_hydro"
# Fallback résultats (filtrés par project_id ou aoi_id)
_TRONCON_RES  = "ecocompensation_results.troncons_hydro"
_SURFACE_RES  = "ecocompensation_results.surfaces_hydro"


def _table_exists(engine: Engine, full_name: str) -> bool:
    with engine.begin() as conn:
        return conn.execute(
            text("SELECT to_regclass(:r) IS NOT NULL").execution_options(no_prepare=True),
            {"r": full_name},
        ).scalar_one()


def _resolve_table(engine: Engine, src: str, res: str) -> str | None:
    if _table_exists(engine, src):
        return src
    if _table_exists(engine, res):
        return res
    return None


def _analyze_layer(
    engine: Engine,
    table: str,
    parcel_wkt: str,
    *,
    geom_col: str = "geom_2154",
    project_id: str | None = None,
    aoi_id: str | None = None,
) -> dict:
    """
    Teste intersection puis distance si pas d'intersection.
    Retourne {"intersects": bool, "dist_m": float | None, "error": str | None}
    """
    # Clause de filtre projet/aoi si disponible (tables résultats)
    proj_filter = ""
    params: dict = {"wkt": parcel_wkt}
    if project_id and aoi_id:
        proj_filter = f"AND (t.project_id = :project_id OR t.aoi_id = :aoi_id)"
        params["project_id"] = project_id
        params["aoi_id"]     = aoi_id

    # 1. Intersection
    sql_intersect = f"""
        SELECT COUNT(*)::int
        FROM {table} t
        WHERE ST_Intersects(t.{geom_col}, ST_GeomFromText(:wkt, 2154))
        {proj_filter}
    """
    try:
        with engine.begin() as conn:
            n = conn.execute(text(sql_intersect), params).scalar_one()
    except Exception as e:
        return {"intersects": False, "dist_m": None, "error": str(e)[:200]}

    if n > 0:
        return {"intersects": True, "dist_m": 0.0, "count": n}

    # 2. Distance minimale
    sql_dist = f"""
        SELECT MIN(ST_Distance(t.{geom_col}, ST_GeomFromText(:wkt, 2154)))::double precision
        FROM {table} t
        {("WHERE " + proj_filter.lstrip("AND ")) if proj_filter else ""}
    """
    try:
        with engine.begin() as conn:
            dist = conn.execute(text(sql_dist), params).scalar_one()
    except Exception as e:
        return {"intersects": False, "dist_m": None, "error": str(e)[:200]}

    return {
        "intersects": False,
        "dist_m":     round(float(dist), 1) if dist is not None else None,
        "count":      0,
    }


def analyze_troncons_hydro(
    engine: Engine,
    parcel_wkt: str,
    project_id: str | None = None,
    aoi_id: str | None = None,
) -> dict:
    """
    Analyse tronçons hydrographiques.
    Retourne {"intersects", "dist_m", "count", "source", "error"?}
    """
    table = _resolve_table(engine, _TRONCON_SRC, _TRONCON_RES)
    if table is None:
        return {"intersects": False, "dist_m": None, "error": "Table troncons_hydro introuvable"}

    result = _analyze_layer(
        engine, table, parcel_wkt,
        project_id=project_id if table == _TRONCON_RES else None,
        aoi_id=aoi_id if table == _TRONCON_RES else None,
    )
    result["source"] = table
    return result


def analyze_surfaces_hydro(
    engine: Engine,
    parcel_wkt: str,
    project_id: str | None = None,
    aoi_id: str | None = None,
) -> dict:
    """
    Analyse surfaces hydrographiques.
    Retourne {"intersects", "dist_m", "count", "source", "error"?}
    """
    table = _resolve_table(engine, _SURFACE_SRC, _SURFACE_RES)
    if table is None:
        return {"intersects": False, "dist_m": None, "error": "Table surfaces_hydro introuvable"}

    result = _analyze_layer(
        engine, table, parcel_wkt,
        project_id=project_id if table == _SURFACE_RES else None,
        aoi_id=aoi_id if table == _SURFACE_RES else None,
    )
    result["source"] = table
    return result