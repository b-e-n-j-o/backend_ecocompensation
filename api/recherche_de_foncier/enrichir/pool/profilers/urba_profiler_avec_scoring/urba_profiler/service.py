"""
service.py — Orchestration du profiler urba.

    run_urba_profile(request) -> UrbaProfilResponse

Déroulé :
1. Géométries des parcelles -> Lambert-93, réparées.
2. Regroupement spatial des parcelles en "clusters" (grille de cluster_cell_m) :
   1 requête WFS par couche et par cluster, au lieu d'1 par parcelle.
3. Récupération des entités GPU (threads, cache, subdivision si > 5000).
4. Pour chaque cible (parcelle, puis UF = union de ses parcelles) et chaque
   couche : mesure entité par entité (intersect.measure_feature) puis
   agrégation en éléments de rapport (intersect.aggregate).
5. Synthèse par cible (zonages, documents d'urbanisme, compteurs, flags).

Aucune écriture en base : le résultat est renvoyé, au front / appelant de le
persister si besoin.
"""

from __future__ import annotations

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Set, Tuple

from shapely.geometry import box
from shapely.ops import unary_union
from shapely.strtree import STRtree

from .config import LAYERS_BY_KEY, LayerDef, ProfilerSettings, default_layer_keys
from .geometry import from_geojson
from .intersect import Hit, Target, aggregate, measure_feature
from .nomenclature import lookup, parse_idurba, typezone_simple
from .schemas import (
    Cible,
    Couche,
    DocumentUrba,
    AnneauZonage,
    RunMeta,
    Synthese,
    UrbaProfilRequest,
    UrbaProfilResponse,
    ZoneResume,
)
from .wfs_client import Feature, WfsClient

log = logging.getLogger(__name__)

ClusterKey = Tuple[int, int]
CONTEXT_BUFFER_M = 100.0


class LayerIndex:
    """Entités d'une couche pour un cluster + index spatial."""

    def __init__(self, features: List[Feature]):
        self.features = features
        self.tree = STRtree([f.geom for f in features]) if features else None

    def query(self, geom, distance: float) -> List[Feature]:
        if self.tree is None:
            return []
        g = geom.buffer(distance) if distance > 0 else geom
        return [self.features[i] for i in self.tree.query(g)]


# ---------------------------------------------------------------------------
# Paramètres
# ---------------------------------------------------------------------------


def _merge_settings(base: ProfilerSettings, req: UrbaProfilRequest) -> ProfilerSettings:
    o = req.options
    overrides = {k: v for k, v in o.model_dump().items() if v is not None and hasattr(base, k)}
    if "scoring_profil" in o.model_fields_set:  # null explicite = pas de scoring
        overrides["scoring_profil"] = o.scoring_profil
    s = replace(base, **overrides)
    if s.layers is None:
        s = replace(s, layers=default_layer_keys())
    unknown = [k for k in s.layers if k not in LAYERS_BY_KEY]
    if unknown:
        raise ValueError(f"Couches inconnues : {unknown}. Disponibles : {list(LAYERS_BY_KEY)}")
    if s.scoring_profil is not None:
        from .scoring.loader import get_rules

        profils = get_rules().profils
        if s.scoring_profil not in profils:
            raise ValueError(f"Profil de scoring inconnu : {s.scoring_profil!r}. Disponibles : {list(profils)}")
    return s


# ---------------------------------------------------------------------------
# Clusters
# ---------------------------------------------------------------------------


def _cluster_parcelles(geoms: Dict[str, object], cell: float) -> Dict[ClusterKey, List[str]]:
    clusters: Dict[ClusterKey, List[str]] = {}
    for pid, g in geoms.items():
        c = g.centroid
        k = (math.floor(c.x / cell), math.floor(c.y / cell))
        clusters.setdefault(k, []).append(pid)
    return clusters


def _cluster_bbox(pids: Sequence[str], geoms: Dict[str, object], margin: float):
    b = unary_union([geoms[p].envelope for p in pids]).bounds
    return (b[0] - margin, b[1] - margin, b[2] + margin, b[3] + margin)


# ---------------------------------------------------------------------------
# Profil d'une cible
# ---------------------------------------------------------------------------


