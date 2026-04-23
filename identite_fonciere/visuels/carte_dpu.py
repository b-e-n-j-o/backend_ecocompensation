"""
visuels/carte_dpu.py
====================
Carte satellite + overlay Droit de Préemption Urbain (DPU).

Source : couche WFS `wfs_du:info_surf`, filtrée sur `typeinf = '04'`.
Cette couche contient aussi d'autres informations réglementaires (typeinf != 04)
qui sont gérées séparément dans la section "Informations réglementaires".

Logique :
- On re-fetch info_surf avec CQL_FILTER typeinf='04' + bbox buffer 300m
- On intersecte Shapely avec l'UF
- Si intersection → carte satellite + zone violette + contour UF jaune + légende
- Si pas d'intersection → on génère quand même un PNG "Non soumis" pour
  que le PDF puisse afficher la section avec le message approprié

La fonction principale retourne toujours un résultat, même quand l'UF n'est
pas soumise — ce qui permet d'avoir une section DPU systématique dans le rapport.
"""
from __future__ import annotations

import io
import logging
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import geopandas as gpd
import matplotlib
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import requests
from matplotlib import gridspec
from matplotlib.colors import to_rgba
from shapely.ops import unary_union

matplotlib.use("Agg")
warnings.filterwarnings("ignore")

logger = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────────────
WFS_ENDPOINT    = "https://data.geopf.fr/wfs/ows"
DPU_TYPENAME    = "wfs_du:info_surf"
DPU_TYPEINF     = "04"
DPU_COLOR       = "#6D28D9"          # violet — convention préemption
DPU_BUFFER_M    = 300.0              # buffer visuel autour de l'UF

MAP_SQUARE_SIDE_IN  = 6.2
RIGHT_PANEL_RATIO   = 0.34


def _add_basemap(ax, crs_str: str) -> None:
    try:
        import contextily as ctx
        ctx.add_basemap(ax, crs=crs_str, source=ctx.providers.Esri.WorldImagery,
                        zoom="auto", attribution=False, zorder=0)
        return
    except Exception:
        pass
    try:
        import contextily as ctx
        ctx.add_basemap(ax, crs=crs_str, source=ctx.providers.OpenStreetMap.Mapnik,
                        zoom="auto", attribution=False, zorder=0)
    except Exception:
        pass


def fetch_dpu_in_bbox(
    bbox: Tuple[float, float, float, float],
    timeout: int = 30,
) -> gpd.GeoDataFrame:
    """
    Récupère les zones DPU (info_surf, typeinf=04) dans la bbox via WFS.
    Utilise CQL_FILTER pour ne ramener que les entités DPU — plus propre
    que de filtrer côté Python sur un fetch bbox générique.
    Retourne un GeoDataFrame EPSG:4326 (vide si aucun résultat).
    """
    minx, miny, maxx, maxy = bbox
    params = {
        "service":      "WFS",
        "version":      "2.0.0",
        "request":      "GetFeature",
        "typeNames":    DPU_TYPENAME,
        "srsName":      "EPSG:4326",
        "outputFormat": "application/json",
        "bbox":         f"{minx},{miny},{maxx},{maxy},EPSG:4326",
        "CQL_FILTER":   f"typeinf='{DPU_TYPEINF}'",
        "count":        "500",
    }
    try:
        r = requests.get(WFS_ENDPOINT, params=params, timeout=timeout)
        r.raise_for_status()
    except Exception as e:
        logger.warning("DPU WFS fetch error : %s", e)
        return gpd.GeoDataFrame()

    try:
        gdf = gpd.read_file(io.BytesIO(r.content))
    except Exception as e:
        logger.warning("DPU GeoJSON parse error : %s", e)
        return gpd.GeoDataFrame()

    if gdf.empty or "geometry" not in gdf.columns:
        return gpd.GeoDataFrame()

    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs("EPSG:4326")

    logger.info("   DPU : %d entité(s) dans la bbox", len(gdf))
    return gdf


