#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pré-analyse parcellaire (sans projet / sans écriture en base résultats).

- Charge la géométrie parcelle via WFS cadastre (même logique que main._load_parcelle_wfs).
- Construit une BBOX en EPSG:3857 à partir du **buffer** (m) autour de la parcelle :
  sert de fenêtre de requête WFS (comme les couches AOI qui utilisent la BBOX de l’AOI).
- Pour chaque couche du registre (hors parcelles / UF / sous-ensembles) :
  * récupère les entités dans cette fenêtre (WFS ou SQL selon la couche) ;
  * teste l’**intersection avec la parcelle cible** (polygone strict), pas avec le buffer ;
  * lignes / polygones : `intersects` gère correctement les tronçons (LineString).

Retour : rapport JSON par couche (compte, types géométriques, extraits d’attributs).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, asdict
from typing import Any, Callable

import geopandas as gpd
import pandas as pd
import requests
from fastapi import HTTPException
from shapely import force_2d
from shapely.geometry.base import BaseGeometry
from sqlalchemy import text
from sqlalchemy.engine import Engine

from layers.layer_runner import LAYER_REGISTRY
from layers.preanalyze.geometry import compute_geometry_metrics
from layers.preanalyze.hydro import analyze_surfaces_hydro, analyze_troncons_hydro
from layers.preanalyze.zdv import analyze_zdv

logger = logging.getLogger(__name__)

SRS_WFS = "EPSG:3857"
SRS_2154 = "EPSG:2154"

SKIP_KEYS = frozenset({"parcelles", "unites_foncieres", "sous_ensembles"})

# ── Chargement parcelle (aligné sur main._load_parcelle_wfs) ─────────────────


def _load_parcelle_wfs(code_insee: str, section: str, numero: str) -> gpd.GeoDataFrame:
    resp = requests.get(
        "https://data.geopf.fr/wfs/ows",
        params={
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": "CADASTRALPARCELS.PARCELLAIRE_EXPRESS:parcelle",
            "srsName": "EPSG:2154",
            "outputFormat": "application/json",
            "CQL_FILTER": f"code_insee='{code_insee}' AND section='{section}' AND numero='{numero}'",
        },
        timeout=60,
    )
    resp.raise_for_status()
    gdf = gpd.read_file(resp.text)
    if gdf.empty:
        raise HTTPException(404, f"Parcelle {code_insee}/{section}/{numero} introuvable")
    if gdf.crs is None or gdf.crs.to_string() != SRS_2154:
        gdf = gdf.to_crs(SRS_2154)
    return gdf


def _parcel_union(gdf: gpd.GeoDataFrame) -> BaseGeometry:
    u = gdf.union_all()
    if hasattr(u, "geoms") and len(u.geoms) == 1:
        return u.geoms[0]
    return u


def _bbox_3857_from_buffered_parcel(parcel_geom: BaseGeometry, buffer_m: float) -> tuple[float, float, float, float]:
    g = gpd.GeoDataFrame(geometry=[parcel_geom.buffer(float(buffer_m), resolution=32)], crs=SRS_2154)
    return tuple(g.to_crs(SRS_WFS).total_bounds)


def _geom_summary(gdf: gpd.GeoDataFrame) -> tuple[list[str], str | None]:
    if gdf.empty:
        return [], None
    gt = sorted({str(x) for x in gdf.geometry.geom_type.unique()})
    return gt, (" / ".join(gt) if gt else None)


def _sample_strings(gdf: gpd.GeoDataFrame, cols: list[str], k: int = 4) -> list[str]:
    out: list[str] = []
    for col in cols:
        if col not in gdf.columns:
            continue
        for v in gdf[col].dropna().head(k):
            s = str(v).strip()
            if s and s not in out:
                out.append(s[:120])
            if len(out) >= k:
                return out
    return out


def _sql_table_exists(conn, full_name: str) -> bool:
    return conn.execute(
        text("SELECT to_regclass(:r) IS NOT NULL").execution_options(no_prepare=True),
        {"r": full_name},
    ).scalar_one()


