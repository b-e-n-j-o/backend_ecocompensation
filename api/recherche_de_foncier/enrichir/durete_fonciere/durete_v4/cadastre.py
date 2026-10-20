# -*- coding: utf-8 -*-
"""
Référence cadastrale : normalisation d'IDU et géométrie de parcelle (WFS IGN).

IDU canonique = 14 caractères : INSEE(5) + préfixe/com_abs(3) + section(2) + numéro(4),
c'est le format de `id_parcelle` dans DVF et du cadastre Etalab.
Formats acceptés en entrée : 14 car., 13 car. (section 1 lettre non paddée),
avec ou sans espaces, minuscules.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from . import config, net

log = logging.getLogger("durete.cadastre")


@dataclass(frozen=True)
class RefParcelle:
    insee: str
    prefixe: str
    section: str   # toujours 2 caractères ("0D", "AB")
    numero: str    # 4 chiffres

    @property
    def idu(self) -> str:
        return f"{self.insee}{self.prefixe}{self.section}{self.numero}"

    @property
    def departement(self) -> str:
        return self.insee[:3] if self.insee.startswith("97") else self.insee[:2]


def parse_idu(idu: str) -> RefParcelle:
    s = "".join(str(idu).split()).upper()
    if len(s) < 10:
        raise ValueError(f"IDU trop court : '{idu}'")
    insee, numero, mid = s[:5], s[-4:], s[5:-4]
    if not numero.isdigit():
        raise ValueError(f"IDU invalide (numéro) : '{idu}'")
    if len(mid) == 5:
        prefixe, section = mid[:3], mid[3:]
    elif len(mid) == 4:
        prefixe, section = mid[:3], "0" + mid[3:]
    elif len(mid) in (1, 2):
        prefixe, section = "000", mid.rjust(2, "0")
    else:
        raise ValueError(f"IDU de longueur inattendue : '{idu}'")
    return RefParcelle(insee, prefixe, section, numero)


def normaliser_idu(idu: str) -> str:
    return parse_idu(idu).idu


# ---------------------------------------------------------------------------
# Géométrie (Parcellaire Express via WFS Géoplateforme), en Lambert-93
# ---------------------------------------------------------------------------
@lru_cache(maxsize=2048)
def _geom_cached(idu14: str):
    import geopandas as gpd
    from shapely.ops import unary_union

    ref = parse_idu(idu14)
    cql = f"code_insee='{ref.insee}' AND section='{ref.section}' AND numero='{ref.numero}'"
    if ref.prefixe != "000":
        cql += f" AND com_abs='{ref.prefixe}'"
    status, body = net.get(
        config.WFS_URL,
        {
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeNames": "CADASTRALPARCELS.PARCELLAIRE_EXPRESS:parcelle",
            "srsName": "EPSG:2154", "outputFormat": "application/json",
            "CQL_FILTER": cql,
        },
        timeout=30, ttl=30 * 86400,
    )
    if status != 200 or not body:
        raise RuntimeError(f"WFS cadastre HTTP {status} pour {idu14}")
    feats = [f for f in (json.loads(body).get("features") or []) if f.get("geometry")]
    if not feats:
        raise ValueError(f"Parcelle introuvable au cadastre IGN : {idu14}")
    gdf = gpd.GeoDataFrame.from_features(feats, crs=2154)
    return unary_union(gdf.geometry)


def geometrie_parcelle(idu: str):
    """Géométrie shapely de la parcelle en EPSG:2154 (mise en cache)."""
    return _geom_cached(normaliser_idu(idu))


def to_wgs84(geom_2154):
    from pyproj import Transformer
    from shapely.ops import transform

    t = Transformer.from_crs(2154, 4326, always_xy=True)
    return transform(t.transform, geom_2154)


def surface_m2(geom_2154) -> Optional[float]:
    try:
        return float(geom_2154.area)
    except Exception:  # noqa: BLE001
        return None
