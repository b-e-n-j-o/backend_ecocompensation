# -*- coding: utf-8 -*-
"""
RPG (Registre Parcellaire Graphique) multi-millésimes via WFS Géoplateforme.

Corrections vs V3 :
  - calcul des surfaces en Lambert-93 (EPSG:2154) — V3 calculait en EPSG:3857,
    qui surestime les surfaces d'un facteur ~1/cos²(lat) (≈ ×2 en Gironde) ;
  - seuil de recouvrement significatif (RPG_SEUIL_RATIO / RPG_SEUIL_M2) :
    un îlot voisin qui « touche » la parcelle sur un liseré n'est plus compté ;
  - un millésime en échec réseau est marqué `indisponible` et NE casse PAS
    la continuité (V3 le comptait comme « non agricole ») ;
  - les millésimes sont interrogés en parallèle, résultat mis en cache ;
  - l'inférence « bail rural » est sortie du module : elle dépend du
    propriétaire (un GAEC qui déclare sa propre PAC n'a pas de bail) et vit
    désormais dans scoring.py.
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Optional

from .. import cadastre, config, net
from ..referentiels import libelle_culture

log = logging.getLogger("durete.rpg")

RPG_LAYERS = {
    2010: "RPG.2010:rpg_2010",
    2013: "RPG.2013:rpg_2013",
    2014: "RPG.2014:rpg_2014",
    **{y: f"RPG.{y}:parcelles_graphiques" for y in range(2015, 2025)},
}
_COLS_CULTURE = ("code_cultu", "code_cultur", "culture_d1", "cult_maj", "code_group")


def _fetch_annee(typename: str, bbox: tuple):
    """GeoDataFrame (EPSG:2154) des îlots intersectant la bbox ; None si erreur ; vide si rien."""
    import geopandas as gpd

    status, body = net.get(
        config.WFS_URL,
        {
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeName": typename, "outputFormat": "application/json",
            "srsName": "EPSG:2154",
            "bbox": f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]},EPSG:2154",
            "count": 2000,
        },
        timeout=90, ttl=90 * 86400,
    )
    if status != 200 or not body:
        return None
    try:
        feats = json.loads(body).get("features") or []
    except ValueError as e:
        log.warning("RPG %s illisible : %s", typename, e)
        return None
    feats = [f for f in feats if f.get("geometry")]
    if not feats:
        return gpd.GeoDataFrame(geometry=[], crs=2154)
    return gpd.GeoDataFrame.from_features(feats, crs=2154)


def analyser_annee(gdf, parcelle) -> dict:
    """Statut d'un millésime pour une parcelle (pur — prend des géométries en 2154)."""
    if gdf is None:
        return {"status": "indisponible"}
    surf_parcelle = float(parcelle.area) or 1.0
    if gdf.empty:
        return {"status": "non_declare", "couverture": 0.0}
    inter = gdf[gdf.intersects(parcelle)].copy()
    if inter.empty:
        return {"status": "non_declare", "couverture": 0.0}
    inter["geometry"] = inter.geometry.intersection(parcelle)
    inter["area"] = inter.geometry.area
    total = float(inter["area"].sum())
    ratio = min(1.0, total / surf_parcelle)
    if ratio < config.RPG_SEUIL_RATIO and total < config.RPG_SEUIL_M2:
        return {"status": "non_declare", "couverture": round(ratio, 3), "contact_marginal_m2": round(total)}
    col = next((c for c in _COLS_CULTURE if c in inter.columns), None)
    cultures: dict[str, float] = {}
    if col:
        for code, area in zip(inter[col], inter["area"]):
            if code:
                cultures[str(code)] = cultures.get(str(code), 0.0) + float(area)
    return {
        "status": "agricole",
        "surface_m2": round(total),
        "couverture": round(ratio, 3),
        "cultures": {k: round(v) for k, v in sorted(cultures.items(), key=lambda kv: -kv[1])},
    }


def analyser_continuite(by_year: dict[int, dict]) -> dict:
    """Continuité sur les seuls millésimes OBSERVÉS (indisponibles ignorés)."""
    observes = sorted(y for y, d in by_year.items() if d.get("status") != "indisponible")
    agricoles = [y for y in observes if by_year[y]["status"] == "agricole"]
    max_run = run = 0
    for y in observes:
        run = run + 1 if by_year[y]["status"] == "agricole" else 0
        max_run = max(max_run, run)
    codes: dict[str, float] = {}
    for y in agricoles:
        for c, a in by_year[y].get("cultures", {}).items():
            codes[c] = codes.get(c, 0) + a
    derniere = agricoles[-1] if agricoles else None
    return {
        "nb_millesimes_observes": len(observes),
        "nb_millesimes_indisponibles": len(by_year) - len(observes),
        "nb_annees_agricoles": len(agricoles),
        "annees_agricoles": agricoles,
        "max_consecutif": max_run,
        "derniere_annee_agricole": derniere,
        "declaree_dernier_millesime": bool(observes) and derniere == observes[-1],
        "part_annees_agricoles": round(len(agricoles) / len(observes), 2) if observes else None,
        "cultures_principales": [
            {"code": c, "libelle": libelle_culture(c)}
            for c, _ in sorted(codes.items(), key=lambda kv: -kv[1])[:5]
        ],
    }


@lru_cache(maxsize=1024)
def _fetch_rpg_cached(idu14: str, annees: tuple) -> dict:
    parcelle = cadastre.geometrie_parcelle(idu14)
    bbox = parcelle.buffer(5).bounds
    layers = {y: RPG_LAYERS[y] for y in annees if y in RPG_LAYERS}
    with ThreadPoolExecutor(max_workers=config.RPG_MAX_WORKERS) as ex:
        gdfs = dict(zip(layers, ex.map(lambda t: _fetch_annee(t, bbox), layers.values())))
    by_year = {y: analyser_annee(gdfs[y], parcelle) for y in sorted(layers)}
    return {"idu": idu14, "surface_parcelle_m2": round(parcelle.area),
            "summary": analyser_continuite(by_year), "by_year": by_year}


def fetch_rpg(idu: str, annees: Optional[list[int]] = None) -> dict:
    try:
        return _fetch_rpg_cached(cadastre.normaliser_idu(idu), tuple(sorted(annees or RPG_LAYERS)))
    except Exception as e:  # noqa: BLE001
        log.warning("RPG indisponible pour %s : %s", idu, e)
        return {"idu": idu, "erreur": str(e), "summary": None, "by_year": {}}
