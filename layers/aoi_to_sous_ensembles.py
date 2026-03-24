#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aoi_to_sous_ensembles.py
========================

Calcule et stocke tous les sous-ensembles contigus (k=2..MAX_K) des unités
foncières dans ecocompensation_results.sous_ensembles.

Pré-requis : aoi_to_unites_foncieres doit avoir tourné pour ce project_id.

Signature compatible layer_runner :
    run(engine, project_id, aoi_id, cb=None) -> int

Note : engine_ppm n'est pas nécessaire ici — on lit uniquement depuis
ecocompensation_results.unites_foncieres (base core), les géométries
individuelles y sont déjà stockées.
"""

from __future__ import annotations

import os
import sys
import time
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import text

# ─────────────────────────────────────────────
# Paramètres
# ─────────────────────────────────────────────

MAX_K                = 5
MAX_UF_PARCELLES     = 10
MIN_AREA_HA_PREFILTER = 7.0   # même seuil que le filtre final
BATCH_INSERT         = 200

DDL = """
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


# ─────────────────────────────────────────────
# Énumération sous-ensembles contigus (DFS)
# ─────────────────────────────────────────────

def get_contiguous_subsets(adj: dict, nodes: list, max_k: int) -> list[frozenset]:
    results: set[frozenset] = set()

    def dfs(current: frozenset, frontier: set):
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


def build_adjacency(conn, project_id: str, uf_id: str, members: list[dict]) -> dict:
    """Graphe de contigüité depuis les géométries stockées dans unites_foncieres."""
    adj = {m["idu"]: set() for m in members}
    if len(members) <= 1:
        return adj
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


def compute_union_metrics_batch(
    conn, project_id: str, subsets: list[tuple[str, list[str]]], cx: float, cy: float
) -> list[dict]:
    """
    Calcule ST_Union + surface_ha + miller + dist_centre_m en une requête
    pour tous les sous-ensembles d'une UF via VALUES clause.
    """
    if not subsets:
        return []

    values_parts = []
    params: dict = {"pid": project_id, "cx": cx, "cy": cy}
    for idx, (sid, idus) in enumerate(subsets):
        pg_array = "{" + ",".join(idus) + "}"
        values_parts.append(f"(:sid_{idx}, :idus_{idx})")
        params[f"sid_{idx}"]  = sid
        params[f"idus_{idx}"] = pg_array

    values_sql = ", ".join(values_parts)
    rows = conn.execute(
        text(f"""
            WITH input(subset_id, idus) AS (VALUES {values_sql}),
            unions AS (
                SELECT i.subset_id, ST_Union(u.geom_2154) AS geom
                FROM input i
                JOIN ecocompensation_results.unites_foncieres u
                  ON u.project_id = :pid
                 AND u.idu = ANY(CAST(i.idus AS text[]))
                GROUP BY i.subset_id
            )
            SELECT
                subset_id,
                ST_AsText(geom)                                             AS geom_wkt,
                ST_Area(geom) / 10000.0                                     AS surface_ha,
                4 * pi() * ST_Area(geom) / NULLIF(ST_Perimeter(geom)^2, 0) AS miller,
                ST_Distance(
                    ST_Centroid(geom),
                    ST_SetSRID(ST_MakePoint(:cx, :cy), 2154)
                )                                                           AS dist_centre_m
            FROM unions
        """),
        params,
    ).mappings().all()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# run()
# ─────────────────────────────────────────────

