#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_tiled_vs_monolithic.py
============================

Carroyage adaptatif avec groupement SIREN par tuile.

Optimisation clé : dans chaque tuile, on cherche les paires contiguës
UNIQUEMENT au sein du même SIREN — pas entre tous les IDUs de la tuile.
Ex: 500 IDUs répartis en 150 SIRENs → ~3 IDUs/SIREN → quelques paires
possibles par SIREN au lieu de 500×499/2 = 124,750 combinaisons globales.

Usage :
  python scripts/test_tiled_vs_monolithic.py --full
  python scripts/test_tiled_vs_monolithic.py --sample-size 3000
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from collections import defaultdict, Counter
from datetime import datetime
from importlib.util import module_from_spec, spec_from_file_location
from math import ceil
from pathlib import Path

from sqlalchemy import create_engine, text

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _rss_mb() -> str:
    try:
        import psutil
        return f"{psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024:.0f} MiB"
    except ImportError:
        pass
    try:
        import resource
        kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return f"{kb/1024/1024:.0f} MiB" if sys.platform == "darwin" else f"{kb/1024:.0f} MiB"
    except Exception:
        return "? MiB"

def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")

def _log(prefix: str, msg: str) -> None:
    print(f"[{_ts()}] [{prefix}] {msg}  (RAM={_rss_mb()})")


# ──────────────────────────────────────────────────────────────────────────────
# Chargement dynamique db.py
# ──────────────────────────────────────────────────────────────────────────────

def _load_backend_module(module_name: str, filename: str):
    backend_root = Path(__file__).resolve().parents[1]
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))
    module_path = backend_root / filename
    spec = spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Impossible de charger : {module_path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

_db        = _load_backend_module("backend_db", "db.py")
get_engine = _db.get_engine

def get_engine_ppm():
    url = (
        f"postgresql+psycopg://{os.environ['SUPABASE_PPM_USER']}"
        f":{os.environ['SUPABASE_PPM_PASSWORD']}"
        f"@{os.environ['SUPABASE_PPM_HOST']}"
        f":{os.environ.get('SUPABASE_PPM_PORT', '5432')}"
        f"/{os.environ.get('SUPABASE_PPM_DB', 'postgres')}"
        f"?sslmode=require"
    )
    return create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=2,
                         connect_args={"connect_timeout": 10})


# ──────────────────────────────────────────────────────────────────────────────
# Résolution projet / AOI
# ──────────────────────────────────────────────────────────────────────────────

def _resolve_project_and_aoi(engine, project_id: str | None) -> tuple[str, str]:
    with engine.begin() as conn:
        if project_id:
            row = conn.execute(text("""
                SELECT id::text AS project_id, aoi_id::text AS aoi_id
                FROM ecocompensation.projects
                WHERE id = CAST(:pid AS uuid) LIMIT 1
            """), {"pid": project_id}).mappings().one_or_none()
        else:
            row = conn.execute(text("""
                SELECT id::text AS project_id, aoi_id::text AS aoi_id
                FROM ecocompensation.projects
                WHERE aoi_id IS NOT NULL
                ORDER BY created_at DESC LIMIT 1
            """)).mappings().one_or_none()
    if not row:
        raise SystemExit("Aucun projet/AOI trouvé.")
    return str(row["project_id"]), str(row["aoi_id"])