def _sql_count_intersects(
    engine: Engine,
    schema_table: str,
    parcel_wkt: str,
    geom_col: str = "geom_2154",
) -> int | None:
    with engine.begin() as conn:
        if not _sql_table_exists(conn, schema_table):
            return None
        q = f"""
            SELECT count(*)::int
            FROM {schema_table} t
            WHERE ST_Intersects(t.{geom_col}, ST_GeomFromText(:wkt, 2154))
        """
        return conn.execute(text(q), {"wkt": parcel_wkt}).scalar_one()


def _sql_raster_tiles_count(engine: Engine, schema_table: str, parcel_wkt: str) -> int | None:
    with engine.begin() as conn:
        if not _sql_table_exists(conn, schema_table):
            return None
        q = f"""
            SELECT count(*)::int
            FROM {schema_table} r
            WHERE ST_Intersects(r.rast, ST_GeomFromText(:wkt, 2154))
        """
        return conn.execute(text(q), {"wkt": parcel_wkt}).scalar_one()


def _gdf_intersect_parcel(gdf_raw: gpd.GeoDataFrame, parcel_geom: BaseGeometry) -> gpd.GeoDataFrame:
    if gdf_raw.empty:
        return gdf_raw
    if gdf_raw.crs is None:
        gdf_raw = gdf_raw.set_crs(SRS_WFS, allow_override=True)
    elif gdf_raw.crs.to_string() != SRS_2154:
        gdf_raw = gdf_raw.to_crs(SRS_2154)
    gdf_raw = gdf_raw.rename_geometry("geom_2154")
    gdf_raw["geom_2154"] = gdf_raw["geom_2154"].apply(force_2d)
    return gdf_raw[gdf_raw.geom_2154.intersects(parcel_geom)].copy()


def _wfs_bdtopo_paginate_intersect(
    type_name: str,
    bbox_3857: tuple[float, float, float, float],
    parcel_geom: BaseGeometry,
    *,
    page_limit: int = 5000,
    sleep_s: float = 0.0,
) -> gpd.GeoDataFrame:
    wfs_url = "https://data.geopf.fr/wfs/ows"
    minx, miny, maxx, maxy = bbox_3857
    start_index = 0
    parts: list[gpd.GeoDataFrame] = []

    while True:
        params = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": type_name,
            "srsName": SRS_WFS,
            "bbox": f"{minx},{miny},{maxx},{maxy},{SRS_WFS}",
            "count": page_limit,
            "startIndex": start_index,
        }
        resp = requests.get(wfs_url, params=params, timeout=300)
        if resp.status_code != 200:
            raise RuntimeError(f"WFS HTTP {resp.status_code}: {resp.text[:400]}")
        gdf = gpd.read_file(resp.url)
        if gdf.empty:
            break
        if gdf.crs is None or gdf.crs.to_string() != SRS_WFS:
            gdf = gdf.set_crs(SRS_WFS, allow_override=True)
        clipped = _gdf_intersect_parcel(gdf, parcel_geom)
        if not clipped.empty:
            parts.append(clipped)
        if len(gdf) < page_limit:
            break
        start_index += page_limit
        if sleep_s > 0:
            time.sleep(sleep_s)

    if not parts:
        return gpd.GeoDataFrame(geometry=[], crs=SRS_2154)
    return gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=SRS_2154)


@dataclass
class LayerPreanalyzeRow:
    key: str
    label: str
    status: str  # ok | error | skipped
    intersects: bool | None = None
    n: int | None = None
    geometry_types: list[str] | None = None
    geometry_types_label: str | None = None
    samples: list[str] | None = None
    detail: dict[str, Any] | None = None
    error: str | None = None


def _layers_order_meta() -> list[dict[str, str]]:
    return [{"key": c["key"], "label": c["label"]} for c in LAYER_REGISTRY if c["key"] not in SKIP_KEYS]