def run(engine, project_id: str, aoi_id: str, cb=None) -> int:
    """
    Calcule et stocke les sous-ensembles contigus pour le projet.

    :param engine:      Engine base principale (core) — seule base nécessaire.
    :param project_id:  UUID du projet.
    :param aoi_id:      UUID de l'AOI (utilisé pour récupérer le centre).
    :param cb:          Callback log optionnel.
    :return:            Nombre de sous-ensembles insérés.
    """
    log = cb or print
    t0  = time.perf_counter()

    # ── Vérification dépendance ──────────────────────────────────────────
    with engine.begin() as conn:
        n_uf = conn.execute(
            text("SELECT COUNT(*) FROM ecocompensation_results.unites_foncieres WHERE project_id = :pid"),
            {"pid": project_id},
        ).scalar_one()

    if n_uf == 0:
        raise RuntimeError(
            f"unites_foncieres vide pour project_id={project_id}. "
            "Lance d'abord aoi_to_unites_foncieres."
        )
    log(f"✅ {n_uf:,} lignes unites_foncieres trouvées — dépendance OK")

    # ── DDL ──────────────────────────────────────────────────────────────
    with engine.begin() as conn:
        conn.execute(text(DDL))
    log("✅ Table sous_ensembles prête (colonnes PM incluses).")

    # ── Purge ────────────────────────────────────────────────────────────
    with engine.begin() as conn:
        deleted = conn.execute(
            text("DELETE FROM ecocompensation_results.sous_ensembles WHERE project_id = :pid"),
            {"pid": project_id},
        ).rowcount
    if deleted:
        log(f"🧹 {deleted} anciens sous-ensembles supprimés.")

    # ── Centre AOI ───────────────────────────────────────────────────────
    with engine.begin() as conn:
        row = conn.execute(
            text("""
                SELECT ST_X(ST_Centroid(geom_2154)) AS cx,
                       ST_Y(ST_Centroid(geom_2154)) AS cy
                FROM ecocompensation.aoi WHERE id = :aid
            """),
            {"aid": aoi_id},
        ).one_or_none()
    if not row:
        raise RuntimeError(f"AOI introuvable pour aoi_id={aoi_id}")
    cx, cy = float(row[0]), float(row[1])
    log(f"   Centre AOI : x={cx:.0f}, y={cy:.0f}")

    # ── Chargement membres UF ────────────────────────────────────────────
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT uf_id, idu, surface_ha, siren, denomination
            FROM ecocompensation_results.unites_foncieres
            WHERE project_id = :pid
        """), {"pid": project_id}).mappings().all()

    by_uf: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_uf[r["uf_id"]].append(
            {
                "idu": r["idu"],
                "surface_ha": float(r["surface_ha"] or 0),
                "siren": (r.get("siren") or "") or "",
                "denomination": (r.get("denomination") or "") or "",
            }
        )

    uf_qualifiees = {
        uf_id: members for uf_id, members in by_uf.items()
        if 2 <= len(members) <= MAX_UF_PARCELLES
    }
    n_cap = sum(1 for m in by_uf.values() if len(m) > MAX_UF_PARCELLES)
    log(f"📦 {len(by_uf)} UF → {len(uf_qualifiees)} retenues (cap ≤ {MAX_UF_PARCELLES}p) | {n_cap} ignorées")

    # ── Boucle principale ────────────────────────────────────────────────
    total_inserted   = 0
    total_uf         = len(uf_qualifiees)
    n_uf_done        = 0
    n_prefilter_skip = 0

    with engine.begin() as conn:
        insert_buffer: list[dict] = []

        def flush():
            nonlocal total_inserted
            if not insert_buffer:
                return
            conn.execute(text("""
                INSERT INTO ecocompensation_results.sous_ensembles
                    (project_id, uf_id, subset_id, k, idus,
                     surface_ha, geom_2154, miller, dist_centre_m,
                     denomination, siren)
                VALUES
                    (:project_id, :uf_id, :subset_id, :k, :idus,
                     :surface_ha, ST_GeomFromText(:geom_wkt, 2154),
                     :miller, :dist_centre_m,
                     :denomination, :siren)
            """), insert_buffer)
            total_inserted += len(insert_buffer)
            insert_buffer.clear()

        for uf_id, members in uf_qualifiees.items():
            n_uf_done += 1
            # PM : identique pour toutes les parcelles de l’UF
            pm_denom = (members[0].get("denomination") or "") or ""
            pm_siren = (members[0].get("siren") or "") or ""
            adj      = build_adjacency(conn, project_id, uf_id, members)
            idus     = [m["idu"] for m in members]
            subsets  = get_contiguous_subsets(adj, idus, MAX_K)
            surf_map = {m["idu"]: m["surface_ha"] for m in members}

            subsets_ok = []
            for i_s, s in enumerate(subsets):
                surf = sum(surf_map.get(idu, 0) for idu in s)
                if surf < MIN_AREA_HA_PREFILTER:
                    n_prefilter_skip += 1
                    continue
                sid = f"{uf_id}_{i_s:04d}"
                subsets_ok.append((sid, list(s), len(s)))

            if not subsets_ok:
                continue

            metrics = compute_union_metrics_batch(
                conn, project_id,
                [(sid, idus_list) for sid, idus_list, _ in subsets_ok],
                cx, cy,
            )
            m_by_sid = {r["subset_id"]: r for r in metrics}

            for sid, idus_list, k in subsets_ok:
                m = m_by_sid.get(sid)
                if m is None or m["geom_wkt"] is None:
                    continue
                insert_buffer.append({
                    "project_id":    project_id,
                    "uf_id":         uf_id,
                    "subset_id":     sid,
                    "k":             k,
                    "idus":          idus_list,
                    "surface_ha":    round(float(m["surface_ha"] or 0), 4),
                    "geom_wkt":      m["geom_wkt"],
                    "miller":        round(float(m["miller"] or 0), 6),
                    "dist_centre_m": round(float(m["dist_centre_m"] or 0), 1),
                    "denomination":  pm_denom or None,
                    "siren":         pm_siren or None,
                })

            if len(insert_buffer) >= BATCH_INSERT:
                flush()

            if n_uf_done % 50 == 0 or n_uf_done == total_uf:
                elapsed = time.perf_counter() - t0
                rate    = n_uf_done / elapsed if elapsed > 0 else 1
                eta     = (total_uf - n_uf_done) / rate
                sys.stdout.write(
                    f"\r   [{n_uf_done:>4}/{total_uf}]  "
                    f"insérés: {total_inserted + len(insert_buffer):>6,}  "
                    f"ETA: ~{eta:.0f}s    "
                )
                sys.stdout.flush()

        flush()

    print()
    elapsed = time.perf_counter() - t0
    log(f"\n✅ {total_inserted:,} sous-ensembles insérés en {elapsed:.1f}s")
    log(f"   Ignorés pré-filtre surface (<{MIN_AREA_HA_PREFILTER} ha) : {n_prefilter_skip:,}")
    return total_inserted


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def main():
    load_dotenv(Path(__file__).parent / ".env")

    from db import get_engine
    engine = get_engine()

    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT id, aoi_id FROM ecocompensation.projects WHERE aoi_id IS NOT NULL ORDER BY created_at DESC LIMIT 1")
        ).mappings().one_or_none()

    if not row:
        print("Aucun projet avec AOI trouvé.")
        return

    project_id = str(row["id"])
    aoi_id     = str(row["aoi_id"])
    print(f"🔗 Projet : {project_id} | AOI : {aoi_id}")
    print(f"   MAX_K={MAX_K}  |  cap≤{MAX_UF_PARCELLES}p  |  pré-filtre≥{MIN_AREA_HA_PREFILTER}ha\n")

    n = run(engine, project_id, aoi_id, cb=print)
    print(f"Total insérés : {n:,}")


if __name__ == "__main__":
    main()