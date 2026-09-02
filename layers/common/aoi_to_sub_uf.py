#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aoi_to_sub_uf.py
================

AOI → sous-ensembles de parcelles de personnes morales (k=2..MAX_K).

Une passe unique :
  1. Parcelles PPM dans l'AOI
  2. Groupement SIREN + clustering de contiguité (union-find)
  3. Énumération des sous-ensembles contigus (DFS)
  4. Écriture de ecocompensation_results.unites_foncieres (trace par parcelle)
     et ecocompensation_results.sous_ensembles (produit utile)

Les paires ST_Touches sont calculées une seule fois par SIREN (plus de
re-join spatial au moment des sous-ensembles). Les unions / Miller /
distance au centre se font en mémoire (shapely) à partir des WKT déjà
chargés — pas de second aller-retour PostGIS par sous-ensemble.

Signature layer_runner :
    run(engine_core, project_id, aoi_id, cb=None, *, engine_ppm=None,
        min_area_ha=7.0, max_uf_parcelles=10) -> int
    (retour = nombre de sous-ensembles insérés)
"""

from __future__ import annotations

import logging
import math
import os
import time
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from shapely import wkt as shapely_wkt
from shapely.geometry import Point
from shapely.ops import unary_union
from sqlalchemy import text

logger = logging.getLogger(__name__)

PARCELLES_PM_TABLE = os.getenv("PARCELLES_PM_TABLE", "public.parcelles_personnes_morales")
MIN_AREA_HA_PREFILTER = 7.0
MAX_K = 5
MAX_UF_PARCELLES = 10
BATCH_UF = 500
BATCH_SS = 200

DDL_UF = """
CREATE SCHEMA IF NOT EXISTS ecocompensation_results;
CREATE TABLE IF NOT EXISTS ecocompensation_results.unites_foncieres (
    project_id      uuid NOT NULL,
    uf_id           text NOT NULL,
    siren           text,
    denomination    text,
    forme_juridique text,
    nb_parcelles    integer,
    surface_ha_uf   double precision,
    idu             text NOT NULL,
    surface_ha      double precision,
    geom_2154       geometry(Geometry, 2154)
);
CREATE INDEX IF NOT EXISTS idx_uf_geom
    ON ecocompensation_results.unites_foncieres USING GIST (geom_2154);
CREATE INDEX IF NOT EXISTS idx_uf_project_id
    ON ecocompensation_results.unites_foncieres (project_id);
CREATE INDEX IF NOT EXISTS idx_uf_uf_id
    ON ecocompensation_results.unites_foncieres (uf_id);
