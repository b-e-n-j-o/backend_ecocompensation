"""
core/subdivision_fiscale/subdivision.py
=======================================
Logique metier de recuperation des subdivisions fiscales IGN
pour l'unite fonciere analysee.

Le module retourne toujours une structure exploitable par le PDF :
- UF subdivisee : details par subdivision + geometries pour la carte
- UF non subdivisee : message explicite + carte de contexte UF seule
"""
from __future__ import annotations

import io
import logging
from typing import Any, Dict, List, Sequence

import geopandas as gpd
import requests

from ..parcelle import ParcelleResult
from ...utils.geo import intersects_gdf

logger = logging.getLogger(__name__)

WFS_ENDPOINT = "https://data.geopf.fr/wfs/ows"
LAYER_SUBDIV = "CADASTRALPARCELS.PARCELLAIRE_EXPRESS:subdivision_fiscale"


def _wfs_get_subdivisions(cql: str, timeout: int = 30) -> gpd.GeoDataFrame:
    params = {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "typeNames": LAYER_SUBDIV,
        "outputFormat": "application/json",
        "SRSNAME": "EPSG:4326",
        "CQL_FILTER": cql,
        "count": "5000",
    }
    try:
        resp = requests.get(WFS_ENDPOINT, params=params, timeout=timeout)
        resp.raise_for_status()
        gdf = gpd.read_file(io.BytesIO(resp.content))
    except Exception as exc:
        logger.warning("Subdivision fiscale WFS en erreur: %s", exc)
        return gpd.GeoDataFrame()

    if gdf.empty or "geometry" not in gdf.columns:
        return gpd.GeoDataFrame()

    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(4326)
    return gdf


def _idu_list_from_parcelles(parcelles: Sequence[ParcelleResult]) -> List[str]:
    idus: List[str] = []
    for p in parcelles:
        if not getattr(p, "ok", False):
            continue
        idu = str(getattr(p, "idu", "") or "").strip()
        if idu and idu not in idus:
            idus.append(idu)
    return idus


def _safe_area_m2(gdf: gpd.GeoDataFrame) -> List[float]:
    if gdf.empty:
        return []
    try:
        areas = gdf.to_crs(2154).geometry.area
        return [float(a) for a in areas]
    except Exception:
        return [0.0 for _ in range(len(gdf))]


def _normalize_subdivision_gdf(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if gdf.empty:
        return gdf
    out = gdf.copy()
    if "lettre" not in out.columns:
        out["lettre"] = "n/a"
    if "idu_parcel" not in out.columns:
        out["idu_parcel"] = ""
    out["lettre"] = out["lettre"].fillna("n/a").astype(str).str.strip()
    out["idu_parcel"] = out["idu_parcel"].fillna("").astype(str).str.strip()
    out["surface_calc_m2"] = _safe_area_m2(out)
    return out


def compute_subdivision_result(
    uf_gdf: gpd.GeoDataFrame,
    parcelles_results: Sequence[ParcelleResult],
) -> Dict[str, Any]:
    """
    Retourne un resultat metier unifie pour la section subdivision fiscale.
    """
    idus = _idu_list_from_parcelles(parcelles_results)
    total_uf_m2 = 0.0
    try:
        total_uf_m2 = float(uf_gdf.to_crs(2154).geometry.area.sum())
    except Exception:
        total_uf_m2 = 0.0

    if not idus:
        return {
            "subdivisee": False,
            "nb_entites": 0,
            "nb_parcelles_avec_subdivision": 0,
            "rows": [],
            "subdivisions_gdf": gpd.GeoDataFrame(),
            "notes": ["Aucun IDU parcellaire disponible pour interroger le flux subdivision."],
            "source": "IGN Parcellaire Express - subdivision_fiscale",
        }

    cql = " OR ".join(f"idu_parcel='{idu}'" for idu in idus)
    raw = _wfs_get_subdivisions(cql)
    if raw.empty:
        return {
            "subdivisee": False,
            "nb_entites": 0,
            "nb_parcelles_avec_subdivision": 0,
            "rows": [],
            "subdivisions_gdf": gpd.GeoDataFrame(),
            "notes": ["Aucune subdivision fiscale detectee sur les parcelles de l'unite fonciere."],
            "source": "IGN Parcellaire Express - subdivision_fiscale",
        }

    inter = intersects_gdf(uf_gdf, raw)
    inter = _normalize_subdivision_gdf(inter)
    if inter.empty:
        return {
            "subdivisee": False,
            "nb_entites": 0,
            "nb_parcelles_avec_subdivision": 0,
            "rows": [],
            "subdivisions_gdf": gpd.GeoDataFrame(),
            "notes": ["Des entites subdivision existent en bbox mais aucune n'intersecte l'unite fonciere."],
            "source": "IGN Parcellaire Express - subdivision_fiscale",
        }

    rows: List[Dict[str, Any]] = []
    seen = set()
    for _, row in inter.iterrows():
        idu = str(row.get("idu_parcel", "") or "").strip()
        lettre = str(row.get("lettre", "n/a") or "n/a").strip() or "n/a"
        surf = float(row.get("surface_calc_m2", 0.0) or 0.0)
        key = (idu, lettre, round(surf, 2))
        if key in seen:
            continue
        seen.add(key)
        pct = (surf / total_uf_m2 * 100.0) if total_uf_m2 > 0 else 0.0
        rows.append(
            {
                "idu_parcel": idu or "n/a",
                "lettre": lettre,
                "surface_calc_m2": surf,
                "pct_uf": pct,
            }
        )

    rows.sort(key=lambda r: (r["idu_parcel"], r["lettre"]))

    nb_entites = len(rows)
    nb_parcelles_avec_subdiv = len({r["idu_parcel"] for r in rows if r["idu_parcel"] != "n/a"})
    # Convention metier : subdivisee seulement si plusieurs entites fiscales sur l'UF.
    subdivisee = nb_entites > 1

    return {
        "subdivisee": subdivisee,
        "nb_entites": nb_entites,
        "nb_parcelles_avec_subdivision": nb_parcelles_avec_subdiv,
        "rows": rows,
        "subdivisions_gdf": inter,
        "notes": [],
        "source": "IGN Parcellaire Express - subdivision_fiscale",
    }
