"""
visuels/carte_subdivision.py
============================
Carte satellite + overlay des geometries de subdivision fiscale.
"""
from __future__ import annotations

import logging
import warnings
from typing import Dict, List

import geopandas as gpd
import matplotlib
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
from matplotlib import gridspec
from shapely.ops import unary_union

matplotlib.use("Agg")
warnings.filterwarnings("ignore")

logger = logging.getLogger(__name__)

MAP_SQUARE_SIDE_IN = 6.2
RIGHT_PANEL_RATIO = 0.34

_COLORS = [
    "#2563EB",
    "#DC2626",
    "#059669",
    "#D97706",
    "#7C3AED",
    "#DB2777",
]


def _add_basemap(ax, crs_str: str) -> None:
    try:
        import contextily as ctx

        ctx.add_basemap(
            ax,
            crs=crs_str,
            source=ctx.providers.Esri.WorldImagery,
            zoom="auto",
            attribution=False,
            zorder=0,
        )
        return
    except Exception:
        pass
    try:
        import contextily as ctx

        ctx.add_basemap(
            ax,
            crs=crs_str,
            source=ctx.providers.OpenStreetMap.Mapnik,
            zoom="auto",
            attribution=False,
            zorder=0,
        )
    except Exception:
        pass


def render_subdivision_map(
    uf_gdf: gpd.GeoDataFrame,
    subdivisions_gdf: gpd.GeoDataFrame,
    out_path: str,
    subdivisee: bool,
    buffer_m: float = 300.0,
    dpi: int = 150,
) -> str:
    side = float(MAP_SQUARE_SIDE_IN)
    right = float(RIGHT_PANEL_RATIO)
    fig = plt.figure(figsize=(side * (1 + right), side), facecolor="white")
    gs = gridspec.GridSpec(
        1,
        2,
        figure=fig,
        width_ratios=[1.0, right],
        wspace=0.06,
        left=0.04,
        right=0.98,
        bottom=0.05,
        top=0.97,
    )
    ax_map = fig.add_subplot(gs[0, 0])
    ax_leg = fig.add_subplot(gs[0, 1])

    uf_3857 = uf_gdf.to_crs(3857)
    uf_union = unary_union(uf_3857.geometry)
    minx, miny, maxx, maxy = uf_union.bounds
    pad = max(float(buffer_m), 80.0)

    legend_handles: List[mpatches.Patch] = []
    legend_labels: List[str] = []

    if subdivisee and subdivisions_gdf is not None and not subdivisions_gdf.empty:
        sub_3857 = subdivisions_gdf.to_crs(3857).copy()
        if "lettre" not in sub_3857.columns:
            sub_3857["lettre"] = "n/a"
        for idx, lettre in enumerate(sorted(sub_3857["lettre"].fillna("n/a").astype(str).unique())):
            color = _COLORS[idx % len(_COLORS)]
            one = sub_3857[sub_3857["lettre"].fillna("n/a").astype(str) == lettre]
            if one.empty:
                continue
            one.plot(ax=ax_map, facecolor=color, edgecolor="#111827", linewidth=1.2, alpha=0.45, zorder=2)
            legend_handles.append(
                mpatches.Patch(facecolor=color, edgecolor="#111827", alpha=0.75)
            )
            legend_labels.append(f"Subdivision {lettre}")

    uf_3857.plot(ax=ax_map, facecolor="none", edgecolor="#FFD600", linewidth=2.5, zorder=4)
    ax_map.set_xlim(minx - pad, maxx + pad)
    ax_map.set_ylim(miny - pad, maxy + pad)
    ax_map.set_aspect("equal", adjustable="box")
    _add_basemap(ax_map, uf_3857.crs.to_string())
    ax_map.set_axis_off()

    ax_leg.cla()
    ax_leg.axis("off")
    ax_leg.set_xlim(0, 1)
    ax_leg.set_ylim(0, 1)

    uf_handle = mpatches.Patch(facecolor="none", edgecolor="#FFD600", linewidth=2.0)
    if legend_handles:
        ax_leg.legend(
            legend_handles + [uf_handle],
            legend_labels + ["Unite fonciere"],
            loc="center",
            fontsize=8.5,
            framealpha=0.92,
            edgecolor="#cccccc",
            facecolor="#fafafa",
            title="Subdivision fiscale",
            title_fontsize=9,
        )
    else:
        ax_leg.legend(
            [uf_handle],
            ["Unite fonciere"],
            loc="center",
            fontsize=8.5,
            framealpha=0.92,
            edgecolor="#cccccc",
            facecolor="#fafafa",
            title="Subdivision fiscale",
            title_fontsize=9,
        )
        ax_leg.text(
            0.5,
            0.34,
            "Aucune subdivision\nsur l'unite fonciere",
            ha="center",
            va="center",
            fontsize=8.5,
            color="#666666",
            style="italic",
        )

    fig.savefig(out_path, dpi=dpi, bbox_inches="tight", facecolor="white", pad_inches=0.12)
    plt.close(fig)
    logger.info("Carte subdivision enregistree: %s", out_path)
    return out_path
