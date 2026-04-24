"""
visuels/carte_prescriptions.py
==============================
Carte unique : prescriptions surfaciques + linéaires + ponctuelles (GPU),
avec fond tuilé (contextily) et contour UF.

Couches : wfs_du:prescription_surf, prescription_lin, prescription_pct.
"""
from __future__ import annotations

import logging
import warnings
from typing import Dict, List

import geopandas as gpd
import matplotlib
import matplotlib.patches as mpatches
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib import gridspec
from matplotlib.colors import to_rgba
from matplotlib.lines import Line2D
from shapely.geometry import box
from shapely.ops import unary_union

from .basemap_utils import add_basemap_with_retry

matplotlib.use("Agg")
warnings.filterwarnings("ignore")

logger = logging.getLogger(__name__)

MAP_SQUARE_SIDE_IN = 6.2
RIGHT_PANEL_RATIO = 0.36

# Couleurs distinctes par famille (orange / ambre — réglement prescription)
COLOR_SURF = "#EA580C"
COLOR_LIN = "#C2410C"
COLOR_PCT = "#D97706"

def _add_basemap(ax, crs_str: str) -> None:
    ok = add_basemap_with_retry(ax=ax, crs_str=crs_str, logger=logger)
    if not ok:
        logger.warning("Carte générée sans fond de tuiles (prescriptions).")


def _plot_parcelles_overlay(ax, parcelle_results: List) -> None:
    """Trace les limites des parcelles composant l'UF + numéros."""
    if not parcelle_results:
        return
    for parc in parcelle_results:
        if not getattr(parc, "ok", False) or getattr(parc, "gdf", None) is None or parc.gdf.empty:
            continue
        try:
            parc_3857 = parc.gdf.to_crs(3857)
            parc_3857.plot(
                ax=ax,
                facecolor="none",
                edgecolor="#FFD600",
                linewidth=1.1,
                zorder=4.5,
            )
            for geom in parc_3857.geometry:
                if geom is None or geom.is_empty:
                    continue
                rp = geom.representative_point()
                raw_num = str(getattr(parc.ref, "numero", "") or "")
                num = raw_num.lstrip("0") or raw_num or "—"
                ax.text(
                    rp.x,
                    rp.y,
                    num,
                    fontsize=7,
                    ha="center",
                    va="center",
                    color="white",
                    fontweight="bold",
                    path_effects=[pe.withStroke(linewidth=1.8, foreground="black")],
                    zorder=6,
                    clip_on=True,
                )
        except Exception as exc:
            logger.debug("Parcelle prescriptions ignorée (%s)", exc)


def _clip_to_buffer(
    gdf: gpd.GeoDataFrame,
    uf_gdf: gpd.GeoDataFrame,
    buffer_m: float,
) -> gpd.GeoDataFrame:
    if gdf.empty:
        return gdf
    uf_3857 = unary_union(uf_gdf.to_crs(3857).geometry)
    clip_box = box(*uf_3857.buffer(buffer_m).bounds)
    clipped = gdf.to_crs(3857).clip(clip_box)
    return clipped[~clipped.geometry.is_empty].copy() if not clipped.empty else clipped


def _plot_geom(ax, sub: gpd.GeoDataFrame, color: str, is_polygon: bool, is_line: bool) -> None:
    if sub.empty:
        return
    for geom_type in sub.geometry.geom_type.unique():
        part = sub[sub.geometry.geom_type == geom_type]
        try:
            if is_polygon and geom_type in ("Polygon", "MultiPolygon"):
                rgba = list(to_rgba(color))
                rgba[3] = 0.38
                part.plot(ax=ax, facecolor=rgba, edgecolor=color, linewidth=1.2, zorder=2)
            elif is_line and geom_type in ("LineString", "MultiLineString"):
                part.plot(ax=ax, color=color, linewidth=2.0, zorder=3, alpha=0.92)
            elif (not is_polygon and not is_line) and geom_type in ("Point", "MultiPoint"):
                part.plot(
                    ax=ax,
                    color=color,
                    markersize=9,
                    zorder=4,
                    alpha=0.95,
                    marker="o",
                    edgecolors="white",
                    linewidths=0.7,
                )
        except Exception as exc:
            logger.debug("plot prescriptions %s : %s", geom_type, exc)


