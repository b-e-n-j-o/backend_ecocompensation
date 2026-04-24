"""
visuels/carte_plu.py
Génère la carte satellite + overlay PLU (même logique que plu_visuels.py de Latresne)
mais depuis un GeoDataFrame WFS au lieu de PostGIS.
"""
from __future__ import annotations

import logging
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import geopandas as gpd
import matplotlib
import matplotlib.patches as mpatches
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
from matplotlib import gridspec
from matplotlib.colors import to_rgba
from shapely.ops import unary_union
from .basemap_utils import add_basemap_with_retry
from ..utils.geo import intersects_gdf

matplotlib.use("Agg")
warnings.filterwarnings("ignore")

logger = logging.getLogger(__name__)

# Constantes visuelles (alignées sur plu_visuels.py)
PLU_MAP_SQUARE_SIDE_IN = 6.2
PLU_MAP_RIGHT_PANEL_RATIO = 0.34
MIN_PCT_DISPLAY = 1.0


def _color_from_typezone(typezone: Any) -> str:
    """Couleur CNIG par typezone (N/A/U/AU…) — identique à plu_visuels.py."""
    if not typezone or str(typezone).strip().lower() in ("nan", "none", ""):
        return "#9CA3AF"
    u = str(typezone).strip().upper()
    if u.startswith("N"):
        return "#2D6A4F"
    if u == "A":
        return "#E9C46A"
    if u == "U":
        return "#C1121F"
    if u.startswith("AU"):
        return "#E07A7A"
    if u.startswith("U"):
        return "#D4574A"
    return "#D4A5A5"


def _build_color_map(plu_gdf: gpd.GeoDataFrame) -> Dict[str, str]:
    """Couleur par libelle, basée sur typezone."""
    if plu_gdf.empty:
        return {}
    colors: Dict[str, str] = {}
    tz_col = "typezone" if "typezone" in plu_gdf.columns else None
    lb_col = "libelle" if "libelle" in plu_gdf.columns else None
    if not lb_col:
        return {}
    for _, row in plu_gdf.iterrows():
        lb = str(row.get(lb_col) or "").strip()
        if not lb:
            continue
        tz = str(row.get(tz_col) or "") if tz_col else ""
        colors[lb] = _color_from_typezone(tz)
    return colors


def _add_basemap(ax, crs_str: str) -> None:
    ok = add_basemap_with_retry(ax=ax, crs_str=crs_str, logger=logger)
    if not ok:
        logger.warning("Carte générée sans fond de tuiles (PLU).")


def _plot_parcelles_overlay(ax, parcelle_results: Optional[List[Any]]) -> None:
    """Trace les limites des parcelles composant l'UF + étiquettes de numéro."""
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
            logger.debug("Parcelle PLU ignorée (%s)", exc)


