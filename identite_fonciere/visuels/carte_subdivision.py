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
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
from matplotlib import gridspec
from shapely.ops import unary_union
from .basemap_utils import add_basemap_with_retry

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
    ok = add_basemap_with_retry(ax=ax, crs_str=crs_str, logger=logger)
    if not ok:
        logger.warning("Carte générée sans fond de tuiles (subdivision).")


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
            logger.debug("Parcelle subdivision ignorée (%s)", exc)


def render_subdivision_map(
    uf_gdf: gpd.GeoDataFrame,
    subdivisions_gdf: gpd.GeoDataFrame,
    out_path: str,
    subdivisee: bool,
    parcelle_results: List = None,
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

    _plot_parcelles_overlay(ax_map, parcelle_results or [])
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
