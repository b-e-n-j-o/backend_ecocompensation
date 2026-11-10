import itertools
import json
import os

import pytest

from uf_pocket import (
    Parcel, count_all_connected, iter_connected_subsets, search_block, synthetic_block,
    parcels_from_geojson, adjacency_from_geojson,
)


def _connected(S, adj):
    S = set(S)
    start = next(iter(S))
    seen, stack = {start}, [start]
    while stack:
        u = stack.pop()
        for v in adj[u]:
            if v in S and v not in seen:
                seen.add(v)
                stack.append(v)
    return seen == S


@pytest.mark.parametrize("king", [False, True])
def test_esu_matches_bruteforce(king):
    _, adj = synthetic_block(20, seed=1, king=king)
    nodes = sorted(adj)
    brute = {frozenset(c) for k in range(2, 5) for c in itertools.combinations(nodes, k)
             if _connected(c, adj)}
    esu = list(iter_connected_subsets(adj, nodes, 2, 4))
    assert len(esu) == len(set(esu))          # aucun doublon
    assert set(esu) == brute


def test_pruned_search_matches_bruteforce_filter():
    parcels, adj = synthetic_block(30, seed=3, king=True)
    min_area = 60_000
    res = search_block(parcels, adj, kmax=4, min_area=min_area, quota=10**9)
    eligible = {i for i, p in parcels.items() if not p.excluded}
    brute = set()
    for k in range(2, 5):
        for c in itertools.combinations(sorted(eligible), k):
            if (_connected(c, adj) and any(parcels[i].anchor for i in c)
                    and sum(parcels[i].area for i in c) >= min_area):
                brute.add(tuple(sorted(c)))
    assert {c.idus for c in res.candidates} == brute


def test_big_block_finds_pocket():
    """Bloc de 127 : une poche de 5 parcelles près d'une obs. faune doit ressortir."""
    parcels, adj = synthetic_block(127, seed=0, king=True)
    target = ["P0050", "P0051", "P0052", "P0062", "P0063"]
    # aucune ancre ailleurs, la poche est la seule zone compatible
    parcels = {i: Parcel(i, p.area, excluded=(i not in target and p.excluded),
                         anchor=(i == "P0051"), occ_ok_area=p.occ_ok_area)
               for i, p in parcels.items()}
    for i in target:
        parcels[i] = Parcel(i, 23_000, excluded=False, anchor=(i == "P0051"), occ_ok_area=23_000)
    res = search_block(parcels, adj, kmax=5, min_area=110_000)
    assert any(set(target) <= set(c.idus) for c in res.candidates)
    assert len(res.pocket) < 127


FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "bloc_351397096.geojson")


@pytest.mark.skipif(not os.path.exists(FIXTURE), reason="fixture réelle non exportée")
def test_cas_reel_351397096():
    feats = json.load(open(FIXTURE))["features"]
    res = search_block(parcels_from_geojson(feats), adjacency_from_geojson(feats),
                       kmax=5, min_area=float(os.environ.get("MIN_AREA", 0)))
    want = {"331220000D2368", "331220000D2369", "331220000D2370",
            "331220000D2381", "331220000D2384"}
    assert any(want <= set(c.idus) for c in res.candidates)