def compute_dpu_result_from_intersections(
    intersections: List[Dict[str, Any]],
    uf_gdf: gpd.GeoDataFrame,
) -> Dict[str, Any]:
    """
    Dérive le résultat DPU depuis les intersections déjà calculées par le pipeline
    principal (info_surf). Évite un second appel WFS avec CQL_FILTER qui provoque
    un HTTP 500 sur le serveur GPU (bbox + CQL_FILTER simultanés non supportés).

    Filtre les éléments de info_surf dont typeinf == '04'.
    """
    dpu_elements: List[Dict[str, Any]] = []
    dpu_gdf = gpd.GeoDataFrame()

    for layer in intersections:
        if layer.get("table") != "info_surf":
            continue
        for el in layer.get("elements") or []:
            ti = str(el.get("typeinf") or "").strip()
            # Accepte '04', '04 — Droit...' (déjà enrichi), ou libellé contenant DPU
            if ti.startswith("04") or "préemption" in el.get("libelle", "").lower():
                dpu_elements.append(el)

    if not dpu_elements:
        logger.info("   DPU : non soumise (typeinf=04 absent des intersections info_surf)")
        return {"intersecte": False, "dpu_gdf": dpu_gdf, "libelles": [], "nb_entites": 0}

    libelles: List[str] = []
    for el in dpu_elements:
        lb = str(el.get("libelle") or "").strip()
        if lb and lb not in libelles:
            libelles.append(lb)

    # Récupère la géométrie DPU depuis le GDF info_surf intersecté
    # en re-fetchant info_surf (sans CQL_FILTER cette fois — bbox seul)
    from ..utils.geo import gdf_bbox_4326, intersects_gdf

    try:
        bbox    = gdf_bbox_4326(uf_gdf, buffer_m=DPU_BUFFER_M)
        raw_gdf = fetch_dpu_in_bbox_no_cql(bbox)
        if not raw_gdf.empty and "typeinf" in raw_gdf.columns:
            dpu_gdf = raw_gdf[raw_gdf["typeinf"].astype(str).str.strip() == "04"].copy()
            dpu_gdf = intersects_gdf(uf_gdf, dpu_gdf)
    except Exception as e:
        logger.warning("   DPU : géométrie non récupérée pour la carte (%s), carte simplifiée", e)
        dpu_gdf = gpd.GeoDataFrame()

    nb = len(dpu_gdf) if not dpu_gdf.empty else len(dpu_elements)
    logger.info("   DPU : ✅ soumise — %d entité(s), libellés: %s", nb, libelles)
    return {
        "intersecte": True,
        "dpu_gdf":    dpu_gdf,
        "libelles":   libelles,
        "nb_entites": nb,
    }


def fetch_dpu_in_bbox_no_cql(
    bbox: Tuple[float, float, float, float],
    timeout: int = 30,
) -> gpd.GeoDataFrame:
    """
    Récupère info_surf SANS CQL_FILTER (bbox seul) — le serveur GPU refuse
    la combinaison bbox+CQL_FILTER avec HTTP 500. Le filtrage typeinf=04
    est fait côté Python après réception.
    """
    minx, miny, maxx, maxy = bbox
    params = {
        "service":      "WFS",
        "version":      "2.0.0",
        "request":      "GetFeature",
        "typeNames":    DPU_TYPENAME,
        "srsName":      "EPSG:4326",
        "outputFormat": "application/json",
        "bbox":         f"{minx},{miny},{maxx},{maxy},EPSG:4326",
        "count":        "500",
    }
    try:
        r = requests.get(WFS_ENDPOINT, params=params, timeout=timeout)
        r.raise_for_status()
        gdf = gpd.read_file(io.BytesIO(r.content))
        if gdf.empty or "geometry" not in gdf.columns:
            return gpd.GeoDataFrame()
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")
        elif gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs("EPSG:4326")
        return gdf
    except Exception as e:
        logger.warning("DPU info_surf bbox fetch error : %s", e)
        return gpd.GeoDataFrame()


