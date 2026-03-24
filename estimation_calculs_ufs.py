#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
benchmark_uf_contigus.py  (v3)
==============================

Lit uniquement depuis ecocompensation_results.unites_foncieres.
Reconstruction de l'adjacence via self-join SQL sur uf_id — évite de passer
les géométries en paramètres (plantait sur les grandes UF avec 27k+ paires).
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from urllib.parse import quote_plus

# ─────────────────────────────────────────────
# Paramètres
# ─────────────────────────────────────────────

SEUIL_HA    = 15.0
MAX_K       = 5
LOG_EVERY   = 50
SAMPLE_SIZE: int | None = None   # None = toutes, int = N premières (nb_parcelles DESC)


# ─────────────────────────────────────────────
# Énumération des sous-ensembles contigus
# ─────────────────────────────────────────────

def get_contiguous_subsets(
    adj: dict[str, set[str]],
    nodes: list[str],
    max_k: int,
) -> list[frozenset[str]]:
    """
    DFS avec canonical ordering (pivot = max lexicographique du current_set).
    Zéro doublon, pas de post-dédup nécessaire.
    """
    results: set[frozenset[str]] = set()

    def dfs(current: frozenset[str], frontier: set[str]):
        if len(current) >= 2:
            results.add(current)
        if len(current) >= max_k:
            return
        pivot = max(current)
        for node in sorted(frontier):
            if node > pivot:
                new = current | {node}
                new_frontier = (frontier | adj.get(node, set())) - new
                dfs(new, new_frontier)

    for start in nodes:
        dfs(frozenset({start}), set(adj.get(start, set())))

    return list(results)


# ─────────────────────────────────────────────
# Chargement
# ─────────────────────────────────────────────

def load_all_ufs(conn, project_id: str, sample: int | None) -> dict[str, list[dict]]:
    """
    Retourne {uf_id: [{"idu", "surface_ha"}, ...]} trié nb_parcelles DESC.
    On ne charge PAS les géométries ici — elles sont récupérées par uf_id dans build_adjacency.
    """
    rows = conn.execute(
        text("""
            SELECT uf_id, idu, surface_ha, nb_parcelles
            FROM ecocompensation_results.unites_foncieres
            WHERE project_id = :pid
            ORDER BY nb_parcelles DESC, uf_id
        """),
        {"pid": project_id},
    ).mappings().all()

    by_uf: dict[str, list[dict]] = defaultdict(list)
    nb_map: dict[str, int] = {}
    for r in rows:
        by_uf[r["uf_id"]].append({
            "idu":        r["idu"],
            "surface_ha": float(r["surface_ha"] or 0),
        })
        nb_map[r["uf_id"]] = r["nb_parcelles"]

    ordered = dict(sorted(by_uf.items(), key=lambda kv: -nb_map.get(kv[0], 0)))

    if sample:
        ordered = dict(list(ordered.items())[:sample])

    return ordered


