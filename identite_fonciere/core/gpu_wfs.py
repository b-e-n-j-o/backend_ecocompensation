"""
core/gpu_wfs.py
Client WFS pour le Géoportail de l'Urbanisme (GPU).

Endpoint : https://data.geopf.fr/wfs/ows
Couches  : wfs_du:* (urbanisme) et wfs_sup:* (servitudes)

Catalogue vérifié sur data.geopf.fr (avril 2026) :
  PLU         : wfs_du:zone_urba                     ✅
  Servitudes  : wfs_sup:assiette_sup_s / _l           ✅
  Prescriptions: wfs_du:prescription_surf / _lin / _pct ✅
  Informations : wfs_du:info_surf / info_lin / info_pct ✅
  Préemption  : wfs_du:zone_pdc                       ❌ inexistante
                → DPU via info_surf typeinf=04         ✅

Notes terrain :
- libelong souvent vide → skippé si chaîne vide
- DPU (typeinf=04) dans info_surf : a sa page dédiée dans le rapport PDF
- info_lin a 36 entités sur Latresne (alignements, reculs, etc.)
- assiette_sup_s/l : assiettes des SUP (périmètre d'effet réglementaire)
"""
from __future__ import annotations

import io
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import geopandas as gpd
import requests

logger = logging.getLogger(__name__)

# Endpoint unifié — tous les flux GPU et IGN sont maintenant sur data.geopf.fr
GPU_WFS = "https://data.geopf.fr/wfs/ows"
WFS_RETRY_COUNT = 2
WFS_RETRY_BACKOFF_S = 0.35


def _get_with_retry(
    url: str,
    params: Dict[str, Any],
    timeout: int,
    retries: int = WFS_RETRY_COUNT,
    backoff_s: float = WFS_RETRY_BACKOFF_S,
) -> requests.Response:
    """
    Requête HTTP GET avec retry silencieux (erreurs réseau/timeout/5xx).
    """
    last_exc: Optional[Exception] = None
    attempts = retries + 1
    for i in range(attempts):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            # Retry sur indisponibilité transitoire côté serveur.
            if resp.status_code >= 500:
                raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_exc = e
            if i < attempts - 1:
                # Backoff léger pour laisser le WFS se stabiliser.
                time.sleep(backoff_s * (i + 1))
                continue
            raise e
    # Défensif (la boucle lève déjà à la dernière tentative).
    assert last_exc is not None
    raise last_exc

