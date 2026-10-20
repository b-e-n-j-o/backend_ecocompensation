"""
service.py — Orchestration du profiler urba.

    run_urba_profile(request) -> UrbaProfilResponse

Déroulé :
1. Géométries des parcelles -> Lambert-93, réparées.
2. Grille de cellules (cluster_cell_m), fusion des emprises voisines sous cluster_max_side_m.
   1 requête WFS par cluster et par couche, jamais par parcelle.
3. Récupération des entités GPU (débit limité, cache, subdivision si > 5000),
   puis seconde passe sur les seuls échecs.
4. Pour chaque cible (parcelle, puis UF = union de ses parcelles) et chaque
   couche : mesure entité par entité (intersect.measure_feature) puis
   agrégation en éléments de rapport (intersect.aggregate).
5. Synthèse par cible (zonages, documents d'urbanisme, compteurs, flags).

Aucune écriture en base ici : l'appelant persiste le rapport et, s'il passe
`context_out`, les géométries découpées (parcelle + 100 m) pour la carte.
"""

from __future__ import annotations

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from shapely.ops import unary_union
from shapely.strtree import STRtree

from .config import LAYERS_BY_KEY, LayerDef, ProfilerSettings, default_layer_keys
from .geometry import from_geojson
from .intersect import Hit, Target, aggregate, measure_feature
from .nomenclature import lookup, parse_idurba
from .schemas import (
    Cible,
    Couche,
    DocumentUrba,
    RunMeta,
    Synthese,
    UrbaProfilRequest,
    UrbaProfilResponse,
    ZoneResume,
)
from .wfs_client import Feature, WfsClient, WfsError

log = logging.getLogger(__name__)

# Contexte carto : l'entité GPU est découpée sur l'emprise de la parcelle + ce tampon.
# On ne stocke jamais le polygone GPU complet (une zone peut couvrir une demi-commune).
CONTEXT_BUFFER_M = 100.0

ClusterKey = Tuple[int, int]
Bbox = Tuple[float, float, float, float]


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
    s = replace(base, **overrides)
    if s.layers is None:
        s = replace(s, layers=default_layer_keys())
    unknown = [k for k in s.layers if k not in LAYERS_BY_KEY]
    if unknown:
        raise ValueError(f"Couches inconnues : {unknown}. Disponibles : {list(LAYERS_BY_KEY)}")
    return s


# ---------------------------------------------------------------------------
# Clusters
# ---------------------------------------------------------------------------


@dataclass
class _Cluster:
    key: ClusterKey
    pids: List[str]
    bbox: Bbox


def _cluster_parcelles(geoms: Dict[str, object], cell: float) -> Dict[ClusterKey, List[str]]:
    clusters: Dict[ClusterKey, List[str]] = {}
    for pid, geom in geoms.items():
        c = geom.centroid
        key = (math.floor(c.x / cell), math.floor(c.y / cell))
        clusters.setdefault(key, []).append(pid)
    return clusters


def _cluster_bbox(pids: Sequence[str], geoms: Dict[str, object], margin: float) -> Bbox:
    bounds = [geoms[pid].bounds for pid in pids]
    return (
        min(b[0] for b in bounds) - margin,
        min(b[1] for b in bounds) - margin,
        max(b[2] for b in bounds) + margin,
        max(b[3] for b in bounds) + margin,
    )


def _boxes_touch(a: Bbox, b: Bbox) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def _side(box: Bbox) -> float:
    return max(box[2] - box[0], box[3] - box[1])


