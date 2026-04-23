"""
visuels/carte_intro.py
======================
Carte de page de garde :
- fond cartographique tuilé,
- contour de l'unité foncière,
- limites des parcelles composant l'UF,
- numéro cadastral centré sur chaque parcelle.
"""
from __future__ import annotations

import logging
import warnings
from typing import Any, List

import geopandas as gpd
import matplotlib
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
from shapely.ops import unary_union

from .basemap_utils import add_basemap_with_retry

matplotlib.use("Agg")
warnings.filterwarnings("ignore")

logger = logging.getLogger(__name__)


def _add_basemap(ax, crs_str: str) -> None:
    ok = add_basemap_with_retry(ax=ax, crs_str=crs_str, logger=logger)
    if not ok:
        logger.warning("Carte générée sans fond de tuiles (intro).")


def render_intro_map(
    uf_gdf: gpd.GeoDataFrame,
    parcelle_results: List[Any],
    out_path: str,
    dpi: int = 150,
    buffer_m: float = 120.0,
) -> str:
    """
    Rend une carte de synthèse pour la page de garde.

    Args:
        uf_gdf: GeoDataFrame de l'unité foncière.
        parcelle_results: résultats `fetch_parcelles` (objets avec `ok`, `gdf`, `ref`).
        out_path: chemin du PNG de sortie.
    """
    fig, ax = plt.subplots(figsize=(6.7, 4.6), facecolor="white")
    uf_3857 = uf_gdf.to_crs(3857)
    uf_union = unary_union(uf_3857.geometry)
    minx, miny, maxx, maxy = uf_union.bounds
    pad = max(float(buffer_m), 80.0)

    # Parcelles composant l'UF (découpage interne + labels de numéro).
    for parc in parcelle_results:
        if not getattr(parc, "ok", False) or getattr(parc, "gdf", None) is None or parc.gdf.empty:
            continue
        try:
            parc_3857 = parc.gdf.to_crs(3857)
            parc_3857.plot(
                ax=ax,
                facecolor="none",
                edgecolor="#0F172A",
                linewidth=1.25,
                zorder=4,
            )
            for geom in parc_3857.geometry:
                if geom is None or geom.is_empty:
                    continue
                rp = geom.representative_point()
                num = str(getattr(parc.ref, "numero", "") or "").lstrip("0") or str(getattr(parc.ref, "numero", "") or "—")
                ax.text(
                    rp.x,
                    rp.y,
                    num,
                    fontsize=8,
                    ha="center",
                    va="center",
                    color="white",
                    fontweight="bold",
                    path_effects=[pe.withStroke(linewidth=1.8, foreground="black")],
                    zorder=6,
                    clip_on=True,
                )
        except Exception as exc:
            logger.debug("Parcelle intro ignorée (%s)", exc)

    # Contour UF par-dessus pour matérialiser clairement l'emprise.
    uf_3857.plot(
        ax=ax,
        facecolor="none",
        edgecolor="#FFD600",
        linewidth=2.8,
        zorder=5,
    )

    ax.set_xlim(minx - pad, maxx + pad)
    ax.set_ylim(miny - pad, maxy + pad)
    ax.set_aspect("equal", adjustable="box")
    _add_basemap(ax, uf_3857.crs.to_string())
    ax.set_axis_off()

    fig.tight_layout(pad=0.02)
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight", facecolor="white", pad_inches=0.05)
    plt.close(fig)
    logger.info("Carte intro enregistrée : %s", out_path)
    return out_path