# Catalogue des couches GPU
# keep : attributs à extraire (dans l'ordre d'affichage souhaité)
# group_by : clé de déduplication (une ligne par valeur distincte)
# note_typeinf : pour info_surf, on filtre selon typeinf pour distinguer
#   DPU (04) des autres informations génériques
GPU_LAYERS = [
    # ── PLU / PLUi ────────────────────────────────────────────────────────
    {
        "layer": "wfs_du:zone_urba",
        "table": "zone_urba",
        "display_name": "Zonage PLU / PLUi",
        "article": "3",
        "type": "information",
        "geom_type": "polygon",
        # libelong souvent vide — on le garde, le PDF ignore les vides
        "keep": ["libelle", "libelong", "typezone", "destdomi"],
        "group_by": "libelle",
        "attribut_discriminant": "libelle",
    },
    # ── Servitudes d'utilité publique ─────────────────────────────────────
    {
        "layer": "wfs_sup:assiette_sup_s",
        "table": "assiette_sup_s",
        "display_name": "Servitudes d'utilité publique",
        "article": "4",
        "type": "servitude",
        "geom_type": "polygon",
        # suptype = code (ex: ac1, pt2…), nomsuplitt = libellé long
        "keep": ["suptype", "nomsuplitt", "nomass", "typeass", "idgen"],
        "group_by": "suptype",
        "attribut_discriminant": "suptype",
    },
    {
        "layer": "wfs_sup:assiette_sup_l",
        "table": "assiette_sup_l",
        "display_name": "Servitudes linéaires (SUP)",
        "article": "4",
        "type": "servitude",
        "geom_type": "lineaire",
        "keep": ["suptype", "nomsuplitt", "nomass", "typeass"],
        "group_by": "suptype",
        "attribut_discriminant": "suptype",
    },
    # ── Prescriptions ─────────────────────────────────────────────────────
    {
        "layer": "wfs_du:prescription_surf",
        "table": "prescription_surf",
        "display_name": "Prescriptions surfaciques",
        "article": "7",
        "type": "prescription",
        "geom_type": "polygon",
        # txt = référence réglementaire (ex: ER pour emplacement réservé)
        # typepsc = code type (02 = risque, 05 = emplacement réservé…)
        "keep": ["libelle", "txt", "typepsc"],
        "group_by": "libelle",
        "attribut_discriminant": "libelle",
    },
    {
        "layer": "wfs_du:prescription_lin",
        "table": "prescription_lin",
        "display_name": "Prescriptions linéaires",
        "article": "7",
        "type": "prescription",
        "geom_type": "lineaire",
        "keep": ["libelle", "txt", "typepsc"],
        "group_by": "libelle",
        "attribut_discriminant": "libelle",
    },
    {
        "layer": "wfs_du:prescription_pct",
        "table": "prescription_pct",
        "display_name": "Prescriptions ponctuelles",
        "article": "7",
        "type": "prescription",
        "geom_type": "point",
        "keep": ["libelle", "txt", "typepsc"],
        "group_by": "libelle",
        "attribut_discriminant": "libelle",
    },
    # ── Informations réglementaires ───────────────────────────────────────
    # info_surf : polygones d'information. Contient notamment le DPU
    # (typeinf=04) qui a sa page dédiée, mais aussi espaces boisés,
    # zones archéologiques, etc. La couche complète reste dans article 7.
    {
        "layer": "wfs_du:info_surf",
        "table": "info_surf",
        "display_name": "Informations surfaciques",
        "article": "7",
        "type": "information",
        "geom_type": "polygon",
        "keep": ["libelle", "txt", "typeinf"],
        "group_by": "libelle",
        "attribut_discriminant": "libelle",
    },
    # info_lin : informations linéaires (alignements, reculs, coulées vertes…)
    # Vérifié présent — 36 entités sur bbox Latresne.
    {
        "layer": "wfs_du:info_lin",
        "table": "info_lin",
        "display_name": "Informations linéaires",
        "article": "7",
        "type": "information",
        "geom_type": "lineaire",
        "keep": ["libelle", "txt", "typeinf"],
        "group_by": "libelle",
        "attribut_discriminant": "libelle",
    },
    # info_pct : informations ponctuelles (arbres remarquables, éléments bâtis…)
    # Vérifié présent (0 entités Latresne mais couche accessible).
    {
        "layer": "wfs_du:info_pct",
        "table": "info_pct",
        "display_name": "Informations ponctuelles",
        "article": "7",
        "type": "information",
        "geom_type": "point",
        "keep": ["libelle", "txt", "typeinf"],
        "group_by": "libelle",
        "attribut_discriminant": "libelle",
    },
    # zone_pdc SUPPRIMÉE : couche inexistante sur data.geopf.fr
    # (vérifié check_wfs_preemption.py). DPU géré via info_surf/typeinf=04.
]

GPU_LAYERS_BY_TABLE = {cfg["table"]: cfg for cfg in GPU_LAYERS}


@dataclass
class LayerResult:
    table: str
    display_name: str
    article: str
    layer_type: str
    geom_type: str
    status: str          # ok | empty | error
    gdf: gpd.GeoDataFrame = field(default_factory=gpd.GeoDataFrame)
    hits: int = 0        # nombre d'entités dans la bbox (pré-comptage)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == "ok" and not self.gdf.empty