def _build_clusters(geoms: Dict[str, object], cell: float, max_side: float, margin: float) -> List[_Cluster]:
    """Cellules non vides, puis fusion des voisins dont l'emprise reste sous max_side."""
    raw = _cluster_parcelles(geoms, cell)
    clusters = [
        _Cluster(key=key, pids=pids, bbox=_cluster_bbox(pids, geoms, margin))
        for key, pids in raw.items()
    ]
    changed = True
    while changed:
        changed = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                if not _boxes_touch(clusters[i].bbox, clusters[j].bbox):
                    continue
                pids = clusters[i].pids + clusters[j].pids
                bbox = _cluster_bbox(pids, geoms, margin)
                if _side(bbox) > max_side:
                    continue
                clusters[i] = _Cluster(
                    key=min(clusters[i].key, clusters[j].key),
                    pids=pids,
                    bbox=bbox,
                )
                del clusters[j]
                changed = True
                break
            if changed:
                break
    clusters.sort(key=lambda c: c.key)
    return clusters


# ---------------------------------------------------------------------------
# Profil d'une cible
# ---------------------------------------------------------------------------


def _json_attr(value: object) -> object:
    if value is None or isinstance(value, (str, int, bool)):
        if isinstance(value, str) and len(value) > 800:
            return value[:800]
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    return str(value)[:800]


def _clip_context(feature_geom, target_geom):
    """Découpe l'entité sur la parcelle élargie de CONTEXT_BUFFER_M."""
    from shapely import make_valid

    buf = target_geom.buffer(CONTEXT_BUFFER_M)
    try:
        clip = feature_geom.intersection(buf)
    except Exception:
        clip = make_valid(feature_geom).intersection(buf)
    if clip is None or clip.is_empty:
        return None
    return clip