def run_preanalyze_parcelle(
    engine: Engine,
    *,
    code_insee: str,
    section: str,
    numero: str,
    buffer_m: float = 50.0,
    on_start: Callable[[dict[str, Any]], None] | None = None,
    on_running: Callable[[str], None] | None = None,
    on_layer: Callable[[LayerPreanalyzeRow], None] | None = None,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    gdf_p = _load_parcelle_wfs(code_insee, section, numero)
    parcel_geom = _parcel_union(gdf_p)
    parcel_wkt = parcel_geom.wkt
    parcel_area_m2 = float(parcel_geom.area)
    geom_metrics = compute_geometry_metrics(parcel_geom)
    bbox_3857 = _bbox_3857_from_buffered_parcel(parcel_geom, buffer_m)

    rows: list[LayerPreanalyzeRow] = []

    if on_start:
        on_start(
            {
                "parcelle": {
                    "code_insee": code_insee,
                    "section": section,
                    "numero": numero,
                    "surface_ha": geom_metrics["surface_ha"],
                    "perimeter_m": geom_metrics["perimeter_m"],
                    "miller": geom_metrics["miller"],
                    "buffer_m": float(buffer_m),
                },
                "bbox_3857": list(bbox_3857),
                "layers_order": _layers_order_meta(),
                "method": "BBOX buffer (EPSG:3857) pour WFS ; intersection stricte avec la parcelle (EPSG:2154). "
                "Couches SQL/geo : ST_Intersects direct sur la parcelle.",
            }
        )

    # Imports paresseux des utilitaires WFS Patrinat / carroyage
    try:
        from layers.carroyage_utils import harvest_adaptive, dedup_on_id_or_geom
    except Exception as e:
        harvest_adaptive = None  # type: ignore
        dedup_on_id_or_geom = None  # type: ignore
        logger.warning("carroyage_utils indisponible: %s", e)

    def _push_row(r: LayerPreanalyzeRow) -> None:
        rows.append(r)
        if on_layer:
            on_layer(r)

    for cfg in LAYER_REGISTRY:
        key = cfg["key"]
        label = cfg["label"]
        if key in SKIP_KEYS:
            continue

        if on_running:
            on_running(key)

        try:
            if key == "geomce":
                from layers.aoi_to_geomce import TABLES_CONFIG

                sub: dict[str, int] = {}
                total = 0
                for tcfg in TABLES_CONFIG:
                    src = tcfg["src"]
                    n = _sql_count_intersects(engine, src, parcel_wkt, "geom_2154")
                    if n is None:
                        sub[tcfg["label"]] = -1
                    else:
                        sub[tcfg["label"]] = n
                        total += n
                _push_row(
                    LayerPreanalyzeRow(
                        key=key,
                        label=label,
                        status="ok",
                        intersects=total > 0,
                        n=total,
                        detail={"par_table_geo": sub},
                    )
                )
                continue

            if key == "zone_de_vegetation":
                zdv = analyze_zdv(engine, parcel_wkt, parcel_area_m2)
                nats = list(zdv.get("natures", []))
                has_ix = bool(zdv.get("intersects")) and len(nats) > 0
                _push_row(
                    LayerPreanalyzeRow(
                        key=key,
                        label=label,
                        status="ok" if "error" not in zdv else "error",
                        intersects=zdv.get("intersects", False),
                        n=len(nats),
                        samples=[],
                        detail={
                            "natures": nats,
                            "total_surface_ha": zdv.get("total_surface_ha", 0),
                            "pct_total": zdv.get("pct_total", 0),
                        }
                        if has_ix
                        else None,
                        error=zdv.get("error"),
                    )
                )
                continue

            if key == "zone_humide":
                n = _sql_count_intersects(engine, "geo.zone_humide", parcel_wkt)
                if n is None:
                    _push_row(LayerPreanalyzeRow(key=key, label=label, status="skipped", error="Table geo.zone_humide absente"))
                else:
                    _push_row(LayerPreanalyzeRow(key=key, label=label, status="ok", intersects=n > 0, n=n))
                continue

            if key == "troncons_hydro":
                hydro_t = analyze_troncons_hydro(engine, parcel_wkt)
                dist_label = None
                if not hydro_t.get("intersects") and hydro_t.get("dist_m") is not None:
                    dist_label = f"Plus proche : {hydro_t['dist_m']:.0f} m"
                _push_row(
                    LayerPreanalyzeRow(
                        key=key,
                        label=label,
                        status="ok" if "error" not in hydro_t else "error",
                        intersects=hydro_t.get("intersects", False),
                        n=hydro_t.get("count", 0),
                        samples=[dist_label] if dist_label else [],
                        detail={"dist_m": hydro_t.get("dist_m"), "source": hydro_t.get("source")},
                        error=hydro_t.get("error"),
                    )
                )
                continue

            if key == "routes":
                gdf = _wfs_bdtopo_paginate_intersect("BDTOPO_V3:troncon_de_route", bbox_3857, parcel_geom, sleep_s=0.25)
                gt, gl = _geom_summary(gdf)
                samp = _sample_strings(gdf, ["nom_voie_ban_gauche", "cleabs", "nature"]) if not gdf.empty else []
                _push_row(
                    LayerPreanalyzeRow(
                        key=key,
                        label=label,
                        status="ok",
                        intersects=len(gdf) > 0,
                        n=len(gdf),
                        geometry_types=gt,
                        geometry_types_label=gl,
                        samples=samp,
                    )
                )
                continue

            if key == "voies_ferrees":
                gdf = _wfs_bdtopo_paginate_intersect("BDTOPO_V3:troncon_de_voie_ferree", bbox_3857, parcel_geom, sleep_s=0.25)
                gt, gl = _geom_summary(gdf)
                samp = _sample_strings(gdf, ["cleabs", "nature"]) if not gdf.empty else []
                _push_row(
                    LayerPreanalyzeRow(
                        key=key,
                        label=label,
                        status="ok",
                        intersects=len(gdf) > 0,
                        n=len(gdf),
                        geometry_types=gt,
                        geometry_types_label=gl,
                        samples=samp,
                    )
                )
                continue

            if key == "surfaces_hydro":
                hydro_s = analyze_surfaces_hydro(engine, parcel_wkt)
                dist_label = None
                if not hydro_s.get("intersects") and hydro_s.get("dist_m") is not None:
                    dist_label = f"Plus proche : {hydro_s['dist_m']:.0f} m"
                _push_row(
                    LayerPreanalyzeRow(
                        key=key,
                        label=label,
                        status="ok" if "error" not in hydro_s else "error",
                        intersects=hydro_s.get("intersects", False),
                        n=hydro_s.get("count", 0),
                        samples=[dist_label] if dist_label else [],
                        detail={"dist_m": hydro_s.get("dist_m"), "source": hydro_s.get("source")},
                        error=hydro_s.get("error"),
                    )
                )
                continue

            if key == "fragmentation":
                nt = _sql_raster_tiles_count(engine, "ecocompensation.fragmentation_raster", parcel_wkt)
                if nt is None:
                    _push_row(
                        LayerPreanalyzeRow(
                            key=key,
                            label=label,
                            status="skipped",
                            error="Raster ecocompensation.fragmentation_raster absent",
                        )
                    )
                else:
                    _push_row(
                        LayerPreanalyzeRow(
                            key=key,
                            label=label,
                            status="ok",
                            intersects=nt > 0,
                            n=nt,
                            detail={"tuiles_raster_intersectant": nt},
                        )
                    )
                continue

            if key == "zones_humides_probables":
                nt = _sql_raster_tiles_count(engine, "geo.zones_humides_probables", parcel_wkt)
                if nt is None:
                    _push_row(
                        LayerPreanalyzeRow(
                            key=key,
                            label=label,
                            status="skipped",
                            error="Table geo.zones_humides_probables absente",
                        )
                    )
                else:
                    _push_row(
                        LayerPreanalyzeRow(
                            key=key,
                            label=label,
                            status="ok",
                            intersects=nt > 0,
                            n=nt,
                            detail={"tuiles_raster_intersectant": nt},
                        )
                    )
                continue

            if key == "arrachage_vignes":
                n = _sql_count_intersects(engine, "geo.arrachage_vignes", parcel_wkt)
                if n is None:
                    _push_row(LayerPreanalyzeRow(key=key, label=label, status="skipped", error="Table geo.arrachage_vignes absente"))
                else:
                    _push_row(LayerPreanalyzeRow(key=key, label=label, status="ok", intersects=n > 0, n=n))
                continue

            if harvest_adaptive is None:
                _push_row(LayerPreanalyzeRow(key=key, label=label, status="error", error="carroyage_utils indisponible"))
                continue

            # ── Couches Patrinat / DU : harvest + intersection parcelle ──
            if key == "znieff":
                from layers.aoi_to_znieff import COUCHES as ZN_COUCHES, _fetch_one_layer as znieff_fetch

                parts: list[gpd.GeoDataFrame] = []
                for zc in ZN_COUCHES:
                    g = znieff_fetch(zc["layer"], zc["znieff_type"], bbox_3857, lambda _m: None)
                    if not g.empty:
                        parts.append(g)
                if not parts:
                    gdf = gpd.GeoDataFrame(geometry=[], crs=SRS_2154)
                else:
                    gdf_w = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=SRS_WFS)
                    gdf = gdf_w.to_crs(SRS_2154).rename_geometry("geom_2154")
                    gdf = gdf[gdf.geom_2154.intersects(parcel_geom)]
                gt, gl = _geom_summary(gdf)
                samp = _sample_strings(gdf, ["nom_site"]) if not gdf.empty else []
                _push_row(
                    LayerPreanalyzeRow(
                        key=key,
                        label=label,
                        status="ok",
                        intersects=len(gdf) > 0,
                        n=len(gdf),
                        geometry_types=gt,
                        geometry_types_label=gl,
                        samples=samp,
                    )
                )
                continue

            if key == "natura2000":
                from layers.aoi_to_natura_2000 import COUCHES as NA_COUCHES, _fetch_layer as natura_fetch

                parts2: list[gpd.GeoDataFrame] = []
                for nc in NA_COUCHES:
                    g = natura_fetch(nc["layer"], nc["natura_categorie"], bbox_3857, lambda _m: None)
                    if not g.empty:
                        parts2.append(g)
                if not parts2:
                    gdf = gpd.GeoDataFrame(geometry=[], crs=SRS_2154)
                else:
                    gdf_w = gpd.GeoDataFrame(pd.concat(parts2, ignore_index=True), crs=SRS_WFS)
                    gdf = gdf_w.to_crs(SRS_2154).rename_geometry("geom_2154")
                    gdf = gdf[gdf.geom_2154.intersects(parcel_geom)]
                gt, gl = _geom_summary(gdf)
                samp = _sample_strings(gdf, ["nom_site", "natura_categorie"]) if not gdf.empty else []
                _push_row(
                    LayerPreanalyzeRow(
                        key=key,
                        label=label,
                        status="ok",
                        intersects=len(gdf) > 0,
                        n=len(gdf),
                        geometry_types=gt,
                        geometry_types_label=gl,
                        samples=samp,
                    )
                )
                continue

            if key == "reserves_naturelles":
                from layers.aoi_to_reserves_naturelles_et_biologiques import (
                    COUCHES as RN_COUCHES,
                    _fetch_one_layer as rn_fetch,
                )

                parts3: list[gpd.GeoDataFrame] = []
                for rc in RN_COUCHES:
                    g = rn_fetch(rc["layer"], rc["categorie_reserve"], bbox_3857, lambda _m: None)
                    if not g.empty:
                        parts3.append(g)
                if not parts3:
                    gdf = gpd.GeoDataFrame(geometry=[], crs=SRS_2154)
                else:
                    gdf_w = gpd.GeoDataFrame(pd.concat(parts3, ignore_index=True), crs=SRS_WFS)
                    gdf = gdf_w.to_crs(SRS_2154).rename_geometry("geom_2154")
                    gdf = gdf[gdf.geom_2154.intersects(parcel_geom)]
                gt, gl = _geom_summary(gdf)
                samp = _sample_strings(gdf, ["nom_site", "categorie_reserve"]) if not gdf.empty else []
                _push_row(
                    LayerPreanalyzeRow(
                        key=key,
                        label=label,
                        status="ok",
                        intersects=len(gdf) > 0,
                        n=len(gdf),
                        geometry_types=gt,
                        geometry_types_label=gl,
                        samples=samp,
                    )
                )
                continue

            if key == "sites_classes":
                from layers.aoi_to_sites_classes import LAYER as SC_LAYER, WFS_URL as SC_URL, _nom_site_col

                gdf, _fmt = harvest_adaptive(SC_URL, SC_LAYER, bbox_3857, srs=SRS_WFS, cap=5000)
                if gdf.empty:
                    gdf = gpd.GeoDataFrame(geometry=[], crs=SRS_2154)
                else:
                    gdf = dedup_on_id_or_geom(gdf) if dedup_on_id_or_geom else gdf
                    gdf = _nom_site_col(gdf)
                    gdf = gdf.to_crs(SRS_2154).rename_geometry("geom_2154")
                    gdf = gdf[gdf.geom_2154.intersects(parcel_geom)]
                gt, gl = _geom_summary(gdf)
                samp = _sample_strings(gdf, ["nom_site"]) if not gdf.empty else []
                _push_row(
                    LayerPreanalyzeRow(
                        key=key,
                        label=label,
                        status="ok",
                        intersects=len(gdf) > 0,
                        n=len(gdf),
                        geometry_types=gt,
                        geometry_types_label=gl,
                        samples=samp,
                    )
                )
                continue

            if key == "ebc":
                from layers.aoi_to_ebc import URL as EBC_URL, LAYER as EBC_LAYER

                gdf, _fmt = harvest_adaptive(EBC_URL, EBC_LAYER, bbox_3857, srs=SRS_WFS, cap=5000)
                if gdf.empty or "libelle" not in gdf.columns:
                    gdf = gpd.GeoDataFrame(geometry=[], crs=SRS_2154)
                else:
                    lib = gdf["libelle"].astype(str).str.lower()
                    mask = lib.str.contains("boise") | lib.str.contains("boisé")
                    gdf = gdf[mask].copy()
                    gdf = gdf.to_crs(SRS_2154).rename_geometry("geom_2154")
                    gdf = gdf[gdf.geom_2154.intersects(parcel_geom)]
                gt, gl = _geom_summary(gdf)
                samp = _sample_strings(gdf, ["libelle", "insee"]) if not gdf.empty else []
                _push_row(
                    LayerPreanalyzeRow(
                        key=key,
                        label=label,
                        status="ok",
                        intersects=len(gdf) > 0,
                        n=len(gdf),
                        geometry_types=gt,
                        geometry_types_label=gl,
                        samples=samp,
                    )
                )
                continue

            _push_row(LayerPreanalyzeRow(key=key, label=label, status="skipped", error="Couche non gérée par la pré-analyse"))

        except Exception as ex:
            logger.exception("Pré-analyse couche %s", key)
            _push_row(LayerPreanalyzeRow(key=key, label=label, status="error", error=str(ex)[:500]))

    elapsed = time.perf_counter() - t0
    return {
        "parcelle": {
            "code_insee": code_insee,
            "section": section,
            "numero": numero,
            "surface_ha": geom_metrics["surface_ha"],
            "perimeter_m": geom_metrics["perimeter_m"],
            "miller": geom_metrics["miller"],
            "buffer_m": float(buffer_m),
        },
        "bbox_3857": list(bbox_3857),
        "method": "BBOX buffer (EPSG:3857) pour WFS ; intersection stricte avec la parcelle (EPSG:2154). "
        "Couches SQL/geo : ST_Intersects direct sur la parcelle.",
        "duration_s": round(elapsed, 2),
        "layers": [asdict(r) for r in rows],
    }
