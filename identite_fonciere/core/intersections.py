"""
core/intersections.py
Orchestre : UF GeoDataFrame → appels GPU WFS → filtrage Shapely → liste d'intersections.
Produit la même structure de données que analyser_identite_fonciere() de Latresne,
utilisable directement par pdf/rapport.py.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import geopandas as gpd
from shapely.ops import unary_union

from .gpu_wfs import GPU_LAYERS_BY_TABLE, LayerResult, fetch_all_layers
from ..utils.geo import area_intersection_pct, gdf_bbox_4326, intersects_gdf

logger = logging.getLogger(__name__)

# Buffer autour de l'UF pour la requête WFS (en mètres)
WFS_BUFFER_M = 300.0

# Seuil minimal pour un zonage PLU (% surface UF) — comme dans plu_visuels.py
PLU_MIN_PCT = 1.0


def _elements_from_gdf(
    gdf: gpd.GeoDataFrame,
    keep_cols: List[str],
    group_by: Optional[str],
) -> List[Dict[str, Any]]:
    """
    Construit la liste d'éléments (dicts d'attributs) depuis un GeoDataFrame filtré.
    Déduplique selon group_by si défini, sinon par signature complète.
    """
    avail = [c for c in keep_cols if c in gdf.columns]
    if not avail:
        # Pas d'attributs connus mais intersection détectée
        n = len(gdf)
        if n == 1:
            return [{"intersection": "Oui"}]
        return [{"intersection": "Oui", "entités": str(n)}]

    elements: List[Dict[str, Any]] = []
    seen: set = set()

    for _, row in gdf.iterrows():
        obj: Dict[str, Any] = {}
        for col in avail:
            val = row.get(col)
            if val is None:
                continue
            if isinstance(val, float) and str(val) in ("nan", "inf", "-inf"):
                continue
            s = str(val).strip()
            if not s:  # libelong="" sur GPU → on skip
                continue
            obj[col] = s

        if not obj:
            continue

        # Clé de dédup : group_by seul si défini, sinon signature complète
        if group_by and group_by in obj:
            key = obj[group_by]
        else:
            key = json.dumps(obj, sort_keys=True, ensure_ascii=False)

        if key in seen:
            continue
        seen.add(key)
        elements.append(obj)

    return elements


def _layer_to_intersection(
    result: LayerResult,
    uf_gdf: gpd.GeoDataFrame,
) -> Optional[Dict[str, Any]]:
    """
    Filtre une LayerResult par intersection Shapely avec l'UF.
    Retourne un dict d'intersection (même format que Latresne) ou None.
    """
    cfg = GPU_LAYERS_BY_TABLE.get(result.table, {})
    keep = cfg.get("keep", [])
    group_by = cfg.get("group_by")
    attr_disc = cfg.get("attribut_discriminant")

    if not result.ok:
        return None

    # Filtrage géométrique Shapely
    intersected = intersects_gdf(uf_gdf, result.gdf)
    if intersected.empty:
        logger.info("   ⬜ %s : 0 intersection réelle (bbox avait %d entités)",
                    result.table, len(result.gdf))
        return None

    logger.info("   ✅ %s : %d entité(s) intersectent l'UF", result.table, len(intersected))

    # Calcul % de surface pour le PLU
    pct_stats: Dict[str, float] = {}
    if result.table == "zone_urba" and group_by and group_by in intersected.columns:
        try:
            pct_stats = area_intersection_pct(uf_gdf, intersected, group_by)
        except Exception as e:
            logger.warning("⚠️  Calcul % PLU : %s", e)

    # Construction des éléments
    elements = _elements_from_gdf(intersected, keep, group_by)
    if not elements:
        return None

    # Filtre PLU : on retire les zonages < PLU_MIN_PCT
    if result.table == "zone_urba" and pct_stats and group_by:
        allowed = {z for z, p in pct_stats.items() if p >= PLU_MIN_PCT}
        filtered = [
            el for el in elements
            if el.get(group_by, "") in allowed
        ]
        if not filtered:
            # Tous sous le seuil
            return {
                "table": result.table,
                "display_name": result.display_name,
                "article": result.article,
                "type": result.layer_type,
                "attribut_discriminant": attr_disc,
                "elements": [],
                "pct_stats": pct_stats,
                "_plu_all_zonages_below_min_pct": True,
            }
        elements = filtered

    return {
        "table": result.table,
        "display_name": result.display_name,
        "article": result.article,
        "type": result.layer_type,
        "attribut_discriminant": attr_disc,
        "elements": elements,
        "pct_stats": pct_stats,
    }


def compute_intersections(
    uf_gdf: gpd.GeoDataFrame,
    buffer_m: float = WFS_BUFFER_M,
    layers: Optional[List[str]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, float]]:
    """
    Pipeline complet :
    1. Calcul bbox + buffer autour de l'UF
    2. Requêtes GPU WFS parallèles
    3. Filtrage Shapely par intersection réelle
    4. Construction des dicts d'intersection (compatible pdf/rapport.py)

    Retourne (intersections_list, plu_pct_stats).
    """
    bbox = gdf_bbox_4326(uf_gdf, buffer_m=buffer_m)
    layer_results = fetch_all_layers(bbox, layers=layers)

    intersections: List[Dict[str, Any]] = []
    plu_pct_stats: Dict[str, float] = {}

    for lr in layer_results:
        inter = _layer_to_intersection(lr, uf_gdf)
        if inter is not None:
            intersections.append(inter)
            if lr.table == "zone_urba":
                plu_pct_stats = inter.get("pct_stats", {})

    # Tri alphabétique par display_name (comme analyser_identite_fonciere)
    intersections.sort(key=lambda x: x["display_name"])

    logger.info("🎯 %d couche(s) intersectée(s) sur %d testée(s)",
                len(intersections), len(layer_results))
    return intersections, plu_pct_stats