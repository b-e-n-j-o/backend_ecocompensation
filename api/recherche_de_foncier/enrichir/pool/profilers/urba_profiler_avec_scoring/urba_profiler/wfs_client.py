"""
wfs_client.py — Récupération à la volée des entités GPU via le WFS
data.geopf.fr, pour une bbox donnée (en Lambert-93).

Reprend la mécanique éprouvée des scripts ETL (ingest_*.py) :
- GetFeature WFS 2.0.0, sortie GeoJSON, bbox suffixée par le SRS
- limite GPU de 5000 entités/requête -> subdivision récursive de la bbox
- déduplication des entités présentes dans plusieurs tuiles
…mais SANS écriture en base : les entités restent en mémoire le temps du run.

Cache des réponses (pour limiter les allers-retours réseau en dev) :
- mémoire (process) avec TTL
- disque optionnel : variable d'env URBA_WFS_CACHE_DIR=/chemin (réponses JSON gzip)
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from .config import WFS_MAX_FEATURES, ProfilerSettings
from .geometry import TARGET_EPSG, bbox_to_epsg, clean, looks_like_l93, reproject, swap_xy

log = logging.getLogger(__name__)

Bbox = Tuple[float, float, float, float]


class WfsError(RuntimeError):
    pass


@dataclass
class Feature:
    fid: str
    props: Dict[str, Any]  # clés en minuscules
    geom: BaseGeometry  # EPSG:2154


@dataclass
class FetchStats:
    requests: int = 0
    cache_hits: int = 0
    truncated_tiles: int = 0  # tuiles encore saturées à profondeur max


# ---------------------------------------------------------------------------
# Cache des réponses brutes
# ---------------------------------------------------------------------------
class ResponseCache:
    def __init__(self, ttl_s: int = 3600, disk_dir: Optional[str] = None):
        self.ttl_s = ttl_s
        self._mem: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self.disk_dir = Path(disk_dir) if disk_dir else None
        if self.disk_dir:
            self.disk_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(params: Dict[str, Any], url: str) -> str:
        raw = url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
        return hashlib.sha1(raw.encode()).hexdigest()

    def get(self, k: str) -> Optional[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            hit = self._mem.get(k)
            if hit and now - hit[0] < self.ttl_s:
                return hit[1]
        if self.disk_dir:
            p = self.disk_dir / f"{k}.json.gz"
            if p.exists() and now - p.stat().st_mtime < self.ttl_s:
                try:
                    with gzip.open(p, "rt", encoding="utf-8") as f:
                        data = json.load(f)
                    with self._lock:
                        self._mem[k] = (now, data)
                    return data
                except Exception:  # cache corrompu -> on ignore
                    pass
        return None

    def set(self, k: str, data: Dict[str, Any]) -> None:
        with self._lock:
            self._mem[k] = (time.time(), data)
        if self.disk_dir:
            try:
                with gzip.open(self.disk_dir / f"{k}.json.gz", "wt", encoding="utf-8") as f:
                    json.dump(data, f)
            except Exception as e:
                log.debug("cache disque KO: %s", e)


_DEFAULT_CACHE: Optional[ResponseCache] = None


def default_cache() -> ResponseCache:
    """Cache partagé au niveau du process (TTL réglable via URBA_WFS_CACHE_TTL)."""
    global _DEFAULT_CACHE
    if _DEFAULT_CACHE is None:
        _DEFAULT_CACHE = ResponseCache(
            ttl_s=int(os.getenv("URBA_WFS_CACHE_TTL", "3600")),
            disk_dir=os.getenv("URBA_WFS_CACHE_DIR") or None,
        )
    return _DEFAULT_CACHE


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
def _subdivide(b: Bbox) -> List[Bbox]:
    minx, miny, maxx, maxy = b
    mx, my = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    return [(minx, miny, mx, my), (mx, miny, maxx, my), (minx, my, mx, maxy), (mx, my, maxx, maxy)]


class WfsClient:
    """
    Client WFS GPU. Une instance par run (les sessions HTTP sont par thread).
    `get_features(typename, bbox_2154)` est la seule méthode utilisée par le service :
    on peut la remplacer par un faux client dans les tests.
    """

    def __init__(self, settings: ProfilerSettings, cache: Optional[ResponseCache] = None):
        self.s = settings
        self.cache = cache if cache is not None else default_cache()
        self._local = threading.local()
        self.stats = FetchStats()
        self._stats_lock = threading.Lock()
        self._fetch_epsg = int(settings.fetch_srs.split(":")[-1])

    # -- HTTP ---------------------------------------------------------------
    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers["User-Agent"] = "kerelia-urba-profiler/0.1"
            self._local.session = s
        return s

    def _params(self, typename: str, bbox_2154: Bbox) -> Dict[str, Any]:
        b = bbox_to_epsg(bbox_2154, self._fetch_epsg)
        return {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": typename,
            "outputFormat": "application/json",
            "srsName": self.s.fetch_srs,
            # ordre x,y (lon,lat en 4326) + SRS en suffixe : convention de nos scripts ETL
            "bbox": f"{b[0]:.7f},{b[1]:.7f},{b[2]:.7f},{b[3]:.7f},{self.s.fetch_srs}",
            "count": WFS_MAX_FEATURES,
        }

    def _get_json(self, params: Dict[str, Any]) -> Dict[str, Any]:
        k = ResponseCache.key(params, self.s.wfs_url)
        cached = self.cache.get(k)
        if cached is not None:
            with self._stats_lock:
                self.stats.cache_hits += 1
            return cached

        last_exc: Optional[Exception] = None
        for attempt in range(self.s.retries):
            try:
                with self._stats_lock:
                    self.stats.requests += 1
                r = self._session().get(self.s.wfs_url, params=params, timeout=self.s.timeout_s)
                r.raise_for_status()
                data = r.json()
                if not isinstance(data, dict) or "features" not in data:
                    # Le GPU renvoie parfois une ExceptionReport XML avec un 200
                    raise WfsError(f"Réponse WFS inattendue: {r.text[:200]}")
                self.cache.set(k, data)
                return data
            except Exception as e:  # réseau, JSON, ExceptionReport
                last_exc = e
                if attempt < self.s.retries - 1:
                    time.sleep(1.0 * (attempt + 1))
        raise WfsError(f"{params.get('typeNames')} : {str(last_exc)[:300]}")

    # -- Conversion ---------------------------------------------------------
    def _to_feature(self, f: Dict[str, Any], idx: int) -> Optional[Feature]:
        g = f.get("geometry")
        if not g:
            return None
        try:
            geom = shape(g)
        except Exception:
            return None
        if self._fetch_epsg != TARGET_EPSG:
            g2 = reproject(geom, self._fetch_epsg, TARGET_EPSG)
            if not looks_like_l93(g2):
                # Garde-fou ordre des axes (lat/lon renvoyé au lieu de lon/lat)
                g2 = reproject(swap_xy(geom), self._fetch_epsg, TARGET_EPSG)
            geom = g2
        geom = clean(geom)
        props = {str(k).lower(): v for k, v in (f.get("properties") or {}).items()}
        fid = str(f.get("id") or props.get("gml_id") or props.get("gid") or f"_{idx}_{hash(geom.wkb)}")
        return Feature(fid=fid, props=props, geom=geom)

    # -- API ----------------------------------------------------------------
    def get_features(self, typename: str, bbox_2154: Bbox) -> List[Feature]:
        """Toutes les entités de `typename` intersectant la bbox (2154), dédupliquées."""
        out: Dict[str, Feature] = {}
        self._fetch_rec(typename, bbox_2154, 0, out)
        return list(out.values())

    def _fetch_rec(self, typename: str, bbox: Bbox, depth: int, out: Dict[str, Feature]) -> None:
        data = self._get_json(self._params(typename, bbox))
        feats = data.get("features") or []
        matched = data.get("numberMatched")
        saturated = len(feats) >= WFS_MAX_FEATURES or (
            isinstance(matched, int) and matched > len(feats)
        )
        if saturated:
            if depth < self.s.max_subdivision_depth:
                for sub in _subdivide(bbox):
                    self._fetch_rec(typename, sub, depth + 1, out)
                return
            with self._stats_lock:
                self.stats.truncated_tiles += 1
            log.warning("%s : tuile saturée à profondeur max %s (%s)", typename, depth, bbox)

        for i, f in enumerate(feats):
            feat = self._to_feature(f, i)
            if feat is not None and feat.fid not in out:
                out[feat.fid] = feat