def render_prescriptions_map(
    uf_gdf: gpd.GeoDataFrame,
    pres_gdfs: Dict[str, gpd.GeoDataFrame],
    out_path: str,
    parcelle_results: List = None,
    buffer_m: float = 300.0,
    dpi: int = 150,
) -> str:
    """
    Génère un PNG : fond + surf + lin + points + contour UF + légende.

    Args:
        uf_gdf      : UF EPSG:4326
        pres_gdfs : clés attendues prescription_surf | prescription_lin | prescription_pct
                    (géométries déjà intersectées UF recommandé)
    """
    side = float(MAP_SQUARE_SIDE_IN)
    right = float(RIGHT_PANEL_RATIO)
    fig = plt.figure(figsize=(side * (1 + right), side), facecolor="white")
    gs = gridspec.GridSpec(
        1, 2, figure=fig,
        width_ratios=[1.0, right],
        wspace=0.06,
        left=0.03, right=0.98,
        bottom=0.05, top=0.97,
    )
    ax_map = fig.add_subplot(gs[0, 0])
    ax_leg = fig.add_subplot(gs[0, 1])

    uf_3857 = uf_gdf.to_crs(3857)
    uf_union = unary_union(uf_3857.geometry)
    minx, miny, maxx, maxy = uf_union.bounds
    pad = max(float(buffer_m), 80.0)

    parts: List[gpd.GeoDataFrame] = []
    for table in ("prescription_surf", "prescription_lin", "prescription_pct"):
        gdf = pres_gdfs.get(table)
        if gdf is None or gdf.empty or "geometry" not in gdf.columns:
            continue
        clipped = _clip_to_buffer(gdf, uf_gdf, buffer_m)
        if clipped.empty:
            continue
        c = clipped.copy()
        c["_psc_table"] = table
        parts.append(c)

    if parts:
        merged = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs="EPSG:3857")
    else:
        merged = gpd.GeoDataFrame(columns=["geometry", "_psc_table"], crs="EPSG:3857")

    # Tracé : polygones d'abord, puis lignes, puis points
    surf = merged[merged["_psc_table"] == "prescription_surf"] if not merged.empty else merged
    lin = merged[merged["_psc_table"] == "prescription_lin"] if not merged.empty else merged
    pct = merged[merged["_psc_table"] == "prescription_pct"] if not merged.empty else merged

    _plot_geom(ax_map, surf, COLOR_SURF, is_polygon=True, is_line=False)
    _plot_geom(ax_map, lin, COLOR_LIN, is_polygon=False, is_line=True)
    _plot_geom(ax_map, pct, COLOR_PCT, is_polygon=False, is_line=False)

    _plot_parcelles_overlay(ax_map, parcelle_results or [])
    uf_3857.plot(ax=ax_map, facecolor="none", edgecolor="#FFD600", linewidth=2.5, zorder=5)

    ax_map.set_xlim(minx - pad, maxx + pad)
    ax_map.set_ylim(miny - pad, maxy + pad)
    ax_map.set_aspect("equal", adjustable="box")
    _add_basemap(ax_map, uf_3857.crs.to_string())
    ax_map.set_axis_off()

    # Légende : une entrée par famille présente
    ax_leg.cla()
    ax_leg.axis("off")
    ax_leg.set_xlim(0, 1)
    ax_leg.set_ylim(0, 1)

    handles: List = []
    labels: List[str] = []
    if not surf.empty:
        handles.append(mpatches.Patch(facecolor=COLOR_SURF, edgecolor="white", linewidth=0.6, alpha=0.85))
        labels.append("Prescription surfacique")
    if not lin.empty:
        handles.append(Line2D([0], [0], color=COLOR_LIN, linewidth=2.5))
        labels.append("Prescription linéaire")
    if not pct.empty:
        handles.append(
            Line2D(
                [0], [0], marker="o", color="none",
                markerfacecolor=COLOR_PCT, markeredgecolor="white",
                markeredgewidth=0.7, markersize=9,
            )
        )
        labels.append("Prescription ponctuelle")

    if handles:
        ax_leg.legend(
            handles, labels,
            loc="upper left",
            bbox_to_anchor=(0.02, 0.98),
            fontsize=8,
            framealpha=0.92,
            edgecolor="#cccccc",
            facecolor="#fafafa",
            title="Prescriptions PLU",
            title_fontsize=8.5,
        )
    else:
        ax_leg.text(0.5, 0.5, "Aucune prescription\ndans le périmètre", ha="center", va="center", fontsize=8, color="#666666")

    fig.savefig(out_path, dpi=dpi, bbox_inches="tight", facecolor="white", pad_inches=0.12)
    plt.close(fig)
    logger.info("Carte prescriptions enregistrée : %s", out_path)
    return out_path
