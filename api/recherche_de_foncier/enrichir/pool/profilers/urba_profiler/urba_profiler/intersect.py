"""
intersect.py — Logique métier d'intersection cible (parcelle / UF) × entités GPU.

Deux étapes :
1. `measure_feature` : mesure UNE entité GPU contre la cible et décide si elle
   est retenue (position interieur / limite), écartée (contact de limite,
   sous le seuil) ou listée en voisinage.
2. `aggregate` : fusionne les entités identiques (même valeur des champs
   `group_by` de la couche) en un seul `Element` de rapport, en sommant les
   mesures. Ex. une zone "A" découpée en 3 polygones -> 1 ligne "A — 87 %".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import shapely
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .config import LayerDef, ProfilerSettings
from .geometry import to_geojson
from .nomenclature import lookup
from .schemas import Element, Lien, Mesure
from .wfs_client import Feature

# ---------------------------------------------------------------------------
# Cible préparée (géométries dérivées calculées une seule fois)
# ---------------------------------------------------------------------------


@dataclass
class Target:
    id: str
    geom: BaseGeometry  # 2154
    inner: BaseGeometry  # cible rétrécie de edge_tolerance (test "contact de limite")
    area: float

    @classmethod
    def build(cls, id_: str, geom: BaseGeometry, edge_tol: float) -> "Target":
        inner = geom.buffer(-edge_tol) if edge_tol > 0 else geom
        if inner.is_empty:  # parcelle minuscule / très étroite
            inner = geom
        shapely.prepare(geom)
        shapely.prepare(inner)
        return cls(id=id_, geom=geom, inner=inner, area=geom.area)


# ---------------------------------------------------------------------------
# Mesure d'une entité
# ---------------------------------------------------------------------------


@dataclass
class Hit:
    """Résultat brut pour 1 entité GPU vs 1 cible."""

    feature: Feature
    bucket: str  # "retenu" | "ecarte" | "voisinage"
    position: str  # interieur | limite | voisinage
    surface_m2: Optional[float] = None
    longueur_m: Optional[float] = None
    nb_points: Optional[int] = None
    distance_m: Optional[float] = None
    raison_ecart: Optional[str] = None
    inter_geom: Optional[BaseGeometry] = None


def _nb_points(g: BaseGeometry) -> int:
    if g is None or g.is_empty:
        return 0
    return int(shapely.get_num_geometries(g)) if g.geom_type.startswith("Multi") else 1


def measure_feature(t: Target, f: Feature, kind: str, s: ProfilerSettings) -> Optional[Hit]:
    g = f.geom
    if g is None or g.is_empty:
        return None

    intersects = t.geom.intersects(g)

    # --- Hors cible : limite (linéaire/ponctuel) ou voisinage ----------------
    if not intersects:
        d = t.geom.distance(g)
        if kind in ("lin", "pct") and d <= s.edge_tolerance_m:
            if s.keep_linear_on_boundary:
                return Hit(f, "retenu", "limite", longueur_m=0.0 if kind == "lin" else None,
                           nb_points=_nb_points(g) if kind == "pct" else None, distance_m=round(d, 2))
            return Hit(f, "ecarte", "limite", distance_m=round(d, 2), raison_ecart="contact_limite")
        if s.voisinage_m > 0 and d <= s.voisinage_m:
            return Hit(f, "voisinage", "voisinage", distance_m=round(d, 1))
        return None

    # --- Surfacique -----------------------------------------------------------
    if kind == "surf":
        inter = t.geom.intersection(g)
        area = inter.area
        if not t.inner.intersects(g):
            # Ne touche que la bande de tolérance : décalage cadastre/PLU, pas une vraie intersection
            return Hit(f, "ecarte", "limite", surface_m2=round(area, 1), distance_m=0.0,
                       raison_ecart="contact_limite", inter_geom=inter)
        pct = (area / t.area * 100.0) if t.area > 0 else 0.0
        if pct < s.min_pct_surface and area < s.min_area_keep_m2:
            return Hit(f, "ecarte", "interieur", surface_m2=round(area, 1),
                       raison_ecart=f"sous_seuil_{s.min_pct_surface}pct", inter_geom=inter)
        return Hit(f, "retenu", "interieur", surface_m2=area, inter_geom=inter)

    # --- Linéaire -------------------------------------------------------------
    if kind == "lin":
        inter = t.geom.intersection(g)
        length = inter.length
        inner_len = t.inner.intersection(g).length
        position = "interieur" if inner_len > 0 else "limite"
        if position == "limite" and not s.keep_linear_on_boundary:
            return Hit(f, "ecarte", "limite", longueur_m=round(length, 1), raison_ecart="contact_limite")
        return Hit(f, "retenu", position, longueur_m=length, inter_geom=inter,
                   distance_m=0.0 if position == "limite" else None)

    # --- Ponctuel -------------------------------------------------------------
    inter = t.geom.intersection(g)
    n = _nb_points(inter)
    position = "interieur" if t.inner.intersects(g) else "limite"
    return Hit(f, "retenu", position, nb_points=n, inter_geom=inter)


# ---------------------------------------------------------------------------
# Agrégation en éléments de rapport
# ---------------------------------------------------------------------------


def _norm_val(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("null", "none", "nan") else s


def _clean_attr(v: Any) -> Any:
    if isinstance(v, str):
        v = v.strip()
        return None if v == "" or v.lower() in ("null", "none") else v
    return v


def _liens(attrs: Dict[str, Any]) -> List[Lien]:
    out: List[Lien] = []
    if attrs.get("urlfic"):
        out.append(Lien(type="reglement", url=str(attrs["urlfic"]), nom=attrs.get("nomfic")))
    if attrs.get("urlreg"):
        out.append(Lien(type="acte_sup", url=str(attrs["urlreg"]), nom=attrs.get("nomreg")))
    return out


def _quality_flags(layer: LayerDef, attrs: Dict[str, Any]) -> List[str]:
    flags: List[str] = []
    if layer.famille in ("zonage", "prescription", "information") and not attrs.get("libelle"):
        flags.append("libelle_vide")
    if layer.famille in ("zonage", "prescription", "information") and not attrs.get("idurba"):
        flags.append("idurba_absent")
    return flags


def aggregate(
    layer: LayerDef,
    hits: List[Hit],
    target: Target,
    s: ProfilerSettings,
    id_prefix: Optional[str] = None,
) -> List[Element]:
    """Fusionne les hits d'un même bucket par clé `group_by` -> liste d'Element triée."""
    groups: Dict[Tuple[str, ...], List[Hit]] = {}
    order: List[Tuple[str, ...]] = []
    for h in hits:
        k = tuple(_norm_val(h.feature.props.get(f)) for f in layer.group_by)
        # sans aucune valeur discriminante : on ne fusionne pas
        if not any(k):
            k = ("__fid__", h.feature.fid)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(h)

    elements: List[Element] = []
    for i, k in enumerate(order):
        hs = groups[k]
        first = hs[0].feature.props
        attrs = {f: _clean_attr(first.get(f)) for f in layer.fields}

        # Code + libellé officiel CNIG
        code = libelle_cnig = None
        flags = _quality_flags(layer, attrs)
        if layer.code_nomenclature and layer.code_fields:
            tf, sf = layer.code_fields
            code, libelle_cnig, cflags = lookup(layer.code_nomenclature, first.get(tf), first.get(sf) if sf else None)
            flags += cflags

        # Mesures sommées
        positions = {h.position for h in hs}
        position = "interieur" if "interieur" in positions else ("limite" if "limite" in positions else "voisinage")
        mesure = Mesure(position=position)
        dists = [h.distance_m for h in hs if h.distance_m is not None]
        if position != "interieur" and dists:
            mesure.distance_m = round(min(dists), 2)
        if layer.geom_kind == "surf":
            area = sum(h.surface_m2 or 0.0 for h in hs)
            if hs[0].bucket != "voisinage":
                mesure.surface_m2 = round(area, 1)
                mesure.pct_cible = round(min(area / target.area * 100.0, 100.0), 2) if target.area else None
        elif layer.geom_kind == "lin":
            if hs[0].bucket != "voisinage":
                mesure.longueur_m = round(sum(h.longueur_m or 0.0 for h in hs), 1)
        else:
            if hs[0].bucket != "voisinage":
                mesure.nb_points = sum(h.nb_points or 0 for h in hs)

        el = Element(
            element_id="",
            source_ids=[h.feature.fid for h in hs],
            nb_entites=len(hs),
            attributs=attrs,
            code=code,
            libelle_cnig=libelle_cnig,
            mesure=mesure,
            liens=_liens(attrs),
            flags=sorted(set(flags)),
            raison_ecart=hs[0].raison_ecart,
        )
        if s.include_raw:
            el.raw = [h.feature.props for h in hs]
        if s.include_geometries:
            geoms = [h.inter_geom for h in hs if h.inter_geom is not None and not h.inter_geom.is_empty]
            if geoms:
                el.geometry = to_geojson(unary_union(geoms))
        elements.append(el)

    def _sort_key(e: Element):
        m = e.mesure
        return -(m.pct_cible or 0.0), -(m.longueur_m or 0.0), -(m.nb_points or 0), (m.distance_m or 0.0)

    elements.sort(key=_sort_key)
    # ré-indexation après tri pour des ids lisibles
    prefix = id_prefix or layer.key
    for j, e in enumerate(elements):
        e.element_id = f"{prefix}:{j + 1}"
    return elements