def render_plu_map(
    uf_gdf: gpd.GeoDataFrame,
    plu_gdf: gpd.GeoDataFrame,
    pct_stats: Dict[str, float],
    out_path: str,
    parcelle_results: Optional[List[Any]] = None,
    dpi: int = 150,
) -> str:
    """
    Génère le PNG carte satellite + PLU (carte gauche + légende droite).
    Compatible avec build_plu_zonage_page_flowables() du PDF.
    """
    side = float(PLU_MAP_SQUARE_SIDE_IN)
    right = float(PLU_MAP_RIGHT_PANEL_RATIO)
    fig = plt.figure(figsize=(side * (1 + right), side), facecolor="white")
    gs = gridspec.GridSpec(1, 2, figure=fig,
                           width_ratios=[1.0, right],
                           wspace=0.06, left=0.04, right=0.98,
                           bottom=0.07, top=0.97)
    ax_map = fig.add_subplot(gs[0, 0])
    ax_leg = fig.add_subplot(gs[0, 1])

    parc_3857 = uf_gdf.to_crs(3857)
    parc_union = unary_union(parc_3857.geometry)
    minx, miny, maxx, maxy = parc_union.bounds
    pad = 80.0

    color_map = _build_color_map(plu_gdf)

    # PLU overlay
    if not plu_gdf.empty and "libelle" in plu_gdf.columns:
        plu_3857 = plu_gdf.to_crs(3857)
        for lb in plu_3857["libelle"].unique():
            sub = plu_3857[plu_3857["libelle"] == lb]
            col = color_map.get(str(lb), "#888888")
            rgba = list(to_rgba(col))
            rgba[3] = 0.40
            try:
                sub.plot(ax=ax_map, facecolor=rgba, edgecolor=col,
                         linewidth=1.2, zorder=2)
            except Exception:
                pass
            # Labels
            for _, row in sub.iterrows():
                g = row.geometry
                if g is None or g.is_empty or g.area < 200:
                    continue
                try:
                    cx, cy = g.centroid.x, g.centroid.y
                    ax_map.text(cx, cy, str(lb), fontsize=6.5,
                                ha="center", va="center", color="white",
                                fontweight="bold",
                                path_effects=[pe.withStroke(linewidth=1.5, foreground="black")],
                                zorder=5, clip_on=True)
                except Exception:
                    pass

    # Limites des parcelles composantes + labels
    _plot_parcelles_overlay(ax_map, parcelle_results)

    # Contour UF
    parc_3857.plot(ax=ax_map, facecolor="none", edgecolor="#FFD600",
                   linewidth=2.5, zorder=4)

    ax_map.set_xlim(minx - pad, maxx + pad)
    ax_map.set_ylim(miny - pad, maxy + pad)
    ax_map.set_aspect("equal", adjustable="box")
    _add_basemap(ax_map, parc_3857.crs.to_string())
    ax_map.set_axis_off()

    # Légende
    ax_leg.cla()
    ax_leg.axis("off")
    filtered = {k: v for k, v in pct_stats.items() if v >= MIN_PCT_DISPLAY}
    if filtered:
        handles = [
            mpatches.Patch(facecolor=color_map.get(k, "#888888"), edgecolor="white",
                           linewidth=0.75)
            for k in filtered
        ]
        labels = [f"{k}  {v:.1f} %" for k, v in filtered.items()]
        ax_leg.legend(handles, labels, loc="center", fontsize=9,
                      framealpha=0.9, edgecolor="#cccccc", facecolor="#fafafa",
                      title="Zonage PLU", title_fontsize=9)
    else:
        ax_leg.text(0.5, 0.5, "Aucun zonage\n≥ 1 %",
                    ha="center", va="center", fontsize=9)

    fig.savefig(out_path, dpi=dpi, bbox_inches="tight",
                facecolor="white", pad_inches=0.12)
    plt.close(fig)
    logger.info("✅ Carte PLU enregistrée : %s", out_path)
    return out_path


def render_satellite_map(
    uf_gdf: gpd.GeoDataFrame,
    out_path: str,
    dpi: int = 150,
    buffer_m: float = 100.0,
) -> str:
    """
    Carte satellite simple (sans PLU) — page de garde du rapport.
    """
    fig, ax = plt.subplots(figsize=(6, 4), facecolor="white")
    parc_3857 = uf_gdf.to_crs(3857)
    parc_union = unary_union(parc_3857.geometry)
    minx, miny, maxx, maxy = parc_union.bounds

    parc_3857.plot(ax=ax, facecolor="none", edgecolor="#FFD600",
                   linewidth=2.5, zorder=2)
    ax.set_xlim(minx - buffer_m, maxx + buffer_m)
    ax.set_ylim(miny - buffer_m, maxy + buffer_m)
    ax.set_aspect("equal", adjustable="box")
    _add_basemap(ax, parc_3857.crs.to_string())
    ax.set_axis_off()

    fig.tight_layout(pad=0)
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight",
                facecolor="white", pad_inches=0.05)
    plt.close(fig)
    logger.info("✅ Carte satellite enregistrée : %s", out_path)
    return out_path