def build_adjacency(conn, project_id: str, uf_id: str, members: list[dict]) -> dict[str, set[str]]:
    """
    Reconstruit le graphe de contigüité via self-join SQL sur uf_id.
    Les géométries restent côté serveur — aucun WKT transféré en paramètre.

    Court-circuit pour les paires (2 membres toujours contigus par construction).
    """
    adj: dict[str, set[str]] = {m["idu"]: set() for m in members}

    if len(members) <= 1:
        return adj

    if len(members) == 2:
        a, b = members[0]["idu"], members[1]["idu"]
        adj[a].add(b)
        adj[b].add(a)
        return adj

    # Self-join sur la table — géométries restent en base
    rows = conn.execute(
        text("""
            SELECT a.idu AS idu_a, b.idu AS idu_b
            FROM ecocompensation_results.unites_foncieres a
            JOIN ecocompensation_results.unites_foncieres b
              ON a.project_id = b.project_id
             AND a.uf_id      = b.uf_id
             AND a.idu        < b.idu
             AND a.geom_2154 && b.geom_2154
             AND (
                 ST_Touches(a.geom_2154, b.geom_2154)
                 OR ST_Relate(a.geom_2154, b.geom_2154, 'F***1****')
             )
            WHERE a.project_id = :pid
              AND a.uf_id      = :uf_id
        """),
        {"pid": project_id, "uf_id": uf_id},
    ).all()

    for ia, ib in rows:
        adj[ia].add(ib)
        adj[ib].add(ia)

    return adj


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    BASE_DIR = Path(__file__).resolve().parent
    load_dotenv(BASE_DIR / ".env")

    pw = quote_plus(os.getenv("SUPABASE_PASSWORD", ""))
    db_url = (
        f"postgresql+psycopg://{os.getenv('SUPABASE_USER')}:{pw}"
        f"@{os.getenv('SUPABASE_HOST')}:{os.getenv('SUPABASE_PORT', '6543')}"
        f"/{os.getenv('SUPABASE_DB', 'postgres')}"
    )
    engine = create_engine(
        db_url,
        pool_pre_ping=True,
        connect_args={
            "prepare_threshold": None,
            "sslmode": "require",
            # keepalives côté client (libpq) pour stabiliser les connexions SSL
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 5,
        },
    )

    # Projet avec le plus d'UF
    with engine.begin() as conn:
        row = conn.execute(
            text("""
                SELECT project_id, COUNT(*) AS nb
                FROM ecocompensation_results.unites_foncieres
                GROUP BY project_id ORDER BY nb DESC LIMIT 1
            """)
        ).one_or_none()

    if not row:
        print("⚠️  Aucune UF en base. Lance d'abord aoi_to_unites_foncieres.py.")
        return

    project_id = str(row[0])
    print(f"🔗 Projet : {project_id}  ({row[1]:,} lignes UF)")
    print(f"   Seuil : {SEUIL_HA} ha  |  Max k : {MAX_K}  |  Sample : {SAMPLE_SIZE or 'toutes'}")
    print()

    with engine.begin() as conn:
        ufs = load_all_ufs(conn, project_id, SAMPLE_SIZE)

    total_uf = len(ufs)
    print(f"📦 {total_uf} UF à traiter.")
    print()

    # ── Boucle principale ──────────────────────────────────────────────

    t_global = time.perf_counter()

    total_subsets = 0
    total_matched = 0
    by_k_enum:  dict[int, int] = defaultdict(int)
    by_k_match: dict[int, int] = defaultdict(int)
    by_uf_size: dict[int, dict] = defaultdict(lambda: {
        "count": 0, "subsets": 0, "matched": 0, "time_s": 0.0, "pairs_sum": 0
    })
    big_details: list[dict] = []

    with engine.begin() as conn:
        for i, (uf_id, members) in enumerate(ufs.items()):
            nb = len(members)
            t_uf = time.perf_counter()

            # Adjacence via self-join SQL (géométries restent en base)
            adj = build_adjacency(conn, project_id, uf_id, members)
            nb_pairs = sum(len(v) for v in adj.values()) // 2

            # Énumération sous-ensembles contigus
            idus = [m["idu"] for m in members]
            subsets = get_contiguous_subsets(adj, idus, MAX_K)
            n_sub = len(subsets)

            # Match surface (somme surfaces individuelles — pas de chevauchement)
            surf_map = {m["idu"]: m["surface_ha"] for m in members}
            n_match = 0
            for s in subsets:
                by_k_enum[len(s)] += 1
                if sum(surf_map.get(idu, 0.0) for idu in s) >= SEUIL_HA:
                    n_match += 1
                    by_k_match[len(s)] += 1

            elapsed = time.perf_counter() - t_uf
            total_subsets += n_sub
            total_matched += n_match

            st = by_uf_size[nb]
            st["count"]     += 1
            st["subsets"]   += n_sub
            st["matched"]   += n_match
            st["time_s"]    += elapsed
            st["pairs_sum"] += nb_pairs

            if nb >= 20:
                big_details.append({
                    "nb": nb, "uf_id": uf_id,
                    "pairs": nb_pairs, "subsets": n_sub,
                    "matched": n_match, "ms": elapsed * 1000,
                })

            if (i + 1) % LOG_EVERY == 0 or (i + 1) == total_uf:
                et = time.perf_counter() - t_global
                rate = (i + 1) / et if et > 0 else 1
                eta = (total_uf - i - 1) / rate
                print(
                    f"  [{i+1:>4}/{total_uf}]  "
                    f"sous-ens: {total_subsets:>8,}  "
                    f"matchs: {total_matched:>6,}  "
                    f"ETA: ~{eta:.0f}s"
                )

    t_total = time.perf_counter() - t_global

    # ── Rapport ───────────────────────────────────────────────────────

    print()
    print("=" * 72)
    print("RÉSULTATS DU BENCHMARK")
    print("=" * 72)
    print(f"  UF traitées              : {total_uf:,}")
    print(f"  Sous-ensembles énumérés  : {total_subsets:,}")
    print(f"  Sous-ensembles matchant  : {total_matched:,}  (≥ {SEUIL_HA} ha)")
    print(f"  Taux de match            : {total_matched / max(total_subsets, 1) * 100:.1f}%")
    print(f"  Temps total              : {t_total:.1f}s")
    print(f"  Vitesse                  : {total_subsets / max(t_total, 0.001):,.0f} sous-ens./s")
    print()

    print("── Par taille de sous-ensemble (k) ──")
    print(f"  {'k':>4}  {'Énumérés':>12}  {'Matchs':>10}  {'Taux':>8}")
    print(f"  {'─'*46}")
    for k in sorted(by_k_enum):
        e = by_k_enum[k]
        m = by_k_match.get(k, 0)
        print(f"  {k:>4}  {e:>12,}  {m:>10,}  {m/e*100:>7.1f}%")
    print()

    print("── Par tranche de taille d'UF ──")
    groupes = [
        ("2",      range(2, 3)),
        ("3",      range(3, 4)),
        ("4",      range(4, 5)),
        ("5",      range(5, 6)),
        ("6–10",   range(6, 11)),
        ("11–20",  range(11, 21)),
        ("21–50",  range(21, 51)),
        ("51–100", range(51, 101)),
        ("> 100",  range(101, 500)),
    ]
    print(f"  {'Taille':>8}  {'UF':>5}  {'Ss-ens/UF':>10}  {'Total':>10}  {'Matchs':>8}  {'ms/UF':>8}  {'Paires/UF':>10}")
    print(f"  {'─'*72}")
    for label, rng in groupes:
        nb_uf  = sum(by_uf_size[s]["count"]    for s in rng if s in by_uf_size)
        if nb_uf == 0:
            continue
        tot_s  = sum(by_uf_size[s]["subsets"]  for s in rng if s in by_uf_size)
        tot_m  = sum(by_uf_size[s]["matched"]  for s in rng if s in by_uf_size)
        tot_t  = sum(by_uf_size[s]["time_s"]   for s in rng if s in by_uf_size)
        tot_p  = sum(by_uf_size[s]["pairs_sum"]for s in rng if s in by_uf_size)
        print(
            f"  {label:>8}  {nb_uf:>5}  {tot_s/nb_uf:>10.1f}  "
            f"{tot_s:>10,}  {tot_m:>8,}  {tot_t/nb_uf*1000:>8.1f}  {tot_p/nb_uf:>10.1f}"
        )
    print()

    # Comparaison estimation théorique
    D_MOY = 2.5
    total_estim = sum(
        round(sum(s * (D_MOY - 1) ** (k - 1) for k in range(2, min(s, MAX_K) + 1))) * st["count"]
        for s, st in by_uf_size.items()
    )
    ratio = total_subsets / total_estim if total_estim else 0
    print("── Estimation théorique (d=2.5) vs réel ──")
    print(f"  Estimation  : {total_estim:,}")
    print(f"  Réel        : {total_subsets:,}")
    print(f"  Ratio       : {ratio:.2f}x  {'✅' if 0.5 < ratio < 2 else '⚠️  écart significatif'}")
    print()

    if big_details:
        big_details.sort(key=lambda x: -x["nb"])
        print("── Détail grosses UF (≥ 20 parcelles) ──")
        print(f"  {'Taille':>8}  {'uf_id':<28}  {'Paires':>7}  {'Ss-ens':>8}  {'Matchs':>7}  {'ms':>7}")
        for d in big_details[:30]:
            print(
                f"  {d['nb']:>8}  {d['uf_id']:<28}  {d['pairs']:>7}  "
                f"{d['subsets']:>8,}  {d['matched']:>7,}  {d['ms']:>7.1f}"
            )

    print()
    print("Fin du benchmark.")


if __name__ == "__main__":
    main()