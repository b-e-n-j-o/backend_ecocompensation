"""
visuels/carte_servitudes.py
===========================
Génère la carte satellite + overlay des servitudes d'utilité publique (SUP)
intersectant la parcelle/UF.

Logique :
- Fond satellite EPSG:3857 (Esri WorldImagery, fallback OSM)
- Emprise affichée : bounds de l'UF + buffer configurable (défaut 300 m)
- Géométries affichées : seulement celles qui intersectent réellement l'UF
  (déjà filtrées en amont par intersections.py), clippées au buffer visuel
- Une couleur par suptype (palette fixe, stable entre appels)
- Contour UF en jaune vif (même convention que carte_plu.py)
- Légende à droite : une ligne par suptype avec code + libellé long si disponible
- Gestion des 3 géométries SUP : polygon (assiette_sup_s),
  linestring (assiette_sup_l), point (assiette_sup_p)
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

# Constantes visuelles — alignées sur carte_plu.py
MAP_SQUARE_SIDE_IN = 6.2
RIGHT_PANEL_RATIO = 0.40   # légende un peu plus large que PLU (libellés longs)

# Palette SUP : couleurs distinctes par famille de suptype
# Les codes suptype suivent la nomenclature GPU (ac=monuments, pt=télécoms, i=canalisations…)
# On mappe sur des couleurs par préfixe, avec fallback sur une palette cyclique
SUPTYPE_PALETTE_BY_PREFIX = {
    "ac":  "#1B4F72",   # bleu foncé — monuments historiques, archéologie
    "as":  "#0E6655",   # vert foncé — servitudes assainissement
    "a":   "#148F77",   # vert — autres servitudes agriculture/eau
    "el":  "#F39C12",   # orange — électricité haute tension
    "eg":  "#E67E22",   # orange foncé — gaz
    "i":   "#884EA0",   # violet — canalisations, réseaux
    "pm":  "#C0392B",   # rouge — risques (PPRI, PPRN)
    "pt":  "#2E86C1",   # bleu — télécommunications
    "t":   "#1A5276",   # bleu marine — transport/voirie
    "s":   "#76448A",   # violet foncé — sécurité
}

# Fallback cyclique pour les codes non reconnus
_FALLBACK_COLORS = [
    "#2563EB", "#DC2626", "#059669", "#D97706",
    "#7C3AED", "#DB2777", "#0D9488", "#CA8A04",
]


def _color_for_suptype(suptype: str, fallback_index: int = 0) -> str:
    """Retourne une couleur stable pour un code suptype."""
    if not suptype:
        return _FALLBACK_COLORS[0]
    lower = suptype.lower().strip()
    # Cherche le préfixe le plus long qui matche
    best = None
    for prefix, color in SUPTYPE_PALETTE_BY_PREFIX.items():
        if lower.startswith(prefix):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, color)
    if best:
        return best[1]
    return _FALLBACK_COLORS[fallback_index % len(_FALLBACK_COLORS)]


def _build_sup_color_map(sup_gdf: gpd.GeoDataFrame) -> Dict[str, str]:
    """Construit la map {suptype: couleur} depuis le GeoDataFrame SUP."""
    if sup_gdf.empty or "suptype" not in sup_gdf.columns:
        return {}
    color_map: Dict[str, str] = {}
    fallback_idx = 0
    for st in sup_gdf["suptype"].dropna().unique():
        key = str(st).strip()
        if key not in color_map:
            color_map[key] = _color_for_suptype(key, fallback_idx)
            fallback_idx += 1
    return color_map


def _label_for_suptype(suptype: str, nomsuplitt_map: Dict[str, str]) -> str:
    """Libellé légende : 'ac1 — Eglise Saint-Aubin' si nomsuplitt disponible."""
    nom = nomsuplitt_map.get(str(suptype).strip(), "")
    if nom:
        # Tronque si trop long pour la légende
        if len(nom) > 35:
            nom = nom[:33] + "…"
        return f"{suptype} — {nom}"
    return str(suptype)


def _nomsuplitt_map(sup_gdf: gpd.GeoDataFrame) -> Dict[str, str]:
    """Premier nomsuplitt non vide par suptype."""
    result: Dict[str, str] = {}
    if sup_gdf.empty or "suptype" not in sup_gdf.columns:
        return result
    nom_col = "nomsuplitt" if "nomsuplitt" in sup_gdf.columns else None
    if not nom_col:
        return result
    for _, row in sup_gdf.iterrows():
        st = str(row.get("suptype") or "").strip()
        nom = str(row.get(nom_col) or "").strip()
        if st and nom and st not in result:
            result[st] = nom
    return result


def _add_basemap(ax, crs_str: str) -> None:
    """Fond satellite avec fallback OSM."""
    ok = add_basemap_with_retry(ax=ax, crs_str=crs_str, logger=logger)
    if not ok:
        logger.warning("Carte générée sans fond de tuiles (servitudes).")


def _clip_to_buffer(
    gdf: gpd.GeoDataFrame,
    uf_gdf: gpd.GeoDataFrame,
    buffer_m: float,
) -> gpd.GeoDataFrame:
    """
    Clippe les géométries SUP à l'emprise buffer autour de l'UF (EPSG:3857).
    Évite que des servitudes très étendues (ex: PT ligne HT) ne débordent
    complètement hors du cadre visuel.
    """
    if gdf.empty:
        return gdf
    uf_3857 = unary_union(uf_gdf.to_crs(3857).geometry)
    clip_box = box(*uf_3857.buffer(buffer_m).bounds)
    clipped = gdf.to_crs(3857).clip(clip_box)
    return clipped[~clipped.geometry.is_empty].copy() if not clipped.empty else clipped


def _plot_sup_geom(ax, sub: gpd.GeoDataFrame, color: str) -> None:
    """
    Affiche les géométries SUP selon leur type (polygon/line/point).
    Le GeoDataFrame est déjà en EPSG:3857.
    """
    if sub.empty:
        return
    for geom_type in sub.geometry.geom_type.unique():
        part = sub[sub.geometry.geom_type == geom_type]
        try:
            if geom_type in ("Polygon", "MultiPolygon"):
                rgba = list(to_rgba(color))
                rgba[3] = 0.35
                part.plot(ax=ax, facecolor=rgba, edgecolor=color,
                          linewidth=1.5, zorder=2)
            elif geom_type in ("LineString", "MultiLineString"):
                part.plot(ax=ax, color=color, linewidth=2.2,
                          zorder=2, alpha=0.9)
            elif geom_type in ("Point", "MultiPoint"):
                part.plot(ax=ax, color=color, markersize=10,
                          zorder=3, alpha=0.9, marker="o",
                          edgecolors="white", linewidths=0.8)
        except Exception as e:
            logger.debug("plot error %s : %s", geom_type, e)


def _build_legend(
    ax_leg,
    sup_types_in_map: List[str],
    color_map: Dict[str, str],
    nomsuplitt_map: Dict[str, str],
    geom_type_map: Dict[str, str],
) -> None:
    """
    Construit la légende dans l'axe de droite.
    Une entrée par suptype présent sur la carte.
    """
    ax_leg.cla()
    ax_leg.axis("off")
    ax_leg.set_xlim(0, 1)
    ax_leg.set_ylim(0, 1)

    if not sup_types_in_map:
        ax_leg.text(0.5, 0.5, "Aucune servitude\ndans le périmètre",
                    ha="center", va="center", fontsize=8,
                    color="#666666")
        return

    handles = []
    labels = []
    for st in sup_types_in_map:
        color = color_map.get(st, "#888888")
        gtype = geom_type_map.get(st, "Polygon")
        label = _label_for_suptype(st, nomsuplitt_map)

        if gtype in ("LineString", "MultiLineString"):
            handle = Line2D([0], [0], color=color, linewidth=2.5)
        elif gtype in ("Point", "MultiPoint"):
            handle = Line2D([0], [0], marker="o", color="none",
                            markerfacecolor=color, markeredgecolor="white",
                            markeredgewidth=0.8, markersize=9)
        else:
            handle = mpatches.Patch(facecolor=color, edgecolor="white",
                                    linewidth=0.75, alpha=0.85)
        handles.append(handle)
        labels.append(label)

    ax_leg.legend(
        handles, labels,
        loc="upper left",
        bbox_to_anchor=(0.02, 0.98),
        fontsize=7.5,
        framealpha=0.92,
        edgecolor="#cccccc",
        facecolor="#fafafa",
        title="Servitudes (SUP)",
        title_fontsize=8.5,
        handlelength=1.8,
        handletextpad=0.6,
        labelspacing=0.55,
    )


def render_servitudes_map(
    uf_gdf: gpd.GeoDataFrame,
    sup_gdfs: Dict[str, gpd.GeoDataFrame],
    out_path: str,
    buffer_m: float = 300.0,
    dpi: int = 150,
) -> str:
    """
    Génère le PNG carte servitudes (fond satellite + SUP + contour UF + légende).

    Args:
        uf_gdf      : GeoDataFrame de l'UF en EPSG:4326
        sup_gdfs    : dict {table_name: GeoDataFrame} des couches SUP intersectant l'UF
                      (ex: {"assiette_sup_s": gdf_s, "assiette_sup_l": gdf_l})
                      Chaque GDF doit avoir une colonne "suptype"
        out_path    : chemin du PNG de sortie
        buffer_m    : buffer visuel autour de l'UF en mètres
        dpi         : résolution

    Returns:
        Chemin absolu du PNG généré.
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

    # ── Emprise de la carte ────────────────────────────────────────────────
    uf_3857 = uf_gdf.to_crs(3857)
    uf_union = unary_union(uf_3857.geometry)
    minx, miny, maxx, maxy = uf_union.bounds
    pad = max(buffer_m, 80.0)

    # ── Fusion et couleurs de toutes les couches SUP ───────────────────────
    # On fusionne tous les GDFs SUP en un seul pour construire la color map
    all_parts: List[gpd.GeoDataFrame] = []
    for table_name, gdf in sup_gdfs.items():
        if gdf is None or gdf.empty:
            continue
        clipped = _clip_to_buffer(gdf, uf_gdf, buffer_m)
        if clipped.empty:
            continue
        # S'assurer que suptype est présent
        if "suptype" not in clipped.columns:
            clipped = clipped.copy()
            clipped["suptype"] = table_name
        clipped["_table"] = table_name
        all_parts.append(clipped)

    if all_parts:
        sup_all = gpd.GeoDataFrame(
            pd.concat(all_parts, ignore_index=True),
            crs="EPSG:3857",
        )
    else:
        sup_all = gpd.GeoDataFrame(columns=["suptype", "geometry"], crs="EPSG:3857")

    color_map = _build_sup_color_map(sup_all.to_crs(4326) if not sup_all.empty else sup_all)
    nomsuplitt_map = _nomsuplitt_map(sup_all.to_crs(4326) if not sup_all.empty else sup_all)

    # ── Tracé des géométries SUP ───────────────────────────────────────────
    sup_types_in_map: List[str] = []
    geom_type_map: Dict[str, str] = {}

    if not sup_all.empty and "suptype" in sup_all.columns:
        for st in sup_all["suptype"].dropna().unique():
            key = str(st).strip()
            sub = sup_all[sup_all["suptype"] == key]
            if sub.empty:
                continue
            color = color_map.get(key, "#888888")
            _plot_sup_geom(ax_map, sub, color)
            # Type de géométrie majoritaire pour la légende
            dominant = sub.geometry.geom_type.value_counts().index[0]
            geom_type_map[key] = dominant
            sup_types_in_map.append(key)

    # ── Contour UF (jaune, même convention que PLU) ────────────────────────
    uf_3857.plot(ax=ax_map, facecolor="none", edgecolor="#FFD600",
                 linewidth=2.5, zorder=4)

    # ── Emprise + basemap ──────────────────────────────────────────────────
    ax_map.set_xlim(minx - pad, maxx + pad)
    ax_map.set_ylim(miny - pad, maxy + pad)
    ax_map.set_aspect("equal", adjustable="box")
    _add_basemap(ax_map, uf_3857.crs.to_string())
    ax_map.set_axis_off()

    # ── Légende ───────────────────────────────────────────────────────────
    _build_legend(ax_leg, sup_types_in_map, color_map, nomsuplitt_map, geom_type_map)

    fig.savefig(out_path, dpi=dpi, bbox_inches="tight",
                facecolor="white", pad_inches=0.12)
    plt.close(fig)
    logger.info("✅ Carte servitudes enregistrée : %s", out_path)
    return out_path


