#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
count_subsets.py
================
Compte les sous-ensembles contigus par UF SANS appliquer aucun filtre SQL.
Rapide : seulement 1 requête SQL par UF (build_adjacency), pas de check_union_filters.
Permet d'ajuster MAX_UF_NB_PARCELLES avant de lancer le vrai filtrage.
"""
from __future__ import annotations
import os, sys, time
from collections import defaultdict
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from urllib.parse import quote_plus

PROJECT_ID          = "54987c59-ad94-46b2-9f20-aa679dbcf3a1"
MAX_UF_NB_PARCELLES = 10   # cap à tester — modifie ici pour simuler
MAX_K               = 5

def get_contiguous_subsets(adj, nodes, max_k):
    results = set()
    def dfs(current, frontier):
        if len(current) >= 2:
            results.add(current)
        if len(current) >= max_k:
            return
        pivot = max(current)
        for node in sorted(frontier):
            if node > pivot:
                new = current | {node}
                dfs(new, (frontier | adj.get(node, set())) - new)
    for start in nodes:
        dfs(frozenset({start}), set(adj.get(start, set())))
    return list(results)

def build_adjacency(conn, project_id, uf_id, members):
    adj = {m["idu"]: set() for m in members}
    if len(members) <= 1: return adj
    if len(members) == 2:
        a, b = members[0]["idu"], members[1]["idu"]
        adj[a].add(b); adj[b].add(a)
        return adj
    valid = {m["idu"] for m in members}
    rows = conn.execute(text("""
        SELECT a.idu, b.idu
        FROM ecocompensation_results.unites_foncieres a
        JOIN ecocompensation_results.unites_foncieres b
          ON a.project_id = b.project_id AND a.uf_id = b.uf_id
         AND a.idu < b.idu
         AND a.geom_2154 && b.geom_2154
         AND (ST_Touches(a.geom_2154, b.geom_2154)
              OR ST_Relate(a.geom_2154, b.geom_2154, 'F***1****'))
        WHERE a.project_id = :pid AND a.uf_id = :uf_id
          AND a.idu = ANY(:idus) AND b.idu = ANY(:idus)
    """), {"pid": project_id, "uf_id": uf_id, "idus": list(valid)}).all()
    for ia, ib in rows:
        adj[ia].add(ib); adj[ib].add(ia)
    return adj

def main():
    load_dotenv(Path(__file__).parent / ".env")
    pw = quote_plus(os.getenv("SUPABASE_PASSWORD", ""))
    engine = create_engine(
        f"postgresql+psycopg://{os.getenv('SUPABASE_USER')}:{pw}"
        f"@{os.getenv('SUPABASE_HOST')}:{os.getenv('SUPABASE_PORT','6543')}"
        f"/{os.getenv('SUPABASE_DB','postgres')}",
        connect_args={"keepalives": 1, "keepalives_idle": 30,
                      "keepalives_interval": 10, "keepalives_count": 5},
        pool_pre_ping=True,
    )

    # Charger membres (sans géométries)
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT uf_id, idu, surface_ha, nb_parcelles
            FROM ecocompensation_results.unites_foncieres
            WHERE project_id = :pid
        """), {"pid": PROJECT_ID}).mappings().all()

    by_uf = defaultdict(list)
    for r in rows:
        by_uf[r["uf_id"]].append({"idu": r["idu"], "surface_ha": float(r["surface_ha"] or 0)})

    # Appliquer le cap
    uf_qualifiees = {
        uf_id: members for uf_id, members in by_uf.items()
        if 2 <= len(members) <= MAX_UF_NB_PARCELLES
    }
    n_cap = sum(1 for m in by_uf.values() if len(m) > MAX_UF_NB_PARCELLES)

    print(f"📦 {len(by_uf)} UF  →  {len(uf_qualifiees)} retenues (cap ≤ {MAX_UF_NB_PARCELLES}p)  |  {n_cap} ignorées")
    print(f"   Comptage en cours (1 requête SQL/UF, pas de filtre)...\n")

    total_subsets = 0
    by_k = defaultdict(int)
    by_size = defaultdict(lambda: {"count": 0, "subsets": 0})
    t0 = time.perf_counter()

    with engine.begin() as conn:
        for i, (uf_id, members) in enumerate(uf_qualifiees.items(), 1):
            nb = len(members)
            adj = build_adjacency(conn, PROJECT_ID, uf_id, members)
            subsets = get_contiguous_subsets(adj, [m["idu"] for m in members], MAX_K)
            n = len(subsets)
            total_subsets += n
            for s in subsets:
                by_k[len(s)] += 1
            by_size[nb]["count"] += 1
            by_size[nb]["subsets"] += n

            if i % 100 == 0 or i == len(uf_qualifiees):
                elapsed = time.perf_counter() - t0
                rate = i / elapsed if elapsed > 0 else 1
                eta = (len(uf_qualifiees) - i) / rate
                sys.stdout.write(
                    f"\r   [{i:>4}/{len(uf_qualifiees)}]  "
                    f"total ss-ens: {total_subsets:>8,}  ETA: ~{eta:.0f}s    "
                )
                sys.stdout.flush()

    print(f"\n\n{'='*60}")
    print(f"TOTAL SOUS-ENSEMBLES À FILTRER : {total_subsets:,}")
    print(f"Temps comptage                 : {time.perf_counter()-t0:.1f}s")
    print(f"\n── Par taille k ──")
    for k in sorted(by_k):
        print(f"   k={k} : {by_k[k]:,}")
    print(f"\n── Par taille d'UF ──")
    print(f"  {'Taille':>8}  {'UF':>6}  {'Ss-ens/UF':>10}  {'Total':>10}")
    for nb in sorted(by_size):
        s = by_size[nb]
        print(f"  {nb:>8}  {s['count']:>6}  {s['subsets']/s['count']:>10.1f}  {s['subsets']:>10,}")

    # Simulation avec différents caps
    print(f"\n── Simulation caps alternatifs ──")
    caps = [5, 7, 10, 15, 20]
    for cap in caps:
        total = sum(
            by_size[nb]["subsets"]
            for nb in by_size if nb <= cap
        )
        n_uf = sum(by_size[nb]["count"] for nb in by_size if nb <= cap)
        print(f"   cap ≤ {cap:>2}p : {n_uf:>5} UF  →  {total:>8,} sous-ensembles")

if __name__ == "__main__":
    main()