"""

DDL_SS = """
CREATE TABLE IF NOT EXISTS ecocompensation_results.sous_ensembles (
    id            serial PRIMARY KEY,
    project_id    uuid NOT NULL,
    uf_id         text NOT NULL,
    subset_id     text NOT NULL,
    k             integer NOT NULL,
    idus          text[] NOT NULL,
    surface_ha    double precision,
    geom_2154     geometry(Geometry, 2154),
    miller        double precision,
    dist_centre_m double precision,
    denomination  text,
    siren         text,
    created_at    timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ss_geom
    ON ecocompensation_results.sous_ensembles USING GIST (geom_2154);
CREATE INDEX IF NOT EXISTS idx_ss_project_id
    ON ecocompensation_results.sous_ensembles (project_id);
CREATE INDEX IF NOT EXISTS idx_ss_uf_id
    ON ecocompensation_results.sous_ensembles (uf_id);
CREATE INDEX IF NOT EXISTS idx_ss_k
    ON ecocompensation_results.sous_ensembles (k);
"""


class UnionFind:
    def __init__(self):
        self._parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        if x not in self._parent:
            self._parent[x] = x
        if self._parent[x] != x:
            self._parent[x] = self.find(self._parent[x])
        return self._parent[x]

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra

    def clusters(self, nodes: list[str]) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = defaultdict(list)
        for n in nodes:
            groups[self.find(n)].append(n)
        return dict(groups)


def get_contiguous_subsets(adj: dict, nodes: list[str], max_k: int) -> list[frozenset]:
    results: set[frozenset] = set()

    def dfs(current: frozenset, frontier: set) -> None:
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


def _valid_geom(g):
    if g is None or g.is_empty:
        return None
    if g.is_valid:
        return g
    fixed = g.buffer(0)
    return None if fixed is None or fixed.is_empty else fixed


def _union_metrics(wkts: list[str], cx: float, cy: float) -> dict | None:
    geoms = []
    for w in wkts:
        if not w:
            continue
        try:
            g = _valid_geom(shapely_wkt.loads(w))
        except Exception:
            continue
        if g is not None:
            geoms.append(g)
    if not geoms:
        return None
    union = unary_union(geoms)
    union = _valid_geom(union)
    if union is None:
        return None
    area = float(union.area)
    peri = float(union.length)
    miller = (4.0 * math.pi * area) / (peri ** 2) if peri else 0.0
    centroid = union.centroid
    dist = float(centroid.distance(Point(cx, cy)))
    return {
        "geom_wkt": union.wkt,
        "surface_ha": area / 10_000.0,
        "miller": miller,
        "dist_centre_m": dist,
    }


def _get_aoi(conn_core, aoi_id: str) -> tuple[str, float, float]:
    row = conn_core.execute(
        text("""
            SELECT ST_AsText(geom_2154) AS wkt,
                   ST_X(ST_Centroid(geom_2154)) AS cx,
                   ST_Y(ST_Centroid(geom_2154)) AS cy
            FROM ecocompensation.aoi WHERE id = :aid
        """),
        {"aid": aoi_id},
    ).mappings().one_or_none()
    if not row or not row["wkt"]:
        raise RuntimeError(f"AOI introuvable pour aoi_id={aoi_id}")
    return str(row["wkt"]), float(row["cx"]), float(row["cy"])


def _sanitize_dept_code(raw: object) -> str | None:
    """Code département INSEE (2 car. : 33, 2A, 97…)."""
    s = str(raw or "").strip().upper()
    if len(s) == 2 and s.isalnum():
        return s
    return None


def _dept_codes_for_aoi(conn_core, project_id: str, aoi_wkt: str) -> list[str]:
    """Départements couverts par l'AOI — préfiltre attributaire PPM, sans département en dur.

    1. Parcelles déjà tilées du projet (filter_v2 lance les UF après le pool).
    2. Sinon DISTINCT cadastre national, GiST bbox de l'AOI (pas un scan France).
    """
    seen: set[str] = set()
    depts: list[str] = []

    def _collect(rows) -> None:
        for r in rows:
            d = _sanitize_dept_code(r)
            if d and d not in seen:
                seen.add(d)
                depts.append(d)

    _collect(
        conn_core.execute(
            text("""
                SELECT DISTINCT left(trim(code_insee), 2) AS dept
                FROM ecocompensation_results.parcelles
                WHERE project_id = CAST(:pid AS uuid)
                  AND code_insee IS NOT NULL
                  AND length(trim(code_insee)) >= 2
            """),
            {"pid": project_id},
        ).scalars().all()
    )
    if depts:
        return depts

    _collect(
        conn_core.execute(
            text("""
                SELECT DISTINCT left(trim(code_insee), 2) AS dept
                FROM ecocompensation.parcelles
                WHERE geom_2154 && ST_GeomFromText(:w, 2154)
                  AND code_insee IS NOT NULL
                  AND length(trim(code_insee)) >= 2
            """),
            {"w": aoi_wkt},
        ).scalars().all()
    )
    return depts


def _fetch_ppm_in_aoi(
    conn_ppm,
    aoi_wkt: str,
    dept_codes: list[str] | None = None,
) -> list[dict]:
    """PPM ∩ AOI. Préfiltre `code_insee LIKE ANY (33%, 40%…)` dérivé de l'AOI + GiST."""
    dept_sql = ""
    params: dict = {"w": aoi_wkt}
    if dept_codes:
        params["likes"] = [f"{d}%" for d in dept_codes]
        dept_sql = "AND code_insee LIKE ANY(CAST(:likes AS text[]))"
    rows = conn_ppm.execute(
        text(f"""
            SELECT
                idu, siren, denomination, forme_juridique,
                ST_Area(geom_2154)   AS area_m2,
                ST_AsText(geom_2154) AS geom_wkt
            FROM {PARCELLES_PM_TABLE}
            WHERE geom_2154 && ST_GeomFromText(:w, 2154)
              AND ST_Intersects(geom_2154, ST_GeomFromText(:w, 2154))
              AND siren IS NOT NULL AND siren != ''
              AND geom_2154 IS NOT NULL
              {dept_sql}
        """),
        params,
    ).mappings().all()
    return [dict(r) for r in rows]


def _find_touching_pairs(conn_ppm, idus: list[str]) -> list[tuple[str, str]]:
    if len(idus) < 2:
        return []
    rows = conn_ppm.execute(
        text(f"""
            SELECT a.idu, b.idu
            FROM {PARCELLES_PM_TABLE} a
            JOIN {PARCELLES_PM_TABLE} b
              ON a.idu < b.idu
             AND a.geom_2154 && b.geom_2154
             AND (ST_Touches(a.geom_2154, b.geom_2154)
                  OR ST_Relate(a.geom_2154, b.geom_2154, 'F***1****'))
            WHERE a.idu = ANY(:idus) AND b.idu = ANY(:idus)
        """),
        {"idus": idus},
    ).all()
    return [(r[0], r[1]) for r in rows]


def _adj_from_pairs(idus: list[str], pairs: list[tuple[str, str]]) -> dict[str, set]:
    members = set(idus)
    adj: dict[str, set] = {i: set() for i in idus}
    if len(idus) == 2:
        a, b = idus[0], idus[1]
        adj[a].add(b)
        adj[b].add(a)
        return adj
    for a, b in pairs:
        if a in members and b in members:
            adj[a].add(b)
            adj[b].add(a)
    return adj


def run(
    engine_core,
    project_id: str,
    aoi_id: str,
    cb=None,
    *,
    engine_ppm=None,
    min_area_ha: float | None = None,
    max_uf_parcelles: int | None = None,
) -> int:
    """
    Construit UF + sous-ensembles pour l'AOI.

    :return: nombre de sous-ensembles insérés.
    """
    log = cb or logger.info
    engine_ppm = engine_ppm or engine_core
    min_area = MIN_AREA_HA_PREFILTER if min_area_ha is None else float(min_area_ha)
    cap = MAX_UF_PARCELLES if max_uf_parcelles is None else int(max_uf_parcelles)
    t0 = time.perf_counter()

    with engine_core.begin() as conn:
        conn.execute(text(DDL_UF))
        conn.execute(text(DDL_SS))
        conn.execute(
            text("DELETE FROM ecocompensation_results.sous_ensembles WHERE project_id = :pid"),
            {"pid": project_id},
        )
        conn.execute(
            text("DELETE FROM ecocompensation_results.unites_foncieres WHERE project_id = :pid"),
            {"pid": project_id},
        )

    with engine_core.begin() as conn:
        aoi_wkt, cx, cy = _get_aoi(conn, aoi_id)
        depts = _dept_codes_for_aoi(conn, project_id, aoi_wkt)
    log(f"PHASE:unites_foncieres:start centre=({cx:.0f},{cy:.0f})")
    if depts:
        log(f"   Préfiltre PPM départements INSEE : {', '.join(depts)}")
    else:
        log("   Préfiltre PPM : aucun département dérivé de l'AOI — filtre spatial seul")

    t_fetch = time.perf_counter()
    with engine_ppm.begin() as conn:
        parcelles = _fetch_ppm_in_aoi(conn, aoi_wkt, depts)
    log(f"   PPM dans l'AOI : {len(parcelles):,} parcelles ({time.perf_counter() - t_fetch:.1f}s)")

    if not parcelles:
        log("PHASE:unites_foncieres:done:0")
        log("PHASE:sous_ensembles:done:0")
        return 0

    by_siren: dict[str, list[dict]] = defaultdict(list)
    for p in parcelles:
        by_siren[p["siren"]].append(p)
    sirens_multi = {s: ps for s, ps in by_siren.items() if len(ps) >= 2}
    log(f"   {len(by_siren):,} SIREN  |  {len(sirens_multi):,} avec ≥ 2 parcelles")

    if not sirens_multi:
        log("PHASE:unites_foncieres:done:0")
        log("PHASE:sous_ensembles:done:0")
        return 0

    uf_rows: list[dict] = []
    ss_rows: list[dict] = []
    by_size: dict[int, int] = defaultdict(int)
    n_cap_skip = 0
    n_area_skip = 0
    n_prefilter_skip = 0
    n_uf = 0

    log("PHASE:sous_ensembles:start")
    t_clust = time.perf_counter()
    with engine_ppm.begin() as conn_ppm:
        for siren, ps in sirens_multi.items():
            idus = [p["idu"] for p in ps]
            denom = ps[0].get("denomination") or ""
            forme = ps[0].get("forme_juridique") or ""
            idu_to_p = {p["idu"]: p for p in ps}
            pairs = _find_touching_pairs(conn_ppm, idus)

            uf = UnionFind()
            for idu in idus:
                uf.find(idu)
            for a, b in pairs:
                uf.union(a, b)

            cluster_idx = 0
            for _root, members in uf.clusters(idus).items():
                if len(members) < 2:
                    continue
                member_wkts = [idu_to_p[i]["geom_wkt"] for i in members]
                uf_metrics = _union_metrics(member_wkts, cx, cy)
                surface_ha_uf = round((uf_metrics["surface_ha"] if uf_metrics else 0.0), 4)
                if surface_ha_uf < min_area:
                    n_area_skip += 1
                    continue
                if len(members) > cap:
                    n_cap_skip += 1
                    continue

                cluster_idx += 1
                n_uf += 1
                uf_id = f"{siren}_{cluster_idx:03d}"
                nb = len(members)
                by_size[nb] += 1

                for idu in members:
                    p = idu_to_p[idu]
                    uf_rows.append({
                        "project_id": project_id,
                        "uf_id": uf_id,
                        "siren": siren,
                        "denomination": denom,
                        "forme_juridique": forme,
                        "nb_parcelles": nb,
                        "surface_ha_uf": surface_ha_uf,
                        "idu": idu,
                        "surface_ha": round(float(p.get("area_m2") or 0) / 10_000, 4),
                        "geom_wkt": p["geom_wkt"],
                    })

                adj = _adj_from_pairs(members, pairs)
                subsets = get_contiguous_subsets(adj, members, MAX_K)
                surf_map = {
                    i: float(idu_to_p[i].get("area_m2") or 0) / 10_000
                    for i in members
                }
                for i_s, s in enumerate(subsets):
                    if sum(surf_map.get(idu, 0) for idu in s) < min_area:
                        n_prefilter_skip += 1
                        continue
                    idus_list = list(s)
                    metrics = _union_metrics(
                        [idu_to_p[i]["geom_wkt"] for i in idus_list], cx, cy
                    )
                    if metrics is None:
                        continue
                    ss_rows.append({
                        "project_id": project_id,
                        "uf_id": uf_id,
                        "subset_id": f"{uf_id}_{i_s:04d}",
                        "k": len(idus_list),
                        "idus": idus_list,
                        "surface_ha": round(metrics["surface_ha"], 4),
                        "geom_wkt": metrics["geom_wkt"],
                        "miller": round(metrics["miller"], 6),
                        "dist_centre_m": round(metrics["dist_centre_m"], 1),
                        "denomination": denom or None,
                        "siren": siren or None,
                    })

    log(
        f"PHASE:unites_foncieres:done:{len(uf_rows)} "
        f"({n_uf} UF, {time.perf_counter() - t_clust:.1f}s)"
    )
    log(f"   cap >{cap}p ignorées : {n_cap_skip}  |  surface <{min_area} ha : {n_area_skip}")
    for k in sorted(by_size):
        log(f"   {k:>4} parcelles : {by_size[k]:>5} UF")

    sql_uf = """
        INSERT INTO ecocompensation_results.unites_foncieres (
            project_id, uf_id, siren, denomination, forme_juridique,
            nb_parcelles, surface_ha_uf, idu, surface_ha, geom_2154
        ) VALUES (
            :project_id, :uf_id, :siren, :denomination, :forme_juridique,
            :nb_parcelles, :surface_ha_uf, :idu, :surface_ha,
            ST_GeomFromText(:geom_wkt, 2154)
        )
    """
    sql_ss = """
        INSERT INTO ecocompensation_results.sous_ensembles
            (project_id, uf_id, subset_id, k, idus,
             surface_ha, geom_2154, miller, dist_centre_m,
             denomination, siren)
        VALUES
            (:project_id, :uf_id, :subset_id, :k, :idus,
             :surface_ha, ST_GeomFromText(:geom_wkt, 2154),
             :miller, :dist_centre_m,
             :denomination, :siren)
    """

    t_ins = time.perf_counter()
    with engine_core.begin() as conn:
        if uf_rows:
            for i in range(0, len(uf_rows), BATCH_UF):
                conn.execute(text(sql_uf), uf_rows[i : i + BATCH_UF])
        if ss_rows:
            # execute() par ligne — évite executemany / pipeline psycopg3 (pgBouncer :6543)
            for i in range(0, len(ss_rows), BATCH_SS):
                chunk = ss_rows[i : i + BATCH_SS]
                for row in chunk:
                    conn.execute(text(sql_ss), row)
                log(
                    f"TILE_PROGRESS:{min(i + BATCH_SS, len(ss_rows))}"
                    f"/{len(ss_rows)}:{len(ss_rows)}"
                )

    log(f"PHASE:sous_ensembles:done:{len(ss_rows)}")
    log(
        f"aoi_to_sub_uf : {len(ss_rows):,} sous-ensembles, {len(uf_rows):,} lignes UF "
        f"en {time.perf_counter() - t0:.1f}s (insert {time.perf_counter() - t_ins:.1f}s)"
    )
    log(f"   Ignorés pré-filtre surface (<{min_area} ha) : {n_prefilter_skip:,}")
    return len(ss_rows)


def main():
    import argparse

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    from db import get_engine, get_engine_ppm

    engine_core = get_engine()
    engine_ppm = get_engine_ppm()

    parser = argparse.ArgumentParser(
        description="AOI → sous-ensembles UF (personnes morales)."
    )
    parser.add_argument("--aoi", help="UUID de l'AOI ou du projet")
    parser.add_argument("--list", action="store_true", help="Lister les projets")
    parser.add_argument("--cap", type=int, default=MAX_UF_PARCELLES)
    parser.add_argument("--min-area-ha", type=float, default=MIN_AREA_HA_PREFILTER)
    args = parser.parse_args()

    with engine_core.begin() as conn:
        if args.list:
            rows = conn.execute(text("""
                SELECT p.id AS project_id, p.name, p.aoi_id, p.created_at
                FROM ecocompensation.projects p
                WHERE p.aoi_id IS NOT NULL
                ORDER BY p.created_at DESC
                LIMIT 20
            """)).mappings().all()
            print(f"{'Projet':<40} {'AOI id':<40} Nom")
            print("─" * 100)
            for r in rows:
                print(f"{str(r['project_id']):<40} {str(r['aoi_id']):<40} {r['name']}")
            return

        if args.aoi:
            row = conn.execute(text("""
                SELECT p.id AS project_id, p.aoi_id
                FROM ecocompensation.projects p
                WHERE (p.aoi_id = :aid OR p.id = :aid) AND p.aoi_id IS NOT NULL
                LIMIT 1
            """), {"aid": args.aoi}).mappings().one_or_none()
            if not row:
                print(f"Aucun projet pour {args.aoi}")
                return
        else:
            row = conn.execute(text("""
                SELECT p.id AS project_id, p.aoi_id
                FROM ecocompensation.projects p
                WHERE p.aoi_id IS NOT NULL
                ORDER BY p.created_at DESC
                LIMIT 1
            """)).mappings().one_or_none()
            if not row:
                print("Aucun projet avec AOI.")
                return

    project_id = str(row["project_id"])
    aoi_id = str(row["aoi_id"])
    print(f"Projet {project_id}  AOI {aoi_id}  cap≤{args.cap}  ≥{args.min_area_ha} ha")
    n = run(
        engine_core, project_id, aoi_id, cb=print,
        engine_ppm=engine_ppm,
        min_area_ha=args.min_area_ha,
        max_uf_parcelles=args.cap,
    )
    print(f"Sous-ensembles insérés : {n:,}")


if __name__ == "__main__":
    main()