def _set_aoi_buffer(engine, aoi_id: str, buffer_km: float) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE ecocompensation.aoi SET buffer_m = :buffer_m
            WHERE id = CAST(:aid AS uuid)
        """), {"aid": aoi_id, "buffer_m": int(round(buffer_km * 1000))})


# ──────────────────────────────────────────────────────────────────────────────
# Chargement IDUs + bboxes + SIREN
# ──────────────────────────────────────────────────────────────────────────────

def fetch_idus_with_bbox(engine_ppm, aoi_wkt: str) -> tuple[dict, dict]:
    """
    Retourne :
      - idu_bbox  : {idu: (minx, miny, maxx, maxy)}
      - idu_siren : {idu: siren}

    Filtre les SIRENs singleton (1 seule parcelle dans l'AOI) —
    ils ne peuvent jamais former de paire.
    """
    _log("fetch", "Chargement IDUs + bboxes + SIREN depuis DB PPM …")
    t0 = time.perf_counter()
    with engine_ppm.connect() as conn:
        conn.execute(text("SET statement_timeout = '180s'"))
        rows = conn.execute(text("""
            SELECT
                idu, siren,
                ST_XMin(geom_2154) AS minx,
                ST_YMin(geom_2154) AS miny,
                ST_XMax(geom_2154) AS maxx,
                ST_YMax(geom_2154) AS maxy
            FROM public.parcelles_personnes_morales
            WHERE ST_Intersects(geom_2154, ST_GeomFromText(:wkt, 2154))
              AND siren IS NOT NULL AND siren != ''
              AND geom_2154 IS NOT NULL
        """), {"wkt": aoi_wkt}).mappings().all()

    siren_count  = Counter(r["siren"] for r in rows)
    multi_sirens = {s for s, n in siren_count.items() if n >= 2}

    idu_bbox:  dict[str, tuple] = {}
    idu_siren: dict[str, str]   = {}
    n_singleton = 0
    for r in rows:
        if r["siren"] not in multi_sirens:
            n_singleton += 1
            continue
        idu_bbox[r["idu"]]  = (float(r["minx"]), float(r["miny"]),
                                float(r["maxx"]), float(r["maxy"]))
        idu_siren[r["idu"]] = r["siren"]

    _log("fetch",
         f"→ {len(idu_bbox):,} IDUs utiles"
         f"  ({n_singleton:,} singletons SIREN écartés)"
         f"  ({len(multi_sirens):,} SIRENs avec ≥2 parcelles)"
         f"  en {time.perf_counter()-t0:.1f}s")
    return idu_bbox, idu_siren


# ──────────────────────────────────────────────────────────────────────────────
# Carroyage adaptatif avec groupement SIREN
# ──────────────────────────────────────────────────────────────────────────────

MAX_IDU_PER_TILE = 500

def _idus_in_tile(idu_bbox, tx0, ty0, tx1, ty1, buf):
    bx0, by0 = tx0 - buf, ty0 - buf
    bx1, by1 = tx1 + buf, ty1 + buf
    return [
        idu for idu, (mx0, my0, mx1, my1) in idu_bbox.items()
        if mx0 <= bx1 and mx1 >= bx0 and my0 <= by1 and my1 >= by0
    ]


def _query_pairs_by_siren(
    conn,
    tile_idus: list[str],
    idu_siren: dict[str, str],
) -> tuple[set[tuple[str, str]], int]:
    """
    Pour chaque SIREN ayant ≥2 parcelles dans la tuile,
    cherche les paires contiguës uniquement entre ses propres IDUs.
    Retourne (paires, nb_requetes_sql).
    """
    by_siren: dict[str, list[str]] = defaultdict(list)
    for idu in tile_idus:
        by_siren[idu_siren[idu]].append(idu)

    all_pairs: set[tuple[str, str]] = set()
    n_queries = 0
    for siren, siren_idus in by_siren.items():
        if len(siren_idus) < 2:
            continue
        n_queries += 1
        rows = conn.execute(text("""
            SELECT a.idu AS idu_a, b.idu AS idu_b
            FROM public.parcelles_personnes_morales a
            JOIN public.parcelles_personnes_morales b
              ON a.idu < b.idu
             AND a.geom_2154 && b.geom_2154
             AND (
                   ST_Touches(a.geom_2154, b.geom_2154)
                OR ST_Relate(a.geom_2154, b.geom_2154, 'F***1****')
             )
            WHERE a.idu = ANY(:idus) AND b.idu = ANY(:idus)
        """), {"idus": siren_idus}).mappings().all()
        for r in rows:
            all_pairs.add((r["idu_a"], r["idu_b"]))

    return all_pairs, n_queries


def _process_tile_adaptive(
    conn,
    idu_bbox: dict,
    idu_siren: dict,
    tx0: float, ty0: float, tx1: float, ty1: float,
    tile_buffer_m: float,
    all_pairs: set,
    counters: dict,
    depth: int = 0,
) -> None:
    tile_idus = _idus_in_tile(idu_bbox, tx0, ty0, tx1, ty1, tile_buffer_m)

    if len(tile_idus) < 2:
        counters["skipped"] += 1
        return

    if len(tile_idus) <= MAX_IDU_PER_TILE:
        t_tile = time.perf_counter()
        tile_pairs, n_req = _query_pairs_by_siren(conn, tile_idus, idu_siren)
        counters["queries"] += n_req
        before = len(all_pairs)
        all_pairs.update(tile_pairs)
        new = len(all_pairs) - before
        elapsed = time.perf_counter() - t_tile

        # SIRENs multi dans la tuile (pour info)
        by_siren_count: dict[str, int] = defaultdict(int)
        for idu in tile_idus:
            by_siren_count[idu_siren[idu]] += 1
        n_siren_multi = sum(1 for n in by_siren_count.values() if n >= 2)

        indent = "  " * depth
        counters["tiles"] += 1
        _log("tiled",
             f"{indent}{'↳ ' if depth else ''}Tuile {counters['tiles']:>4}"
             f"  depth={depth}"
             f"  IDUs={len(tile_idus):>4,}"
             f"  SIRENs_multi={n_siren_multi:>3}"
             f"  sql={n_req}"
             f"  paires={len(tile_pairs):>4,}"
             f"  nouvelles={new:>4,}"
             f"  total={len(all_pairs):>6,}"
             f"  {elapsed:.1f}s")
    else:
        mx = (tx0 + tx1) / 2
        my = (ty0 + ty1) / 2
        counters["subdivisions"] += 1
        indent = "  " * depth
        _log("tiled",
             f"{indent}⚡ Subdivision depth={depth}"
             f"  {len(tile_idus):,} IDUs > {MAX_IDU_PER_TILE}"
             f"  → 4×{(tx1-tx0)/2/1000:.2f}km")

        for qx0, qy0, qx1, qy1 in [
            (tx0, ty0, mx,  my),
            (mx,  ty0, tx1, my),
            (tx0, my,  mx,  ty1),
            (mx,  my,  tx1, ty1),
        ]:
            _process_tile_adaptive(
                conn, idu_bbox, idu_siren,
                qx0, qy0, qx1, qy1,
                tile_buffer_m, all_pairs, counters, depth + 1,
            )


def find_touching_pairs_tiled(
    engine_ppm,
    idu_bbox: dict,
    idu_siren: dict,
    tile_size_m: float = 5_000.0,
    tile_buffer_m: float = 200.0,
) -> set[tuple[str, str]]:
    if not idu_bbox:
        return set()

    all_minx = min(v[0] for v in idu_bbox.values())
    all_miny = min(v[1] for v in idu_bbox.values())
    all_maxx = max(v[2] for v in idu_bbox.values())
    all_maxy = max(v[3] for v in idu_bbox.values())

    initial_tiles = []
    x = all_minx
    while x < all_maxx:
        y = all_miny
        while y < all_maxy:
            initial_tiles.append((x, y,
                                   min(x + tile_size_m, all_maxx),
                                   min(y + tile_size_m, all_maxy)))
            y += tile_size_m
        x += tile_size_m

    nx = ceil((all_maxx - all_minx) / tile_size_m)
    ny = ceil((all_maxy - all_miny) / tile_size_m)

    _log("tiled",
         f"Grille {nx}×{ny} = {len(initial_tiles)} tuiles de {tile_size_m/1000:.0f}km"
         f"  buffer={tile_buffer_m:.0f}m  max_idu={MAX_IDU_PER_TILE}"
         f"  → requêtes par SIREN dans chaque tuile")

    all_pairs: set[tuple[str, str]] = set()
    counters = {"tiles": 0, "queries": 0, "skipped": 0, "subdivisions": 0}
    t0 = time.perf_counter()

    with engine_ppm.connect() as conn:
        conn.execute(text("SET statement_timeout = '60s'"))
        for tx0, ty0, tx1, ty1 in initial_tiles:
            _process_tile_adaptive(
                conn, idu_bbox, idu_siren,
                tx0, ty0, tx1, ty1,
                tile_buffer_m, all_pairs, counters, depth=0,
            )

    _log("tiled",
         f"✅ {len(all_pairs):,} paires intra-SIREN"
         f"  |  {counters['tiles']} tuiles traitées"
         f"  |  {counters['queries']} requêtes SQL"
         f"  |  {counters['subdivisions']} subdivisions"
         f"  |  {counters['skipped']} tuiles vides"
         f"  |  {time.perf_counter()-t0:.1f}s")
    return all_pairs


# ──────────────────────────────────────────────────────────────────────────────
# Monolithique par SIREN (pour comparaison sur échantillon)
# ──────────────────────────────────────────────────────────────────────────────

def find_touching_pairs_monolithic(
    engine_ppm,
    idus: list[str],
    idu_siren: dict,
    timeout: str = "120s",
) -> set[tuple[str, str]] | None:
    _log("mono", f"Monolithique par SIREN sur {len(idus):,} IDUs …")
    t0 = time.perf_counter()
    by_siren: dict[str, list[str]] = defaultdict(list)
    for idu in idus:
        by_siren[idu_siren[idu]].append(idu)

    all_pairs: set[tuple[str, str]] = set()
    try:
        with engine_ppm.connect() as conn:
            conn.execute(text(f"SET statement_timeout = '{timeout}'"))
            for siren, siren_idus in by_siren.items():
                if len(siren_idus) < 2:
                    continue
                rows = conn.execute(text("""
                    SELECT a.idu AS idu_a, b.idu AS idu_b
                    FROM public.parcelles_personnes_morales a
                    JOIN public.parcelles_personnes_morales b
                      ON a.idu < b.idu
                     AND a.geom_2154 && b.geom_2154
                     AND (
                           ST_Touches(a.geom_2154, b.geom_2154)
                        OR ST_Relate(a.geom_2154, b.geom_2154, 'F***1****')
                     )
                    WHERE a.idu = ANY(:idus) AND b.idu = ANY(:idus)
                """), {"idus": siren_idus}).mappings().all()
                for r in rows:
                    all_pairs.add((r["idu_a"], r["idu_b"]))
        _log("mono", f"→ {len(all_pairs):,} paires en {time.perf_counter()-t0:.1f}s")
        return all_pairs
    except Exception as exc:
        _log("mono", f"❌ Échec après {time.perf_counter()-t0:.1f}s : {exc}")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Modes
# ──────────────────────────────────────────────────────────────────────────────

def run_full_tiled(engine_ppm, idu_bbox, idu_siren, tile_size_m, tile_buffer_m):
    sep = "═" * 72
    print(sep)
    _log("full", f"{len(idu_bbox):,} IDUs — carroyage adaptatif + SIREN-first")
    print(sep)
    pairs = find_touching_pairs_tiled(engine_ppm, idu_bbox, idu_siren,
                                       tile_size_m, tile_buffer_m)
    print(sep)
    _log("full", f"✅ {len(pairs):,} paires intra-SIREN sur toute l'AOI")
    print(sep)


def compare_on_sample(
    engine_ppm, idu_bbox, idu_siren,
    sample_size, tile_size_m, tile_buffer_m, seed=42,
) -> bool:
    sep = "─" * 72
    rng = random.Random(seed)
    sample_idus  = rng.sample(list(idu_bbox.keys()), min(sample_size, len(idu_bbox)))
    sample_bbox  = {idu: idu_bbox[idu]  for idu in sample_idus}
    sample_siren = {idu: idu_siren[idu] for idu in sample_idus}
    _log("compare", f"Échantillon {len(sample_idus):,} IDUs (seed={seed})")
    print(sep)

    print("\n  [A] MONOLITHIQUE par SIREN")
    pairs_mono = find_touching_pairs_monolithic(engine_ppm, sample_idus,
                                                 sample_siren, "120s")
    print("\n  [B] TUILÉ adaptatif + SIREN")
    pairs_tiled = find_touching_pairs_tiled(engine_ppm, sample_bbox, sample_siren,
                                             tile_size_m, tile_buffer_m)
    print(f"\n{sep}\n  COMPARAISON\n{sep}")

    if pairs_mono is None:
        _log("compare", f"❌ Mono timeouté. Tuilé: {len(pairs_tiled):,} paires.")
        return False

    only_mono  = pairs_mono  - pairs_tiled
    only_tiled = pairs_tiled - pairs_mono
    print(f"  Mono  : {len(pairs_mono):,}  |  Tuilé : {len(pairs_tiled):,}"
          f"  |  Communes : {len(pairs_mono & pairs_tiled):,}")
    print(f"  Uniquement mono  : {len(only_mono):,}  ← doit être 0")
    print(f"  Uniquement tuilé : {len(only_tiled):,}  ← doit être 0")
    print(sep)

    if not only_mono and not only_tiled:
        print("  ✅ RÉSULTATS IDENTIQUES — patch module validé.")
        return True
    else:
        print("  ❌ DIVERGENCE")
        if only_mono:  print(f"     Manqués : {list(only_mono)[:3]}")
        if only_tiled: print(f"     En trop  : {list(only_tiled)[:3]}")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project-id")
    parser.add_argument("--buffer-km",     type=float, default=20.0)
    parser.add_argument("--tile-size-km",  type=float, default=5.0)
    parser.add_argument("--tile-buffer-m", type=float, default=200.0)
    parser.add_argument("--sample-size",   type=int,   default=3_000)
    parser.add_argument("--full",          action="store_true", default=False)
    parser.add_argument("--seed",          type=int,   default=42)
    args = parser.parse_args()

    engine     = get_engine()
    engine_ppm = get_engine_ppm()

    project_id, aoi_id = _resolve_project_and_aoi(engine, args.project_id)
    _set_aoi_buffer(engine, aoi_id, args.buffer_km)

    sep = "═" * 72
    print(sep)
    print(f"  Projet       : {project_id}")
    print(f"  AOI          : {aoi_id}")
    print(f"  Buffer AOI   : {args.buffer_km:.1f} km")
    print(f"  Tuile        : {args.tile_size_km:.1f} km  (buffer={args.tile_buffer_m:.0f}m)")
    print(f"  Max IDU/tuile: {MAX_IDU_PER_TILE}")
    print(f"  Mode         : {'full' if args.full else f'sample={args.sample_size:,}'}")
    print(f"  RAM départ   : {_rss_mb()}")
    print(sep)

    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT ST_AsText(ST_Buffer(geom_2154, buffer_m)) AS wkt
            FROM ecocompensation.aoi WHERE id = CAST(:aid AS uuid)
        """), {"aid": aoi_id}).mappings().one_or_none()
    if not row:
        raise SystemExit("AOI introuvable.")

    idu_bbox, idu_siren = fetch_idus_with_bbox(engine_ppm, row["wkt"])
    if not idu_bbox:
        raise SystemExit("Aucun IDU PM dans l'AOI.")

    if args.full:
        run_full_tiled(engine_ppm, idu_bbox, idu_siren,
                       args.tile_size_km * 1000, args.tile_buffer_m)
    else:
        ok = compare_on_sample(engine_ppm, idu_bbox, idu_siren,
                                args.sample_size,
                                args.tile_size_km * 1000, args.tile_buffer_m,
                                seed=args.seed)
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()