def _hits_count(typename: str, bbox: Tuple, timeout: int = 15) -> int:
    """
    Compte les entités dans la bbox via resultType=hits (sans télécharger les géométries).
    Retourne 0 si erreur.
    """
    minx, miny, maxx, maxy = bbox
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": typename,
        "srsName": "EPSG:4326",
        "bbox": f"{minx},{miny},{maxx},{maxy},EPSG:4326",
        "resultType": "hits",
    }
    try:
        r = _get_with_retry(GPU_WFS, params=params, timeout=timeout)
        m = re.search(r'numberMatched="(\d+)"', r.text)
        return int(m.group(1)) if m else 0
    except Exception as e:
        logger.debug("hits error %s : %s", typename, e)
        return 0


def _fetch_layer(
    cfg: Dict[str, Any],
    bbox: Tuple[float, float, float, float],
    timeout: int = 30,
    max_features: int = 2000,
) -> LayerResult:
    """
    Récupère une couche GPU pour la bbox donnée.
    Fait d'abord un hits count pour éviter les téléchargements inutiles.
    """
    layer = cfg["layer"]
    table = cfg["table"]
    minx, miny, maxx, maxy = bbox

    def _empty(status="empty", error=None):
        return LayerResult(
            table=table,
            display_name=cfg["display_name"],
            article=cfg["article"],
            layer_type=cfg["type"],
            geom_type=cfg["geom_type"],
            status=status,
            error=error,
        )

    # Pré-comptage : skip si 0 entités dans la bbox
    n_hits = _hits_count(layer, bbox, timeout=min(timeout, 15))
    if n_hits == 0:
        logger.debug("   skip %s (0 hits bbox)", table)
        return _empty()

    logger.info("   📥 %s : %d hits → fetch …", table, n_hits)

    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": layer,
        "srsName": "EPSG:4326",
        "outputFormat": "application/json",
        "bbox": f"{minx},{miny},{maxx},{maxy},EPSG:4326",
        "count": str(max_features),
    }

    try:
        r = _get_with_retry(GPU_WFS, params=params, timeout=timeout)
    except requests.RequestException as e:
        logger.warning("⚠️  %s — erreur réseau : %s", table, e)
        return _empty("error", str(e))

    try:
        gdf = gpd.read_file(io.BytesIO(r.content))
    except Exception as e:
        logger.warning("⚠️  %s — lecture GeoJSON : %s", table, e)
        return _empty("error", str(e))

    if gdf.empty or "geometry" not in gdf.columns:
        return _empty()

    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs("EPSG:4326")

    logger.info("   ✅ %s : %d features récupérées", table, len(gdf))
    return LayerResult(
        table=table,
        display_name=cfg["display_name"],
        article=cfg["article"],
        layer_type=cfg["type"],
        geom_type=cfg["geom_type"],
        status="ok",
        gdf=gdf,
        hits=n_hits,
    )


def fetch_all_layers(
    bbox: Tuple[float, float, float, float],
    layers: Optional[List[str]] = None,
    max_workers: int = 4,
    timeout: int = 30,
) -> List[LayerResult]:
    """
    Récupère toutes les couches GPU en parallèle pour la bbox.
    Chaque couche fait d'abord un hits count — seules les couches non-vides
    sont téléchargées, ce qui évite du trafic réseau inutile.

    layers : liste de table names à restreindre (None = toutes).
    """
    cfgs = GPU_LAYERS if layers is None else [
        c for c in GPU_LAYERS if c["table"] in layers
    ]
    logger.info(
        "📡 GPU WFS — %d couches | bbox %.5f,%.5f,%.5f,%.5f",
        len(cfgs), *bbox,
    )

    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_fetch_layer, cfg, bbox, timeout): cfg
            for cfg in cfgs
        }
        for fut in as_completed(futures):
            results.append(fut.result())

    ok_count = sum(1 for r in results if r.ok)
    logger.info("🎯 %d/%d couches avec données dans la bbox", ok_count, len(cfgs))
    return results