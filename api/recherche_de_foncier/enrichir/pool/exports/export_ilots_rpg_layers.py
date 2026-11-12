#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Couches QGIS des îlots RPG (EPSG:2154).

  ilots_rpg        union cadastrale de l'îlot (une entité = un îlot)
  ilots_parcelles  parcelles cadastrales membres, liées par num_ilot
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import geopandas as gpd
from shapely import wkb as shapely_wkb
from shapely.geometry.base import BaseGeometry
from sqlalchemy import text

from api.recherche_de_foncier.filtrage.layers.rpg.rpg_ilots.api import get_results, rank_owners
from api.recherche_de_foncier.filtrage.layers.rpg.rpg_ilots.params import RES_TABLE, SRC_SCHEMA

if TYPE_CHECKING:
    from sqlalchemy import Engine


def _as_shapely(geom: Any):
    """PostGIS via SQLAlchemy brut : EWKB hex / bytes, pas un objet Shapely."""
    if geom is None or isinstance(geom, BaseGeometry):
        return geom
    if isinstance(geom, memoryview):
        geom = bytes(geom)
    if isinstance(geom, bytes):
        return shapely_wkb.loads(geom)
    if isinstance(geom, str):
        return shapely_wkb.loads(geom, hex=True)
    return geom


def _gdf(rows: list[dict], geoms) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(rows, geometry=[_as_shapely(g) for g in geoms], crs="EPSG:2154")


def build_ilots_geodataframes(
    engine: "Engine",
    project_id: str,
    *,
    num_ilots: set[str] | None = None,
) -> dict[str, gpd.GeoDataFrame]:
    results = get_results(engine, project_id)
    ilots = (results or {}).get("ilots") or []
    if num_ilots is not None:
        ilots = [il for il in ilots if str(il.get("num_ilot")) in num_ilots]
    if not ilots:
        raise ValueError("Aucun îlot RPG dans les résultats.")
    by_id = {str(il["num_ilot"]): il for il in ilots}
    ids = list(by_id)
    millesime = str((results or {}).get("millesime") or "2025")
    membre = f"{SRC_SCHEMA}.rpg{millesime}_ilot_membre"
    params = {"pid": project_id, "ids": ids}

    with engine.connect() as conn:
        uf_rows = conn.execute(text(f"""
            SELECT num_ilot, geom_2154
            FROM {RES_TABLE}
            WHERE project_id = CAST(:pid AS uuid)
              AND num_ilot = ANY(CAST(:ids AS text[]))
              AND geom_2154 IS NOT NULL
        """), params).mappings().all()
        parc_rows = conn.execute(text(f"""
            SELECT il.num_ilot, c.idu, c.code_insee, c.section, c.numero,
                   ST_Area(c.geom) / 10000.0 AS surface_ha,
                   m.part_parcelle,
                   COALESCE(pm.owners, '{{}}') AS owners,
                   (pp.idu IS NOT NULL) AS in_pool,
                   c.geom AS geom_2154
            FROM {RES_TABLE} il
            CROSS JOIN LATERAL unnest(il.idus) AS u(idu)
            JOIN {SRC_SCHEMA}.cadastre_parcelles c ON c.idu = u.idu
            LEFT JOIN {membre} m ON m.num_ilot = il.num_ilot AND m.idu = u.idu
            LEFT JOIN LATERAL (
                SELECT array_agg(DISTINCT x.owner_key ORDER BY x.owner_key) AS owners
                FROM {SRC_SCHEMA}.pm_ilot_membre x WHERE x.idu = u.idu
            ) pm ON TRUE
            LEFT JOIN ecocompensation_results.parcelles pp
                   ON pp.project_id = il.project_id AND pp.idu = u.idu
            WHERE il.project_id = CAST(:pid AS uuid)
              AND il.num_ilot = ANY(CAST(:ids AS text[]))
              AND c.geom IS NOT NULL
        """), params).mappings().all()

    ilot_rows, ilot_geoms = [], []
    for r in uf_rows:
        il = by_id.get(str(r["num_ilot"]))
        if not il:
            continue
        score = il.get("score_eco") if isinstance(il.get("score_eco"), dict) else {}
        ilot_rows.append({
            "num_ilot": il["num_ilot"],
            "rang": il.get("rang"),
            "surface_ha": il.get("surface_ha"),
            "nb_parcelles": il.get("nb_parcelles"),
            "cat_dominante": il.get("cat_dominante"),
            "culture": il.get("code_cultu_dominant"),
            "pm_unique": bool(il.get("pm_unique")),
            "pm_dominant": il.get("pm_dominant"),
            "dist_centre_km": il.get("distance_centre_km"),
            "score_eco": score.get("total_score"),
            "score_eco_max": score.get("max_score"),
            "durete_fonciere": (il.get("durete_fonciere") or {}).get("score_final"),
        })
        ilot_geoms.append(r["geom_2154"])
    if not ilot_geoms:
        raise ValueError("Aucune géométrie d'îlot RPG en base.")

    owner_rank = rank_owners(parc_rows)
    parc_attrs, parc_geoms = [], []
    for r in parc_rows:
        il = by_id.get(str(r["num_ilot"]), {})
        ranks = owner_rank.get(r["num_ilot"], {})
        owners = sorted(r["owners"] or [], key=lambda o: ranks.get(o, 10**9))
        main = owners[0] if owners else None
        part = r["part_parcelle"]
        parc_attrs.append({
            "num_ilot": r["num_ilot"],
            "rang": il.get("rang"),
            "idu": r["idu"],
            "code_insee": r["code_insee"],
            "section": r["section"],
            "numero": r["numero"],
            "surface_ha": round(float(r["surface_ha"] or 0), 4),
            "part_ilot": float(part) if part is not None else None,
            "proprietaire": main,
            "nb_pm": len(owners),
            "in_pool": bool(r["in_pool"]),
        })
        parc_geoms.append(r["geom_2154"])

    layers: dict[str, gpd.GeoDataFrame] = {"ilots_rpg": _gdf(ilot_rows, ilot_geoms)}
    if parc_geoms:
        layers["ilots_parcelles"] = _gdf(parc_attrs, parc_geoms)
    return layers