def compute_plu_result(
    uf_gdf: gpd.GeoDataFrame,
    plu_gdf: gpd.GeoDataFrame,
    parcelle_results: List[Any],
) -> Dict[str, Any]:
    """
    Construit un contrat PLU enrichi pour la section PDF dédiée :
    - répartition des zonages sur l'UF (% + ha)
    - répartition parcelle par parcelle (% + ha)
    - attributs de zonage (typezone, libelle, libelong)
    """
    empty_result: Dict[str, Any] = {
        "intersecte": False,
        "zonages": [],
        "uf_repartition": [],
        "parcelles_repartition": [],
    }
    if plu_gdf is None or plu_gdf.empty or "geometry" not in plu_gdf.columns:
        return empty_result

    try:
        uf_inter = intersects_gdf(uf_gdf, plu_gdf)
    except Exception as exc:
        logger.warning("PLU result: échec intersection UF/PLU (%s)", exc)
        return empty_result
    if uf_inter.empty:
        return empty_result

    # Normalisation des attributs attendus.
    attrs = uf_inter.copy()
    for col in ("typezone", "libelle", "libelong"):
        if col not in attrs.columns:
            attrs[col] = ""

    attrs_3857 = attrs.to_crs(3857)
    uf_union = unary_union(uf_gdf.to_crs(3857).geometry)
    uf_area_m2 = max(float(uf_union.area), 1.0)

    agg_uf: Dict[tuple, float] = {}
    for _, row in attrs_3857.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        inter = geom.intersection(uf_union)
        area = float(inter.area) if not inter.is_empty else 0.0
        if area <= 0.0:
            continue
        key = (
            str(row.get("typezone") or "—").strip() or "—",
            str(row.get("libelle") or "—").strip() or "—",
            str(row.get("libelong") or "—").strip() or "—",
        )
        agg_uf[key] = agg_uf.get(key, 0.0) + area

    if not agg_uf:
        return empty_result

    uf_repartition: List[Dict[str, Any]] = []
    for (typezone, libelle, libelong), area in agg_uf.items():
        uf_repartition.append(
            {
                "typezone": typezone,
                "libelle": libelle,
                "libelong": libelong,
                "surface_ha": round(area / 10_000.0, 4),
                "pct_uf": round((area / uf_area_m2) * 100.0, 2),
            }
        )
    uf_repartition.sort(key=lambda r: (-r["pct_uf"], r["libelle"], r["typezone"]))

    parcelles_repartition: List[Dict[str, Any]] = []
    for parc in [p for p in parcelle_results if getattr(p, "ok", False) and not p.gdf.empty]:
        parc_3857 = parc.gdf.to_crs(3857)
        parc_union = unary_union(parc_3857.geometry)
        parc_area_m2 = max(float(parc_union.area), 1.0)
        agg_parc: Dict[tuple, float] = {}

        for _, row in attrs_3857.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue
            inter = geom.intersection(parc_union)
            area = float(inter.area) if not inter.is_empty else 0.0
            if area <= 0.0:
                continue
            key = (
                str(row.get("typezone") or "—").strip() or "—",
                str(row.get("libelle") or "—").strip() or "—",
                str(row.get("libelong") or "—").strip() or "—",
            )
            agg_parc[key] = agg_parc.get(key, 0.0) + area

        for (typezone, libelle, libelong), area in agg_parc.items():
            parcelles_repartition.append(
                {
                    "parcelle_ref": parc.ref.label,
                    "idu": parc.idu or "",
                    "typezone": typezone,
                    "libelle": libelle,
                    "libelong": libelong,
                    "surface_ha": round(area / 10_000.0, 4),
                    "pct_parcelle": round((area / parc_area_m2) * 100.0, 2),
                }
            )

    parcelles_repartition.sort(
        key=lambda r: (r["parcelle_ref"], -r["pct_parcelle"], r["libelle"], r["typezone"])
    )

    zonages = [
        {"typezone": r["typezone"], "libelle": r["libelle"], "libelong": r["libelong"]}
        for r in uf_repartition
    ]
    return {
        "intersecte": True,
        "zonages": zonages,
        "uf_repartition": uf_repartition,
        "parcelles_repartition": parcelles_repartition,
    }