def build_servitudes_map_from_intersections(
    uf_gdf: gpd.GeoDataFrame,
    layer_results: List[Any],
    out_path: str,
    buffer_m: float = 300.0,
    dpi: int = 150,
) -> Optional[str]:
    """
    Construit la carte servitudes depuis la liste de LayerResult déjà filtrés.
    Entrée : résultats de fetch_all_layers() + intersect_with_parcelle() déjà appliqué.
    Retourne le chemin PNG ou None si aucune servitude.

    `layer_results` peut être la liste brute de LayerResult — on filtre
    ici sur article == "4" et status == "ok".
    """
    sup_gdfs: Dict[str, gpd.GeoDataFrame] = {}

    for lr in layer_results:
        # On accepte aussi des dicts (si appelé depuis test_pipeline)
        if hasattr(lr, "article"):
            if lr.article != "4" or not lr.ok:
                continue
            sup_gdfs[lr.table] = lr.gdf
        elif isinstance(lr, dict):
            if lr.get("article") != "4":
                continue
            gdf = lr.get("gdf")
            if gdf is not None and not gdf.empty:
                sup_gdfs[lr.get("table", "sup")] = gdf

    if not sup_gdfs:
        logger.info("Aucune couche SUP intersectant l'UF — carte servitudes non générée")
        return None

    return render_servitudes_map(
        uf_gdf=uf_gdf,
        sup_gdfs=sup_gdfs,
        out_path=out_path,
        buffer_m=buffer_m,
        dpi=dpi,
    )