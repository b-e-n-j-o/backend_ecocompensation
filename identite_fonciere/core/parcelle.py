"""
core/parcelle.py
Récupération de la géométrie d'une parcelle via IGN WFS Parcellaire Express.
Même logique que fetch_parcelle_geometry_ign() de identite_parcelle.py,
mais retourne un GeoDataFrame EPSG:4326 + métadonnées.
"""
from __future__ import annotations

import io
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import geopandas as gpd
import requests
from shapely.geometry import mapping

logger = logging.getLogger(__name__)

IGN_WFS = "https://data.geopf.fr/wfs/ows"
IGN_LAYER = "CADASTRALPARCELS.PARCELLAIRE_EXPRESS:parcelle"
WFS_RETRY_COUNT = 2
WFS_RETRY_BACKOFF_S = 0.35


def _get_with_retry(
    url: str,
    params: Dict[str, str],
    timeout: int,
    retries: int = WFS_RETRY_COUNT,
    backoff_s: float = WFS_RETRY_BACKOFF_S,
) -> requests.Response:
    """
    Requête GET avec retry silencieux pour les erreurs transitoires WFS.
    """
    attempts = retries + 1
    last_exc: Optional[Exception] = None
    for i in range(attempts):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            if resp.status_code >= 500:
                raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_exc = e
            if i < attempts - 1:
                time.sleep(backoff_s * (i + 1))
                continue
            raise e
    assert last_exc is not None
    raise last_exc


@dataclass
class ParcelleRef:
    section: str
    numero: str
    insee: str
    commune: str

    def __post_init__(self):
        self.section = self.section.upper().strip()
        self.numero = self.numero.strip().zfill(4)
        self.commune = self.commune.strip()
        self.insee = self.insee.strip()

    @property
    def label(self) -> str:
        return f"{self.section} {self.numero}"


@dataclass
class ParcelleResult:
    ref: ParcelleRef
    gdf: gpd.GeoDataFrame          # EPSG:4326, 1 ligne
    geojson: Dict                   # geometry GeoJSON
    contenance: Optional[float] = None
    idu: Optional[str] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def fetch_parcelle(ref: ParcelleRef, timeout: int = 30) -> ParcelleResult:
    """
    Récupère la géométrie et les attributs d'une parcelle depuis l'IGN WFS.
    Retourne toujours un ParcelleResult (error != None si échec).
    """
    logger.info("🔍 IGN WFS — parcelle %s %s (INSEE: %s)", ref.section, ref.numero, ref.insee)

    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": IGN_LAYER,
        "srsName": "EPSG:4326",
        "outputFormat": "application/json",
        "CQL_FILTER": (
            f"code_insee='{ref.insee}'"
            f" AND section='{ref.section}'"
            f" AND numero='{ref.numero}'"
        ),
    }

    try:
        r = _get_with_retry(IGN_WFS, params=params, timeout=timeout)
    except requests.RequestException as e:
        return _error(ref, f"Erreur réseau IGN WFS : {e}")

    try:
        gdf = gpd.read_file(io.BytesIO(r.content))
    except Exception as e:
        return _error(ref, f"Lecture GeoJSON IGN : {e}")

    if gdf.empty:
        return _error(ref, f"Parcelle {ref.label} non trouvée (INSEE {ref.insee})")

    row = gdf.iloc[0]
    geojson = mapping(row.geometry)

    # Contenance (m²) — attribut IGN optionnel
    contenance = None
    for col in ("contenance", "CONTENANCE", "area"):
        if col in gdf.columns and row[col] is not None:
            try:
                contenance = float(row[col])
            except (TypeError, ValueError):
                pass
            break

    idu = str(row.get("idu") or row.get("IDU") or "").strip() or None

    logger.info("✅ Parcelle %s récupérée (contenance: %s m²)", ref.label, contenance)
    return ParcelleResult(
        ref=ref,
        gdf=gdf[["geometry"]].copy(),
        geojson=geojson,
        contenance=contenance,
        idu=idu,
    )


def fetch_parcelles(refs: List[ParcelleRef]) -> List[ParcelleResult]:
    """Récupère plusieurs parcelles (séquentiel, suffisant pour une UF < 20 parcelles)."""
    results = []
    for ref in refs:
        results.append(fetch_parcelle(ref))
    ok = sum(1 for r in results if r.ok)
    logger.info("📦 %d/%d parcelles récupérées", ok, len(refs))
    return results


def _error(ref: ParcelleRef, msg: str) -> ParcelleResult:
    logger.error("❌ %s", msg)
    import geopandas as gpd
    return ParcelleResult(ref=ref, gdf=gpd.GeoDataFrame(), geojson={}, error=msg)
