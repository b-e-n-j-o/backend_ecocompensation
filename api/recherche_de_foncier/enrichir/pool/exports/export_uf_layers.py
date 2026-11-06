"""
export_uf_layers.py
===================

Couches SIG des unités foncières PM, avec la même table attributaire que l'export parcelles :

  unites_foncieres  1 polygone = union des parcelles de l'UF, attributs agrégés (règles :
                    uf_export_aggregate.COLUMNS)
  uf_parcelles      1 polygone = une parcelle, mêmes colonnes que la couche `parcelles`
                    + uf_id / uf_rang / uf_type
  dictionnaire      table sans géométrie : colonnes, description, unité, règle UF

Les combinaisons (sous-ensembles) ne sont plus exportées.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

import geopandas as gpd
import pandas as pd
from shapely import wkb as shapely_wkb
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from sqlalchemy import text

from api.recherche_de_foncier.enrichir.pool.exports.classement_export_attrs import (
    build_parcelle_export_row,
    mmap_for_parcelle,
)
from api.recherche_de_foncier.enrichir.pool.exports.project_context_layers import (
    LAYER_ZONE_PROJET,
    load_project_context_layers,
)
from api.recherche_de_foncier.enrichir.pool.exports.uf_export_adapter import iter_uf_summaries
from api.recherche_de_foncier.enrichir.pool.exports.uf_export_aggregate import (
    UF_LAYER_KEYS,
    UF_PARC_LAYER_KEYS,
    ParcelItem,
    aggregate_uf_row,
    dictionary_rows,
    uf_parcel_row,
)
from api.recherche_de_foncier.enrichir.pool.exports.uf_export_metrics import (
    attach_filter_fields,
    ensure_uf_parcel_metrics,
    load_uf_parcels,
)

if TYPE_CHECKING:
    from sqlalchemy import Engine

logger = logging.getLogger(__name__)
Log = Callable[[str], None]


@dataclass
class UfExport:
    unites_foncieres: gpd.GeoDataFrame
    uf_parcelles: gpd.GeoDataFrame
    unites_foncieres_shp: gpd.GeoDataFrame   # textes tronqués 254 c.
    uf_parcelles_shp: gpd.GeoDataFrame
    dictionnaire: pd.DataFrame
    metrics_run_id: str


def _as_shapely(geom: Any):
    if geom is None or isinstance(geom, BaseGeometry):
        return geom
    if isinstance(geom, memoryview):
        geom = bytes(geom)
    if isinstance(geom, bytes):
        return shapely_wkb.loads(geom)
    return shapely_wkb.loads(geom, hex=True)


def _load_uf_filter(engine, project_id: str) -> dict:
    with engine.connect() as conn:
        raw = conn.execute(
            text("SELECT last_filter_uf FROM ecocompensation.projects WHERE id = CAST(:pid AS uuid)"),
            {"pid": project_id},
        ).scalar()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    return raw if isinstance(raw, dict) else {}


def _project_centre(engine, project_id: str) -> tuple[float, float] | None:
    """Centre du projet = centroïde de la zone projet (même source que la couche zone_projet)."""
    ctx = load_project_context_layers(engine, project_id)
    gdf = ctx.get(LAYER_ZONE_PROJET)
    if gdf is None or gdf.empty:
        return None
    c = unary_union(list(gdf.geometry)).centroid
    return (c.x, c.y)


def build_uf_export(
    engine: "Engine",
    project_id: str,
    results_uf: dict,
    *,
    force_metrics: bool = False,
    log: Log | None = None,
) -> UfExport:
    from filtre_options import FiltreOptions

    log = log or (lambda m: logger.info("[uf_export] project_id=%s %s", project_id, m))
    t0 = time.perf_counter()
    summaries = iter_uf_summaries(results_uf)
    if not summaries:
        raise ValueError("Aucune unité foncière dans les résultats UF.")

    retained = {u["uf_id"]: {str(p["idu"]) for p in u["parcelles"] if p.get("idu")} for u in summaries}
    uf_info = {
        u["uf_id"]: {
            "uf_id": u["uf_id"], "uf_rang": u["uf_rang"], "uf_type": "PM",
            "dist_centre_km": u["dist_centre_km"], "score_eco": u["score_eco"], "score_eco_max": u["score_eco_max"],
            "siren": u["siren"], "denomination": u["denomination"],
            "forme_juridique": u["forme_juridique"], "deja_compense": u["deja_compense"],
        }
        for u in summaries
    }
    log(f"[UF_EXPORT] {len(summaries)} UF, {sum(len(v) for v in retained.values())} parcelles retenues")

    options_raw = _load_uf_filter(engine, project_id)
    options = FiltreOptions.from_dict(options_raw)
    parcels = load_uf_parcels(engine, project_id, retained, _project_centre(engine, project_id))
    if not parcels:
        raise ValueError("Aucune géométrie de parcelle UF trouvée en base pour ce classement.")

    species = [c["species"] for c in options.faune_criteria if c.get("species")]
    attach_filter_fields(engine, project_id, parcels, species, log)
    run_id, metrics_by_idu = ensure_uf_parcel_metrics(
        engine, project_id, parcels, options_raw, force=force_metrics, log=log
    )

    # lignes parcelles (même builder que la couche `parcelles`)
    items_full: dict[str, list[ParcelItem]] = {}
    items_shp: dict[str, list[ParcelItem]] = {}
    geoms: dict[str, list] = {}
    for p in parcels:
        info = uf_info[p["uf_id"]]
        parcelle = {**p, "rank": info["uf_rang"]}
        mmap = mmap_for_parcelle(parcelle, metrics_by_idu)
        items_full.setdefault(p["uf_id"], []).append(
            ParcelItem(build_parcelle_export_row(parcelle, mmap, options, clip_for_shapefile=False), mmap))
        items_shp.setdefault(p["uf_id"], []).append(
            ParcelItem(build_parcelle_export_row(parcelle, mmap, options, clip_for_shapefile=True), mmap))
        geoms.setdefault(p["uf_id"], []).append(_as_shapely(p["geom_2154"]))

    uf_rows, uf_rows_shp, uf_geoms = [], [], []
    parc_rows, parc_rows_shp, parc_geoms = [], [], []
    for u in summaries:          # ordre du classement
        uid = u["uf_id"]
        if uid not in items_full:
            log(f"[UF_EXPORT] ⚠ UF {uid} : aucune géométrie de parcelle, ignorée")
            continue
        union = unary_union(geoms[uid])
        kw = dict(uf_area_m2=union.area, uf_perimeter_m=union.length)
        uf_rows.append(aggregate_uf_row(items_full[uid], uf_info[uid], **kw))
        uf_rows_shp.append(aggregate_uf_row(items_shp[uid], uf_info[uid], clip_for_shapefile=True, **kw))
        uf_geoms.append(union)
        for it_f, it_s, g in zip(items_full[uid], items_shp[uid], geoms[uid]):
            parc_rows.append(uf_parcel_row(it_f, uf_info[uid]))
            parc_rows_shp.append(uf_parcel_row(it_s, uf_info[uid]))
            parc_geoms.append(g)

    def gdf(rows, gs, cols):
        return gpd.GeoDataFrame(pd.DataFrame(rows, columns=list(cols)), geometry=gs, crs="EPSG:2154")

    out = UfExport(
        unites_foncieres=gdf(uf_rows, uf_geoms, UF_LAYER_KEYS),
        uf_parcelles=gdf(parc_rows, parc_geoms, UF_PARC_LAYER_KEYS),
        unites_foncieres_shp=gdf(uf_rows_shp, uf_geoms, UF_LAYER_KEYS),
        uf_parcelles_shp=gdf(parc_rows_shp, parc_geoms, UF_PARC_LAYER_KEYS),
        dictionnaire=pd.DataFrame(dictionary_rows()),
        metrics_run_id=run_id,
    )
    log(f"[UF_EXPORT] couches prêtes : {len(uf_rows)} UF, {len(parc_rows)} parcelles "
        f"en {time.perf_counter() - t0:.1f}s")
    return out


def write_attribute_table(gpkg_path, df: pd.DataFrame, layer: str) -> None:
    """Ajoute une table sans géométrie au GeoPackage (lisible dans QGIS comme une table)."""
    import pyogrio

    pyogrio.write_dataframe(df, str(gpkg_path), layer=layer, driver="GPKG", append=False)