def compute_dpu_result(
    uf_gdf: gpd.GeoDataFrame,
    buffer_m: float = DPU_BUFFER_M,
    intersections: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Point d'entrée principal. Si `intersections` est fourni (cas normal du
    pipeline batch), utilise compute_dpu_result_from_intersections pour éviter
    le second appel WFS défaillant. Sinon, tente un fetch direct (fallback).
    """
    if intersections is not None:
        return compute_dpu_result_from_intersections(intersections, uf_gdf)

    # Fallback : fetch direct sans CQL_FILTER + filtre Python
    from ..utils.geo import gdf_bbox_4326, intersects_gdf
    bbox    = gdf_bbox_4326(uf_gdf, buffer_m=buffer_m)
    raw_gdf = fetch_dpu_in_bbox_no_cql(bbox)
    if raw_gdf.empty:
        return {"intersecte": False, "dpu_gdf": raw_gdf, "libelles": [], "nb_entites": 0}
    if "typeinf" in raw_gdf.columns:
        raw_gdf = raw_gdf[raw_gdf["typeinf"].astype(str).str.strip() == "04"]
    dpu_inter = intersects_gdf(uf_gdf, raw_gdf)
    if dpu_inter.empty:
        return {"intersecte": False, "dpu_gdf": dpu_inter, "libelles": [], "nb_entites": 0}
    libelles = [
        str(v).strip() for v in dpu_inter.get("libelle", [])
        if v and str(v).strip()
    ] if "libelle" in dpu_inter.columns else []
    return {
        "intersecte": True,
        "dpu_gdf":    dpu_inter,
        "libelles":   list(dict.fromkeys(libelles)),
        "nb_entites": len(dpu_inter),
    }


def render_dpu_map(
    uf_gdf: gpd.GeoDataFrame,
    dpu_gdf: gpd.GeoDataFrame,
    out_path: str,
    intersecte: bool,
    buffer_m: float = DPU_BUFFER_M,
    dpi: int = 150,
) -> str:
    """
    Génère le PNG de la carte DPU :
    - Si intersecte=True  : fond satellite + zone DPU violette + contour UF jaune
    - Si intersecte=False : fond satellite + contour UF jaune uniquement
    """
    side  = float(MAP_SQUARE_SIDE_IN)
    right = float(RIGHT_PANEL_RATIO)
    fig   = plt.figure(figsize=(side * (1 + right), side), facecolor="white")
    gs    = gridspec.GridSpec(
        1, 2, figure=fig,
        width_ratios=[1.0, right],
        wspace=0.06, left=0.04, right=0.98, bottom=0.05, top=0.97,
    )
    ax_map = fig.add_subplot(gs[0, 0])
    ax_leg = fig.add_subplot(gs[0, 1])

    uf_3857   = uf_gdf.to_crs(3857)
    uf_union  = unary_union(uf_3857.geometry)
    minx, miny, maxx, maxy = uf_union.bounds
    pad = max(buffer_m, 80.0)

    # Zone DPU (si intersection)
    if intersecte and not dpu_gdf.empty:
        dpu_3857 = dpu_gdf.to_crs(3857)
        rgba = list(to_rgba(DPU_COLOR))
        rgba[3] = 0.38
        try:
            dpu_3857.plot(ax=ax_map, facecolor=rgba, edgecolor=DPU_COLOR,
                          linewidth=1.5, zorder=2)
        except Exception:
            pass

    # Contour UF
    uf_3857.plot(ax=ax_map, facecolor="none", edgecolor="#FFD600",
                 linewidth=2.5, zorder=4)

    ax_map.set_xlim(minx - pad, maxx + pad)
    ax_map.set_ylim(miny - pad, maxy + pad)
    ax_map.set_aspect("equal", adjustable="box")
    _add_basemap(ax_map, uf_3857.crs.to_string())
    ax_map.set_axis_off()

    # Légende
    ax_leg.cla()
    ax_leg.axis("off")
    ax_leg.set_xlim(0, 1)
    ax_leg.set_ylim(0, 1)

    if intersecte:
        handles = [
            mpatches.Patch(facecolor=DPU_COLOR, edgecolor="white",
                           linewidth=0.75, alpha=0.85),
            mpatches.Patch(facecolor="none", edgecolor="#FFD600",
                           linewidth=2.0),
        ]
        labels = ["Zone DPU", "Unité foncière"]
        ax_leg.legend(handles, labels, loc="center", fontsize=9,
                      framealpha=0.92, edgecolor="#cccccc", facecolor="#fafafa",
                      title="Droit de préemption", title_fontsize=9)
    else:
        handles = [mpatches.Patch(facecolor="none", edgecolor="#FFD600", linewidth=2.0)]
        ax_leg.legend(handles, ["Unité foncière"], loc="center", fontsize=9,
                      framealpha=0.92, edgecolor="#cccccc", facecolor="#fafafa",
                      title="Droit de préemption", title_fontsize=9)
        ax_leg.text(0.5, 0.35, "Non soumise\nau DPU", ha="center", va="center",
                    fontsize=9, color="#666666", style="italic")

    fig.savefig(out_path, dpi=dpi, bbox_inches="tight",
                facecolor="white", pad_inches=0.12)
    plt.close(fig)
    logger.info("✅ Carte DPU enregistrée : %s", out_path)
    return out_path