def _json_attr(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value[:800]
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    return str(value)[:800]


def _clip_context(feature_geom, target_geom):
    from shapely import make_valid

    buf = target_geom.buffer(CONTEXT_BUFFER_M)
    try:
        clip = feature_geom.intersection(buf)
    except Exception:
        clip = make_valid(feature_geom).intersection(buf)
    if clip is None or clip.is_empty:
        return None
    return clip


def _collect_context(context, target, layer, hits, cible_type: str) -> None:
    for h in hits:
        g = h.feature.geom
        if g is None or g.is_empty:
            continue
        clip = _clip_context(g, target.geom)
        if clip is None:
            continue
        props = h.feature.props
        code = libelle_cnig = None
        flags: List[str] = []
        if layer.code_nomenclature and layer.code_fields:
            tf, sf = layer.code_fields
            code, libelle_cnig, flags = lookup(
                layer.code_nomenclature,
                props.get(tf),
                props.get(sf) if sf else None,
            )
        pct = None
        if layer.geom_kind == "surf" and h.bucket != "voisinage" and h.surface_m2 and target.area:
            pct = round(min(h.surface_m2 / target.area * 100.0, 100.0), 2)
        context.append(
            {
                "cible_id": target.id,
                "cible_type": cible_type,
                "layer_key": layer.key,
                "display_name": layer.display_name,
                "famille": layer.famille,
                "geom_kind": layer.geom_kind,
                "fid": h.feature.fid,
                "bucket": h.bucket,
                "position": h.position,
                "surface_m2": None if h.surface_m2 is None else round(h.surface_m2, 1),
                "pct_cible": pct,
                "longueur_m": None if h.longueur_m is None else round(h.longueur_m, 1),
                "nb_points": h.nb_points,
                "distance_m": h.distance_m,
                "code": code,
                "libelle_cnig": libelle_cnig,
                "libelle": _json_attr(props.get("libelle") or props.get("nomsuplitt") or props.get("txt")),
                "attributs": {f: _json_attr(props.get(f)) for f in layer.fields},
                "flags": list(flags),
                "clip": clip,
            }
        )


def _profile_target(
    target: Target,
    cluster_keys: Set[ClusterKey],
    layers: List[LayerDef],
    indexes: Dict[Tuple[str, ClusterKey], LayerIndex],
    fetch_errors: Dict[Tuple[str, ClusterKey], str],
    s: ProfilerSettings,
    context: Optional[List[dict]] = None,
    cible_type: str = "parcelle",
) -> List[Couche]:
    search_dist = max(s.voisinage_m, s.edge_tolerance_m)
    couches: List[Couche] = []
    for layer in layers:
        couche = Couche(
            layer_key=layer.key,
            display_name=layer.display_name,
            famille=layer.famille,
            geom_kind=layer.geom_kind,
            typename=layer.typename,
            status="not_intersected",
            colonnes=dict(layer.fields),
        )
        errs = [fetch_errors[(layer.key, ck)] for ck in cluster_keys if (layer.key, ck) in fetch_errors]
        if errs:
            couche.status = "error"
            couche.error = errs[0]
            couches.append(couche)
            continue

        # candidats (dédupliqués si l'UF chevauche plusieurs clusters)
        cands: Dict[str, Feature] = {}
        for ck in cluster_keys:
            idx = indexes.get((layer.key, ck))
            if idx:
                for f in idx.query(target.geom, search_dist):
                    cands.setdefault(f.fid, f)

        retenus: List[Hit] = []
        ecartes: List[Hit] = []
        voisins: List[Hit] = []
        for f in cands.values():
            h = measure_feature(target, f, layer.geom_kind, s)
            if h is None:
                continue
            {"retenu": retenus, "ecarte": ecartes, "voisinage": voisins}[h.bucket].append(h)

        couche.elements = aggregate(layer, retenus, target, s)
        if s.include_ecartes:
            couche.ecartes = aggregate(layer, ecartes, target, s, id_prefix=f"{layer.key}:ecarte")
        couche.voisinage = aggregate(layer, voisins, target, s, id_prefix=f"{layer.key}:voisinage")
        couche.elements_count = len(couche.elements)
        if couche.elements:
            couche.status = "intersected"
        if context is not None:
            kept = list(retenus) + list(voisins)
            if s.include_ecartes:
                kept.extend(ecartes)
            _collect_context(context, target, layer, kept, cible_type)
        couches.append(couche)
    return couches


def _anneau_zonage(
    target: Target,
    cluster_keys: Set[ClusterKey],
    layers: List[LayerDef],
    indexes: Dict[Tuple[str, ClusterKey], LayerIndex],
    fetch_errors: Dict[Tuple[str, ClusterKey], str],
    rayon: float,
) -> Optional[AnneauZonage]:
    """Part de chaque type de zone dans l'anneau [cible ; cible + rayon]."""
    zl = [l for l in layers if l.famille == "zonage"]
    if rayon <= 0 or not zl:
        return None
    if any((l.key, ck) in fetch_errors for l in zl for ck in cluster_keys):
        return None
    ring = target.geom.buffer(rayon).difference(target.geom)
    area = ring.area
    if area <= 0:
        return None
    acc: Dict[str, float] = {}
    for layer in zl:
        seen: Set[str] = set()
        for ck in cluster_keys:
            idx = indexes.get((layer.key, ck))
            if not idx:
                continue
            for f in idx.query(target.geom, rayon):
                if f.fid in seen:
                    continue
                seen.add(f.fid)
                a = ring.intersection(f.geom).area
                if a > 0:
                    k = typezone_simple(f.props, layer.key)
                    acc[k] = acc.get(k, 0.0) + a
    pct = {k: round(v / area * 100.0, 1) for k, v in sorted(acc.items(), key=lambda kv: -kv[1])}
    pct["non_couvert"] = round(max(0.0, 100.0 - sum(v for k, v in pct.items())), 1)
    return AnneauZonage(rayon_m=rayon, surface_m2=round(area, 1), pct=pct)


def _documents(couches: List[Couche]) -> List[DocumentUrba]:
    docs: Dict[str, DocumentUrba] = {}
    for c in couches:
        if c.famille not in ("zonage", "prescription", "information"):
            continue
        for e in c.elements:
            a = e.attributs
            idurba = a.get("idurba")
            key = idurba or a.get("partition") or "?"
            if key not in docs:
                p = parse_idurba(idurba)
                docs[key] = DocumentUrba(
                    idurba=idurba,
                    partition=a.get("partition"),
                    typedoc=p["typedoc"],
                    date=p["date"],
                    insee=a.get("insee"),
                )
            d = docs[key]
            if c.famille == "zonage":
                d.datappro = d.datappro or a.get("datappro")
                url = a.get("urlfic")
                if url and url not in d.urls_reglement:
                    d.urls_reglement.append(url)
    return list(docs.values())


def _synthese_and_flags(couches: List[Couche]) -> Tuple[Synthese, List[str]]:
    syn = Synthese()
    flags: List[str] = []
    for c in couches:
        if c.status == "error":
            syn.couches_en_erreur.append(c.layer_key)
            continue
        if c.elements:
            syn.couches_intersectees.append(c.layer_key)
            syn.nb_elements_par_famille[c.famille] = syn.nb_elements_par_famille.get(c.famille, 0) + len(c.elements)
        if c.famille == "zonage":
            for e in c.elements:
                syn.zonages.append(
                    ZoneResume(
                        layer_key=c.layer_key,
                        libelle=e.attributs.get("libelle"),
                        libelong=e.attributs.get("libelong"),
                        type=e.code,
                        pct_cible=e.mesure.pct_cible,
                        surface_m2=e.mesure.surface_m2,
                    )
                )
    zonage_layers = [c for c in couches if c.famille == "zonage"]
    if zonage_layers and all(c.status != "error" for c in zonage_layers) and not syn.zonages:
        # RNU, document non versé au GPU, ou parcelle hors document
        flags.append("aucun_zonage_gpu")
    if len(syn.zonages) > 1:
        flags.append("zonage_multiple")
    if syn.couches_en_erreur:
        flags.append("couches_en_erreur")
    syn.zonages.sort(key=lambda z: -(z.pct_cible or 0))
    return syn, flags


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------


def run_urba_profile(
    request: UrbaProfilRequest,
    settings: Optional[ProfilerSettings] = None,
    client: Optional[WfsClient] = None,
    context_out: Optional[List[dict]] = None,
) -> UrbaProfilResponse:
    t0 = time.time()
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    s = _merge_settings(settings or ProfilerSettings(), request)
    layers = [LAYERS_BY_KEY[k] for k in s.layers]

    if len(request.parcelles) > s.max_parcelles:
        raise ValueError(f"{len(request.parcelles)} parcelles > max_parcelles={s.max_parcelles}")

    # 1. Géométries -------------------------------------------------------------
    geoms: Dict[str, object] = {}
    metas: Dict[str, dict] = {}
    errors: List[str] = []
    for p in request.parcelles:
        if p.id in geoms:
            continue
        try:
            g = from_geojson(p.geometry, p.srid)
            if g.is_empty or g.area <= 0:
                raise ValueError("géométrie vide ou sans surface")
            geoms[p.id] = g
            metas[p.id] = p.meta
        except Exception as e:
            errors.append(f"parcelle {p.id} ignorée : {e}")
    if not geoms:
        raise ValueError("Aucune géométrie de parcelle exploitable. " + "; ".join(errors))

    for uf in request.unites_foncieres:
        missing = [pid for pid in uf.parcelle_ids if pid not in geoms]
        if missing:
            errors.append(f"UF {uf.id} : parcelles absentes/invalides {missing}")

    # 2. Clusters ----------------------------------------------------------------
    clusters = _cluster_parcelles(geoms, s.cluster_cell_m)
    parcel_cluster: Dict[str, ClusterKey] = {pid: ck for ck, pids in clusters.items() for pid in pids}
    margin = max(s.voisinage_m, s.edge_tolerance_m, s.anneau_zonage_m) + 10.0

    # 3. Fetch WFS ---------------------------------------------------------------
    client = client or WfsClient(s)
    indexes: Dict[Tuple[str, ClusterKey], LayerIndex] = {}
    fetch_errors: Dict[Tuple[str, ClusterKey], str] = {}
    tasks = [(layer, ck, _cluster_bbox(pids, geoms, margin)) for ck, pids in clusters.items() for layer in layers]

    def _job(layer: LayerDef, ck: ClusterKey, bbox):
        return layer.key, ck, client.get_features(layer.typename, bbox)

    with ThreadPoolExecutor(max_workers=max(1, s.max_workers)) as ex:
        futs = {ex.submit(_job, layer, ck, bbox): (layer.key, ck) for layer, ck, bbox in tasks}
        for fut in as_completed(futs):
            key = futs[fut]
            try:
                lk, ck, feats = fut.result()
                indexes[(lk, ck)] = LayerIndex(feats)
            except Exception as e:
                msg = str(e)[:300]
                fetch_errors[key] = msg
                log.warning("WFS %s cluster %s : %s", key[0], key[1], msg)
    if fetch_errors:
        errors += sorted({f"WFS {k[0]} : {v}" for k, v in fetch_errors.items()})

    # 4. Profils parcelles ---------------------------------------------------------
    out_parcelles: Dict[str, Cible] = {}
    for pid, g in geoms.items():
        t = Target.build(pid, g, s.edge_tolerance_m)
        couches = _profile_target(
            t, {parcel_cluster[pid]}, layers, indexes, fetch_errors, s, context_out, "parcelle"
        )
        syn, flags = _synthese_and_flags(couches)
        syn.anneau_zonage = _anneau_zonage(t, {parcel_cluster[pid]}, layers, indexes, fetch_errors, s.anneau_zonage_m)
        out_parcelles[pid] = Cible(
            id=pid, type="parcelle", surface_m2=round(t.area, 1), meta=metas.get(pid, {}),
            documents=_documents(couches), synthese=syn, couches=couches, flags=flags,
        )

    # 5. Profils unités foncières (union des parcelles) ---------------------------
    out_ufs: Dict[str, Cible] = {}
    for uf in request.unites_foncieres:
        pids = [pid for pid in uf.parcelle_ids if pid in geoms]
        if not pids:
            continue
        g = unary_union([geoms[pid] for pid in pids])
        t = Target.build(uf.id, g, s.edge_tolerance_m)
        cks = {parcel_cluster[pid] for pid in pids}
        couches = _profile_target(t, cks, layers, indexes, fetch_errors, s)
        syn, flags = _synthese_and_flags(couches)
        syn.anneau_zonage = _anneau_zonage(t, cks, layers, indexes, fetch_errors, s.anneau_zonage_m)
        if g.geom_type == "MultiPolygon":
            flags.append("uf_non_contigue")
        if len(pids) < len(uf.parcelle_ids):
            flags.append("uf_incomplete")
        out_ufs[uf.id] = Cible(
            id=uf.id, type="unite_fonciere", surface_m2=round(t.area, 1), parcelle_ids=pids,
            documents=_documents(couches), synthese=syn, couches=couches, flags=flags,
        )

    # 6. Scoring urbanistique ------------------------------------------------------
    if s.scoring_profil is not None:
        from .scoring.engine import score_cible

        for c in list(out_parcelles.values()) + list(out_ufs.values()):
            try:
                c.score = score_cible(c, profil=s.scoring_profil)
            except Exception as e:  # le scoring ne doit jamais faire échouer le profil
                log.exception("scoring %s", c.id)
                errors.append(f"scoring {c.id} : {e}")

    stats = client.stats
    meta = RunMeta(
        started_at=started,
        duration_s=round(time.time() - t0, 2),
        nb_parcelles=len(out_parcelles),
        nb_unites_foncieres=len(out_ufs),
        nb_clusters=len(clusters),
        layers=s.layers,
        wfs_requests=stats.requests,
        wfs_cache_hits=stats.cache_hits,
        wfs_truncated_tiles=stats.truncated_tiles,
        errors=errors,
        settings={k: v for k, v in asdict(s).items() if k not in ("layers",)},
    )
    return UrbaProfilResponse(parcelles=out_parcelles, unites_foncieres=out_ufs, meta=meta)
