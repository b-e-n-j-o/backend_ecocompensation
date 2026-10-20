# -*- coding: utf-8 -*-
"""
Zonage d'urbanisme de la parcelle (Géoportail de l'Urbanisme via API Carto IGN).

NOUVEAU en V4. Pourquoi : la constructibilité est le premier déterminant de
l'attente de prix d'un propriétaire. Une parcelle en zone U/AU porte une
espérance de plus-value → beaucoup plus dure à acquérir pour de la compensation
qu'une parcelle en A/N. C'est une « extension Kerelia » au guide, désactivable
(config.SURCHARGE_ZONAGE_ACTIVE).

Le module urba KerFoncier (croisement pool × WFS GPU) peut être injecté à la
place de ce fetch via `Sources.fetch_gpu` dans pipeline.py, pour ne pas
dupliquer les appels.

Classification : `typezone` CNIG d'abord, `libelle` en repli (saisies CNIG
parfois erronées/vides — on ne s'y fie pas aveuglément).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from .. import cadastre, config, net

log = logging.getLogger("durete.gpu")


def classer_zone(typezone: Optional[str], libelle: Optional[str]) -> str:
    t = (typezone or "").strip().upper()
    if t in ("AH", "NH"):
        return "STECAL"
    if t.startswith("AU"):
        return "AU"
    if t == "U":
        return "U"
    if t.startswith("A"):
        return "A"
    if t.startswith("N"):
        return "N"
    lib = (libelle or "").strip().upper()
    if re.match(r"^\d?\s*AU", lib):
        return "AU"
    if re.match(r"^U", lib):
        return "U"
    if re.match(r"^A", lib):
        return "A"
    if re.match(r"^N", lib):
        return "N"
    return "INCONNU"


_SECTEUR_CC = {"01": "U", "02": "N", "03": "U", "99": "INCONNU"}


def analyser_zonage(features: list[dict], parcelle_2154, source: str = "plu") -> dict:
    """Part de surface de la parcelle par classe de zone (pur)."""
    from shapely.geometry import shape

    surf = float(parcelle_2154.area) or 1.0
    parts: dict[str, float] = {}
    zones = []
    for f in features:
        p = f.get("properties") or {}
        try:
            g = shape(f["geometry"])
        except Exception:  # noqa: BLE001
            continue
        if source == "cc":
            classe = _SECTEUR_CC.get(str(p.get("typesect") or "").zfill(2), "INCONNU")
        else:
            classe = classer_zone(p.get("typezone"), p.get("libelle"))
        aire = float(g.intersection(parcelle_2154).area) if g.is_valid else 0.0
        if aire <= 0:
            continue
        parts[classe] = parts.get(classe, 0.0) + aire
        zones.append({"libelle": p.get("libelle") or p.get("typesect"), "typezone": p.get("typezone"),
                      "classe": classe, "part": round(aire / surf, 3),
                      "libelong": (p.get("libelong") or "")[:160], "idurba": p.get("idurba")})
    if not parts:
        return {"statut": "aucune_zone", "classe_dominante": None, "parts": {}, "zones": []}
    dom = max(parts, key=parts.get)
    return {
        "statut": "ok",
        "document": source,
        "classe_dominante": dom,
        "parts": {k: round(v / surf, 3) for k, v in parts.items()},
        "zones": sorted(zones, key=lambda z: -z["part"]),
    }


def _features_wgs84_to_2154(features: list[dict]) -> list[dict]:
    from pyproj import Transformer
    from shapely.geometry import mapping, shape
    from shapely.ops import transform

    t = Transformer.from_crs(4326, 2154, always_xy=True)
    out = []
    for f in features:
        try:
            g = transform(t.transform, shape(f["geometry"]))
            out.append({"properties": f.get("properties") or {}, "geometry": mapping(g.buffer(0))})
        except Exception:  # noqa: BLE001
            continue
    return out


def fetch_zonage(idu: str) -> dict:
    try:
        parcelle = cadastre.geometrie_parcelle(idu)
        geom = json.dumps(cadastre.to_wgs84(parcelle.simplify(1.0)).__geo_interface__)
        for endpoint, source in (("zone-urba", "plu"), ("secteur-cc", "cc")):
            status, data = net.get_json(f"{config.APICARTO_GPU_URL}/{endpoint}", {"geom": geom},
                                        ttl=30 * 86400, timeout=30)
            if status != 200 or not data:
                continue
            feats = data.get("features") or []
            if feats:
                return analyser_zonage(_features_wgs84_to_2154(feats), parcelle, source)
        return {"statut": "aucune_zone", "classe_dominante": None, "parts": {}, "zones": [],
                "note": "Aucun PLU/CC publié au GPU : RNU probable (constructibilité limitée)"}
    except Exception as e:  # noqa: BLE001
        log.warning("GPU indisponible pour %s : %s", idu, e)
        return {"statut": "indisponible", "erreur": str(e), "classe_dominante": None, "parts": {}}
