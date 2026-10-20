# -*- coding: utf-8 -*-
"""
Couche HTTP commune : session unique, retries avec backoff (429/5xx, Retry-After),
throttling par hôte et cache (mémoire + disque optionnel).

Avant : chaque module faisait son `requests.get` nu, sans retry ni cache —
en batch, DVF re-téléchargeait les mêmes fichiers pour chaque parcelle.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config

log = logging.getLogger("durete.net")

_session: Optional[requests.Session] = None
_session_lock = threading.Lock()


def session() -> requests.Session:
    global _session
    with _session_lock:
        if _session is None:
            s = requests.Session()
            retry = Retry(
                total=3,
                backoff_factor=0.5,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset({"GET", "POST"}),
                respect_retry_after_header=True,
                raise_on_status=False,
            )
            adapter = HTTPAdapter(max_retries=retry, pool_maxsize=16)
            s.mount("https://", adapter)
            s.mount("http://", adapter)
            s.headers["User-Agent"] = config.USER_AGENT
            _session = s
        return _session


# ---------------------------------------------------------------------------
# Throttling par hôte
# ---------------------------------------------------------------------------
_last_call: dict[str, float] = {}
_throttle_lock = threading.Lock()


def throttle(host: str, min_interval: float) -> None:
    with _throttle_lock:
        now = time.monotonic()
        wait = _last_call.get(host, 0.0) + min_interval - now
        if wait > 0:
            time.sleep(wait)
        _last_call[host] = time.monotonic()


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
class _Cache:
    def __init__(self) -> None:
        self._mem: dict[str, tuple[float, int, bytes]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(url: str, params: Optional[dict]) -> str:
        full = url + ("?" + urlencode(sorted((params or {}).items()), doseq=True) if params else "")
        return hashlib.sha1(full.encode("utf-8")).hexdigest()

    def get(self, key: str, ttl: float) -> Optional[tuple[int, bytes]]:
        with self._lock:
            hit = self._mem.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1], hit[2]
        if config.CACHE_DIR:
            p = Path(config.CACHE_DIR) / key[:2] / key
            meta = p.with_suffix(".status")
            if p.exists() and meta.exists() and time.time() - p.stat().st_mtime < ttl:
                try:
                    status = int(meta.read_text())
                    body = p.read_bytes()
                    with self._lock:
                        self._mem[key] = (p.stat().st_mtime, status, body)
                    return status, body
                except (OSError, ValueError):
                    return None
        return None

    def set(self, key: str, status: int, body: bytes) -> None:
        with self._lock:
            self._mem[key] = (time.time(), status, body)
        if config.CACHE_DIR:
            try:
                d = Path(config.CACHE_DIR) / key[:2]
                d.mkdir(parents=True, exist_ok=True)
                (d / key).write_bytes(body)
                (d / key).with_suffix(".status").write_text(str(status))
            except OSError as e:  # le cache disque ne doit jamais casser le pipeline
                log.debug("cache disque indisponible : %s", e)

    def clear(self) -> None:
        with self._lock:
            self._mem.clear()


CACHE = _Cache()


def get(
    url: str,
    params: Optional[dict] = None,
    *,
    timeout: Optional[float] = None,
    ttl: float = 0,
    headers: Optional[dict] = None,
    min_interval: float = 0.0,
) -> tuple[Optional[int], bytes]:
    """
    GET avec retry + cache. Retourne (status_http, corps). status=None si erreur réseau.
    Seules les réponses 200/404 sont mises en cache (un 404 DVF est une info stable).
    """
    key = CACHE.key(url, params)
    if ttl > 0:
        hit = CACHE.get(key, ttl)
        if hit is not None:
            return hit
    if min_interval:
        throttle(urlparse(url).netloc, min_interval)
    try:
        r = session().get(url, params=params, timeout=timeout or config.HTTP_TIMEOUT, headers=headers)
    except requests.RequestException as e:
        log.warning("HTTP erreur %s : %s", url, e)
        return None, b""
    if ttl > 0 and r.status_code in (200, 404):
        CACHE.set(key, r.status_code, r.content)
    return r.status_code, r.content


def get_json(url: str, params: Optional[dict] = None, **kw: Any) -> tuple[Optional[int], Any]:
    status, body = get(url, params, **kw)
    if status != 200:
        return status, None
    try:
        return status, json.loads(body)
    except ValueError:
        log.warning("JSON invalide depuis %s", url)
        return status, None
