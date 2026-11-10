"""
uf_pocket.py — Sous-UF contiguës dans les gros blocs d'une même personne morale.

Principe : au lieu de « k parmi N » sur tout le bloc, on
  1. évalue chaque critère UNE fois par parcelle (exclusions, faune, occ. du sol, surface),
  2. retire du graphe les parcelles exclues (si une parcelle touche une exclusion,
     toute union qui la contient la touche aussi -> filtre exact, pas une heuristique),
  3. garde la « poche » : parcelles à <= kmax-1 sauts d'une parcelle ancre (proche faune)
     dans le graphe des parcelles éligibles,
  4. énumère les sous-ensembles connexes 2..kmax (algo ESU : chaque sous-ensemble
     produit une seule fois, jamais de combinaisons non connexes testées),
     avec élagage distance-à-l'ancre et surface max atteignable,
  5. agrège les critères par sommes/min sur les parcelles (pas de ST_Union par candidat),
  6. s'arrête à un quota et le signale (truncated=True).

Usage CLI :
  python uf_pocket.py --geojson bloc_351397096.geojson --kmax 5 --min-area 50000 -v
  python uf_pocket.py --synthetic 127 --kmax 5 --min-area 20000 -v   # bench
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterator

log = logging.getLogger("uf_pocket")


# --------------------------------------------------------------------------- modèle
@dataclass(frozen=True)
class Parcel:
    idu: str
    area: float                # m² (cadastre = partition -> surfaces additives)
    excluded: bool = False     # intersecte projet / GEOMCE / ENS / préemption ENS / N2000...
    anchor: bool = False       # à <= R d'une observation faune retenue
    occ_ok_area: float = 0.0   # m² de la parcelle dans les classes d'occupation acceptées


@dataclass
class Candidate:
    idus: tuple[str, ...]
    area: float
    occ_ok_area: float
    n_anchor: int

    @property
    def occ_ratio(self) -> float:
        return self.occ_ok_area / self.area if self.area else 0.0


@dataclass
class BlockResult:
    block_size: int
    n_excluded: int
    n_anchor: int
    pocket: list[str]
    pocket_components: list[list[str]]   # « UF globale » par composante (restitution 1 ligne / UF)
    candidates: list[Candidate] = field(default_factory=list)
    n_explored: int = 0
    truncated: bool = False
    seconds: float = 0.0


Adjacency = dict[str, set[str]]


# --------------------------------------------------------------------------- graphe
def _bfs_dist(adj: Adjacency, sources: set[str], allowed: set[str], max_depth: int) -> dict[str, int]:
    dist = {s: 0 for s in sources if s in allowed}
    q = deque(dist)
    while q:
        u = q.popleft()
        if dist[u] >= max_depth:
            continue
        for v in adj.get(u, ()):
            if v in allowed and v not in dist:
                dist[v] = dist[u] + 1
                q.append(v)
    return dist


def _components(adj: Adjacency, nodes: set[str]) -> list[list[str]]:
    seen, comps = set(), []
    for n in sorted(nodes):
        if n in seen:
            continue
        comp, q = [], deque([n])
        seen.add(n)
        while q:
            u = q.popleft()
            comp.append(u)
            for v in adj.get(u, ()):
                if v in nodes and v not in seen:
                    seen.add(v)
                    q.append(v)
        comps.append(sorted(comp))
    return comps


# --------------------------------------------------------------------------- énumération
def iter_connected_subsets(
    adj: Adjacency,
    nodes: list[str],
    kmin: int,
    kmax: int,
    prune: Callable[[frozenset[str], int], bool] | None = None,
) -> Iterator[tuple[frozenset[str], bool]]:
    """ESU (Wernicke 2006) étendu aux tailles kmin..kmax.

    Chaque sous-ensemble connexe est produit exactement une fois (racine = plus petit
    index). `prune(S, k)` -> True pour couper la branche (aucune extension utile).
    Yield (S, explored_flag) ; seuls les S de taille >= kmin sont émis.
    """
    idx = {n: i for i, n in enumerate(nodes)}
    node_set = set(nodes)

    def extend(sub: frozenset[str], ext: set[str], root_i: int, nbh: set[str]):
        if len(sub) >= kmin:
            yield sub
        if len(sub) == kmax:
            return
        ext = set(ext)
        while ext:
            w = ext.pop()
            new_sub = sub | {w}
            if prune and prune(new_sub, kmax):
                continue
            # voisins exclusifs de w : index > racine, pas dans sub ni dans son voisinage
            excl = {
                u for u in adj.get(w, ())
                if u in node_set and idx[u] > root_i and u not in sub and u not in nbh
            }
            yield from extend(new_sub, ext | excl, root_i, nbh | {w} | set(adj.get(w, ())))

    for v in nodes:
        vi = idx[v]
        start = frozenset([v])
        if prune and prune(start, kmax):
            # même si la racine seule est « sans espoir » pour l'ancre, elle peut l'être
            # grâce à ses extensions : on ne coupe ici que sur le critère surface.
            pass
        ext = {u for u in adj.get(v, ()) if u in node_set and idx[u] > vi}
        nbh = {v} | set(adj.get(v, ()))
        yield from extend(start, ext, vi, nbh)


def search_block(
    parcels: dict[str, Parcel],
    adj: Adjacency,
    *,
    kmin: int = 2,
    kmax: int = 5,
    min_area: float = 0.0,
    min_occ_ratio: float = 0.0,
    quota: int = 20_000,
) -> BlockResult:
    t0 = time.perf_counter()
    all_ids = set(parcels)
    eligible = {i for i, p in parcels.items() if not p.excluded}
    anchors = {i for i in eligible if parcels[i].anchor}
    log.info("bloc: %d parcelles | %d exclues | %d ancres faune",
             len(all_ids), len(all_ids) - len(eligible), len(anchors))

    # distance (en sauts) à l'ancre la plus proche, dans le graphe des éligibles
    dist = _bfs_dist(adj, anchors, eligible, kmax - 1)
    pocket = set(dist)
    comps = _components(adj, pocket)
    log.info("poche: %d parcelles en %d composante(s) (rayon %d sauts)",
             len(pocket), len(comps), kmax - 1)

    res = BlockResult(
        block_size=len(all_ids), n_excluded=len(all_ids) - len(eligible),
        n_anchor=len(anchors), pocket=sorted(pocket), pocket_components=comps,
    )
    if not anchors:
        res.seconds = time.perf_counter() - t0
        return res

    max_area = max(parcels[i].area for i in pocket)

    def prune(S: frozenset[str], k: int) -> bool:
        free = k - len(S)
        # il faut encore pouvoir atteindre une ancre
        if min(dist[u] for u in S) > free:
            return True
        # et la surface minimale
        if sum(parcels[u].area for u in S) + free * max_area < min_area:
            return True
        return False

    # ordre : ancres d'abord, puis par distance -> les candidats les plus pertinents
    # sortent avant le quota
    order = sorted(pocket, key=lambda i: (dist[i], i))
    for S in iter_connected_subsets(adj, order, kmin, kmax, prune):
        res.n_explored += 1
        area = sum(parcels[u].area for u in S)
        if area < min_area:
            continue
        n_anchor = sum(1 for u in S if u in anchors)
        if n_anchor == 0:
            continue
        occ = sum(parcels[u].occ_ok_area for u in S)
        if area and occ / area < min_occ_ratio:
            continue
        res.candidates.append(Candidate(tuple(sorted(S)), area, occ, n_anchor))
        if len(res.candidates) >= quota:
            res.truncated = True
            log.warning("quota %d atteint -> arrêt, bloc marqué truncated", quota)
            break

    res.seconds = time.perf_counter() - t0
    log.info("explorés: %d | candidats: %d | truncated=%s | %.3fs",
             res.n_explored, len(res.candidates), res.truncated, res.seconds)
    return res


# --------------------------------------------------------------------------- utilitaires I/O
def adjacency_from_geojson(features: list[dict], tol: float = 0.5) -> Adjacency:
    """Contiguïté avec tolérance (m, géométries en projection métrique, ex. 2154).

    Le cadastre Etalab n'est pas topologiquement propre : ST_Touches strict rate des
    voisins. Un contact ponctuel (coin) est exclu : on exige une longueur partagée > tol.
    """
    from shapely.geometry import shape
    from shapely.strtree import STRtree

    ids = [f["properties"]["idu"] for f in features]
    geoms = [shape(f["geometry"]) for f in features]
    tree = STRtree(geoms)
    adj: Adjacency = {i: set() for i in ids}
    for a, g in enumerate(geoms):
        for b in tree.query(g.buffer(tol)):
            b = int(b)
            if b <= a:
                continue
            inter = g.buffer(tol).intersection(geoms[b].buffer(tol))
            # longueur de frontière partagée ~ surface d'intersection / (2*tol)
            if not inter.is_empty and inter.area / (2 * tol) > 2 * tol:
                adj[ids[a]].add(ids[b])
                adj[ids[b]].add(ids[a])
    return adj


def parcels_from_geojson(features: list[dict]) -> dict[str, Parcel]:
    out = {}
    for f in features:
        p = f["properties"]
        out[p["idu"]] = Parcel(
            idu=p["idu"],
            area=float(p["area"]),
            excluded=bool(p.get("excluded", False)),
            anchor=bool(p.get("anchor", False)),
            occ_ok_area=float(p.get("occ_ok_area", 0.0)),
        )
    return out


def synthetic_block(n: int, seed: int = 0, king: bool = False) -> tuple[dict[str, Parcel], Adjacency]:
    """Bloc ~grille irrégulière de n parcelles (pour bench / tests)."""
    rng = random.Random(seed)
    side = int(n ** 0.5) + 1
    cells = [(r, c) for r in range(side) for c in range(side)][:n]
    ids = {cell: f"P{k:04d}" for k, cell in enumerate(cells)}
    steps = [(0, 1), (1, 0), (0, -1), (-1, 0)]
    if king:
        steps += [(1, 1), (1, -1), (-1, 1), (-1, -1)]
    adj: Adjacency = {i: set() for i in ids.values()}
    for (r, c), i in ids.items():
        for dr, dc in steps:
            j = ids.get((r + dr, c + dc))
            if j:
                adj[i].add(j)
    parcels = {
        i: Parcel(i, area=rng.uniform(5_000, 40_000), excluded=rng.random() < 0.15,
                  anchor=rng.random() < 0.05, occ_ok_area=0.0)
        for i in ids.values()
    }
    parcels = {i: Parcel(p.idu, p.area, p.excluded, p.anchor, p.area * rng.uniform(0.3, 1.0))
               for i, p in parcels.items()}
    return parcels, adj


def count_all_connected(adj: Adjacency, kmin: int, kmax: int) -> int:
    return sum(1 for _ in iter_connected_subsets(adj, sorted(adj), kmin, kmax))


# --------------------------------------------------------------------------- CLI
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--geojson", help="FeatureCollection du bloc (props: idu, area, excluded, anchor, occ_ok_area)")
    src.add_argument("--synthetic", type=int, help="taille d'un bloc synthétique")
    ap.add_argument("--king", action="store_true", help="synthétique : contiguïté 8-voisins")
    ap.add_argument("--kmax", type=int, default=5)
    ap.add_argument("--min-area", type=float, default=0.0)
    ap.add_argument("--min-occ-ratio", type=float, default=0.0)
    ap.add_argument("--quota", type=int, default=20_000)
    ap.add_argument("--tol", type=float, default=0.5, help="tolérance contiguïté (m)")
    ap.add_argument("--count-all", action="store_true", help="compter aussi TOUS les sous-ensembles connexes du bloc")
    ap.add_argument("--expect", nargs="*", help="idus attendus dans au moins un candidat")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if a.geojson:
        feats = json.load(open(a.geojson))["features"]
        parcels = parcels_from_geojson(feats)
        adj = adjacency_from_geojson(feats, a.tol)
        log.info("contiguïté: %d arêtes", sum(len(v) for v in adj.values()) // 2)
    else:
        parcels, adj = synthetic_block(a.synthetic, king=a.king)

    if a.count_all:
        t = time.perf_counter()
        n = count_all_connected(adj, 2, a.kmax)
        log.info("TOUT le bloc : %d sous-ensembles connexes 2..%d (%.2fs)", n, a.kmax, time.perf_counter() - t)

    res = search_block(parcels, adj, kmax=a.kmax, min_area=a.min_area,
                       min_occ_ratio=a.min_occ_ratio, quota=a.quota)
    for c in sorted(res.candidates, key=lambda c: (-c.n_anchor, -c.area))[:10]:
        log.debug("  %s  %.2f ha  occ=%.0f%%  ancres=%d", ",".join(c.idus), c.area / 1e4,
                  100 * c.occ_ratio, c.n_anchor)
    if a.expect:
        want = set(a.expect)
        hit = [c for c in res.candidates if want <= set(c.idus)]
        log.info("attendu %s -> %s", sorted(want), "TROUVÉ" if hit else "ABSENT")


if __name__ == "__main__":
    main()