def _collect_context(
    context: List[dict],
    target: Target,
    layer: LayerDef,
    hits: List[Hit],
    cible_type: str,
) -> None:
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
    cluster_keys: set,
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

        cands: Dict[str, Feature] = {}
        for ck in cluster_keys:
            idx = indexes.get((layer.key, ck))
            if idx:
                for feat in idx.query(target.geom, search_dist):
                    cands.setdefault(feat.fid, feat)

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

    # 2. Clusters : une emprise par groupe de parcelles proches, pas par parcelle.
    margin = max(s.voisinage_m, s.edge_tolerance_m, s.anneau_zonage_m) + 10.0
    clusters = _build_clusters(geoms, s.cluster_cell_m, s.cluster_max_side_m, margin)
    parcel_cluster: Dict[str, ClusterKey] = {pid: cl.key for cl in clusters for pid in cl.pids}
    tasks = [(layer, cl) for layer in layers for cl in clusters]
    log.info(
        "%s parcelles -> %s clusters -> %s requêtes prévues",
        len(geoms),
        len(clusters),
        len(tasks),
    )

    # 3. Fetch WFS ---------------------------------------------------------------
    client = client or WfsClient(s)
    indexes: Dict[Tuple[str, ClusterKey], LayerIndex] = {}
    fetch_errors: Dict[Tuple[str, ClusterKey], str] = {}
    failed: List[dict] = []

    def _remember(layer: LayerDef, cl: _Cluster, exc: Exception) -> None:
        failure = exc.failure if isinstance(exc, WfsError) else None
        message = (failure.message if failure else str(exc))[:300]
        fetch_errors[(layer.key, cl.key)] = message
        failed.append(
            {
                "layer_key": layer.key,
                "cluster": [cl.key[0], cl.key[1]],
                "type": failure.type if failure else "http",
                "status": failure.status if failure else None,
                "message": message,
            }
        )
        log.warning("WFS %s cluster %s : %s", layer.typename, cl.key, message)

    def _run_one(index: int, total: int, layer: LayerDef, cl: _Cluster) -> None:
        try:
            feats = client.get_features(layer.typename, cl.bbox)
        except Exception as exc:
            _remember(layer, cl, exc)
            return
        indexes[(layer.key, cl.key)] = LayerIndex(feats)
        fetch_errors.pop((layer.key, cl.key), None)
        log.info(
            "[%s/%s] %s cluster %s : %s entités",
            index,
            total,
            layer.typename,
            cl.key,
            len(feats),
        )

    def _run_pass(pending: List[Tuple[LayerDef, _Cluster]], breaker: bool) -> None:
        streak: Dict[str, int] = {}
        total = len(tasks)
        done = len(tasks) - len(pending)
        if s.max_workers > 1 and not breaker:
            with ThreadPoolExecutor(max_workers=s.max_workers) as ex:
                futs = {
                    ex.submit(_run_one, done + i + 1, total, layer, cl): (layer, cl)
                    for i, (layer, cl) in enumerate(pending)
                }
                for fut in as_completed(futs):
                    fut.result()
            return
        for i, (layer, cl) in enumerate(pending):
            if breaker and streak.get(layer.key, 0) >= 3:
                log.warning("couche %s suspendue %ss", layer.key, s.layer_cooldown_s)
                time.sleep(s.layer_cooldown_s)
                streak[layer.key] = 0
            before = (layer.key, cl.key) in fetch_errors
            _run_one(done + i + 1, total, layer, cl)
            if (layer.key, cl.key) in fetch_errors:
                streak[layer.key] = streak.get(layer.key, 0) + 1
            else:
                streak[layer.key] = 0
            if before and (layer.key, cl.key) not in fetch_errors:
                failed[:] = [f for f in failed if not (f["layer_key"] == layer.key and f["cluster"] == [cl.key[0], cl.key[1]])]

    _run_pass(tasks, breaker=s.max_workers <= 1)
    if failed:
        log.info("seconde passe dans %ss sur %s tâche(s)", s.retry_pass_wait_s, len(failed))
        time.sleep(s.retry_pass_wait_s)
        saved_interval = getattr(getattr(client, "s", None), "min_interval_s", None)
        if saved_interval is not None:
            client.s.min_interval_s = s.second_pass_interval_s
        retry = [
            (layer, cl)
            for layer, cl in tasks
            if (layer.key, cl.key) in fetch_errors
        ]
        failed.clear()
        for key in list(fetch_errors):
            if key in {(layer.key, cl.key) for layer, cl in retry}:
                fetch_errors.pop(key, None)
        try:
            _run_pass(retry, breaker=True)
        finally:
            if saved_interval is not None:
                client.s.min_interval_s = saved_interval
    if failed:
        errors.extend(
            f"WFS {item['layer_key']} cluster {tuple(item['cluster'])} : {item['message']}"
            for item in failed
        )

    # 4. Profils parcelles ---------------------------------------------------------
    out_parcelles: Dict[str, Cible] = {}
    for pid, g in geoms.items():
        t = Target.build(pid, g, s.edge_tolerance_m)
        couches = _profile_target(
            t, {parcel_cluster[pid]}, layers, indexes, fetch_errors, s, context_out, "parcelle"
        )
        syn, flags = _synthese_and_flags(couches)
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
        if g.geom_type == "MultiPolygon":
            flags.append("uf_non_contigue")
        if len(pids) < len(uf.parcelle_ids):
            flags.append("uf_incomplete")
        out_ufs[uf.id] = Cible(
            id=uf.id, type="unite_fonciere", surface_m2=round(t.area, 1), parcelle_ids=pids,
            documents=_documents(couches), synthese=syn, couches=couches, flags=flags,
        )

    stats = client.stats
    meta = RunMeta(
        started_at=started,
        duration_s=round(time.time() - t0, 2),
        nb_parcelles=len(out_parcelles),
        nb_unites_foncieres=len(out_ufs),
        nb_clusters=len(clusters),
        layers=s.layers,
        wfs_requests_planned=len(tasks),
        wfs_requests=getattr(stats, "requests", 0),
        wfs_cache_hits=getattr(stats, "cache_hits", 0),
        wfs_truncated_tiles=getattr(stats, "truncated_tiles", 0),
        wfs_subdivided_tiles=getattr(stats, "subdivided_tiles", 0),
        wfs_retries=getattr(stats, "retries", 0),
        failed_tasks=failed,
        errors=errors,
        settings={k: v for k, v in asdict(s).items() if k not in ("layers",)},
    )
    return UrbaProfilResponse(parcelles=out_parcelles, unites_foncieres=out_ufs, meta=meta)
