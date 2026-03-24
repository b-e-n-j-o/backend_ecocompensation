#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aoi_to_unites_foncieres.py
==========================

Construit ecocompensation_results.unites_foncieres à partir de
public.parcelles_personnes_morales intersectée avec l'AOI du projet.

Signature compatible layer_runner :
    run(engine_core, project_id, aoi_id, cb=None, *, engine_ppm=None) -> int

Une ligne par parcelle membre — géométrie individuelle conservée.
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import text

logger = logging.getLogger(__name__)

PARCELLES_PM_TABLE = os.getenv("PARCELLES_PM_TABLE", "public.parcelles_personnes_morales")


# ─────────────────────────────────────────────
# Union-Find
# ─────────────────────────────────────────────

class UnionFind:
    def __init__(self):
        self._parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        if x not in self._parent:
            self._parent[x] = x
        if self._parent[x] != x:
            self._parent[x] = self.find(self._parent[x])
        return self._parent[x]

    def union(self, a: str, b: str):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra

    def clusters(self, nodes: list[str]) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = defaultdict(list)
        for n in nodes:
            groups[self.find(n)].append(n)
        return dict(groups)


# ─────────────────────────────────────────────
# Helpers DB
# ─────────────────────────────────────────────

def _get_aoi_geom_wkt(conn_core, aoi_id: str) -> str:
    row = conn_core.execute(
        text("SELECT ST_AsText(geom_2154) FROM ecocompensation.aoi WHERE id = :aid"),
        {"aid": aoi_id},
    ).one_or_none()
    if not row or not row[0]:
        raise RuntimeError(f"AOI introuvable pour aoi_id={aoi_id}")
    return str(row[0])


def _count_ppm_in_aoi(conn_ppm, aoi_wkt: str) -> tuple[int, int]:
    row = conn_ppm.execute(
        text(f"""
            SELECT
                COUNT(*) FILTER (
                    WHERE geom_2154 && ST_GeomFromText(:w, 2154)
                      AND ST_Intersects(geom_2154, ST_GeomFromText(:w, 2154))
                      AND siren IS NOT NULL AND siren != '' AND geom_2154 IS NOT NULL
                ) AS total,
                COUNT(*) FILTER (
                    WHERE code_insee LIKE '33%'
                      AND geom_2154 && ST_GeomFromText(:w, 2154)
                      AND ST_Intersects(geom_2154, ST_GeomFromText(:w, 2154))
                      AND siren IS NOT NULL AND siren != '' AND geom_2154 IS NOT NULL
                ) AS kept
            FROM {PARCELLES_PM_TABLE}
        """),
        {"w": aoi_wkt},
    ).one()
    return int(row[0] or 0), int(row[1] or 0)


def _fetch_ppm_in_aoi(conn_ppm, aoi_wkt: str) -> list[dict]:
    rows = conn_ppm.execute(
        text(f"""
            SELECT
                idu, siren, denomination, forme_juridique,
                ST_Area(geom_2154)   AS area_m2,
                ST_AsText(geom_2154) AS geom_wkt
            FROM {PARCELLES_PM_TABLE}
            WHERE code_insee LIKE '33%'
              AND geom_2154 && ST_GeomFromText(:w, 2154)
              AND ST_Intersects(geom_2154, ST_GeomFromText(:w, 2154))
              AND siren IS NOT NULL AND siren != ''
              AND geom_2154 IS NOT NULL
        """),
        {"w": aoi_wkt},
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


def _compute_union_surface(conn_ppm, idus: list[str]) -> float:
    row = conn_ppm.execute(
        text(f"""
            SELECT ST_Area(ST_Union(geom_2154))
            FROM {PARCELLES_PM_TABLE}
            WHERE idu = ANY(:idus)
        """),
        {"idus": idus},
    ).scalar()
    return float(row or 0.0)


# ─────────────────────────────────────────────
# run()
# ─────────────────────────────────────────────

def run(
    engine_core,
    project_id: str,
    aoi_id: str,
    cb=None,
    *,
    engine_ppm=None,
) -> int:
    """
    Construit ecocompensation_results.unites_foncieres.

    :param engine_core: Engine base principale.
    :param project_id:  UUID du projet.
    :param aoi_id:      UUID de l'AOI — clé centrale de toutes les couches.
    :param cb:          Callback log optionnel cb(str).
    :param engine_ppm:  Engine base PPM (fallback sur engine_core si None).
    :return:            Nombre de lignes insérées.
    """
    log = cb or logger.info
    engine_ppm = engine_ppm or engine_core
    t0 = time.perf_counter()

    # 1. Purge lignes existantes pour ce projet
    with engine_core.begin() as conn:
        deleted = conn.execute(
            text("DELETE FROM ecocompensation_results.unites_foncieres WHERE project_id = :pid"),
            {"pid": project_id},
        ).rowcount
    if deleted:
        log(f"🧹 {deleted} lignes UF supprimées (project_id={project_id})")

    # 2. Géométrie AOI
    with engine_core.begin() as conn:
        aoi_wkt = _get_aoi_geom_wkt(conn, aoi_id)

    # 3. Diagnostique
    log("🔍 Comptage PPM dans l'AOI...")
    with engine_ppm.begin() as conn:
        total, kept = _count_ppm_in_aoi(conn, aoi_wkt)
    log(f"   → sans filtre INSEE : {total:,}  |  code_insee 33% : {kept:,}")

    # 4. Fetch parcelles PM avec géométries individuelles
    log("🔍 Chargement parcelles PM...")
    t_fetch = time.perf_counter()
    with engine_ppm.begin() as conn:
        parcelles = _fetch_ppm_in_aoi(conn, aoi_wkt)
    log(f"   → {len(parcelles):,} parcelles en {time.perf_counter() - t_fetch:.1f}s")

    if not parcelles:
        log("⚠️ Aucune parcelle PM dans l'AOI.")
        return 0

    # 5. Grouper par SIREN (≥ 2 parcelles seulement)
    by_siren: dict[str, list[dict]] = defaultdict(list)
    for p in parcelles:
        by_siren[p["siren"]].append(p)
    sirens_multi = {s: ps for s, ps in by_siren.items() if len(ps) >= 2}
    log(f"   → {len(by_siren):,} SIREN  |  {len(sirens_multi):,} avec ≥ 2 parcelles")

    if not sirens_multi:
        log("⚠️ Aucun SIREN multi-parcelles.")
        return 0

    # 6. Clustering par contigüité + collecte lignes à insérer
    log("🔗 Clustering contigu par SIREN...")
    t_clust = time.perf_counter()
    all_rows: list[dict] = []
    by_size: dict[int, int] = defaultdict(int)

    with engine_ppm.begin() as conn_ppm:
        for siren, ps in sirens_multi.items():
            idus      = [p["idu"] for p in ps]
            denom     = ps[0].get("denomination") or ""
            forme     = ps[0].get("forme_juridique") or ""
            idu_to_p  = {p["idu"]: p for p in ps}
            pairs     = _find_touching_pairs(conn_ppm, idus)

            uf = UnionFind()
            for idu in idus:
                uf.find(idu)
            for a, b in pairs:
                uf.union(a, b)

            cluster_idx = 0
            for root, members in uf.clusters(idus).items():
                if len(members) < 2:
                    continue
                cluster_idx += 1
                uf_id        = f"{siren}_{cluster_idx:03d}"
                nb           = len(members)
                surface_ha_uf = round(_compute_union_surface(conn_ppm, members) / 10_000, 4)
                by_size[nb] += 1

                for idu in members:
                    p = idu_to_p[idu]
                    all_rows.append({
                        "project_id":      project_id,
                        "uf_id":           uf_id,
                        "siren":           siren,
                        "denomination":    denom,
                        "forme_juridique": forme,
                        "nb_parcelles":    nb,
                        "surface_ha_uf":   surface_ha_uf,
                        "idu":             idu,
                        "surface_ha":      round(float(p.get("area_m2") or 0) / 10_000, 4),
                        "geom_wkt":        p["geom_wkt"],
                    })

    nb_ufs = sum(by_size.values())
    log(f"   → {nb_ufs} UF  |  {len(all_rows)} lignes  |  {time.perf_counter() - t_clust:.1f}s")

    # 7. Insertion batch (500 lignes par transaction)
    log("💾 Insertion dans ecocompensation_results.unites_foncieres...")
    t_ins   = time.perf_counter()
    BATCH   = 500
    inserted = 0
    with engine_core.begin() as conn:
        for i in range(0, len(all_rows), BATCH):
            conn.execute(
                text("""
                    INSERT INTO ecocompensation_results.unites_foncieres (
                        project_id, uf_id, siren, denomination, forme_juridique,
                        nb_parcelles, surface_ha_uf, idu, surface_ha, geom_2154
                    ) VALUES (
                        :project_id, :uf_id, :siren, :denomination, :forme_juridique,
                        :nb_parcelles, :surface_ha_uf, :idu, :surface_ha,
                        ST_GeomFromText(:geom_wkt, 2154)
                    )
                """),
                all_rows[i:i + BATCH],
            )
            inserted += len(all_rows[i:i + BATCH])

    log(f"   → {inserted:,} lignes insérées en {time.perf_counter() - t_ins:.1f}s")
    log(f"✅ aoi_to_unites_foncieres terminé en {time.perf_counter() - t0:.1f}s total")

    log("\n📊 Répartition UF par nb parcelles :")
    for k in sorted(by_size):
        log(f"   {k:>4} parcelles : {by_size[k]:>5} UF")

    return inserted


# ─────────────────────────────────────────────
# CLI (usage direct, hors layer_runner)
# ─────────────────────────────────────────────

def main():
    BASE_DIR = Path(__file__).resolve().parent
    load_dotenv(BASE_DIR / ".env")

    from db import get_engine, get_engine_ppm
    engine_core = get_engine()
    engine_ppm  = get_engine_ppm()

    with engine_core.begin() as conn:
        row = conn.execute(
            text("SELECT id, aoi_id FROM ecocompensation.projects WHERE aoi_id IS NOT NULL ORDER BY created_at DESC LIMIT 1")
        ).mappings().one_or_none()

    if not row:
        print("Aucun projet avec AOI trouvé.")
        return

    project_id = str(row["id"])
    aoi_id     = str(row["aoi_id"])
    print(f"🔗 Projet : {project_id} | AOI : {aoi_id}")

    n = run(engine_core, project_id, aoi_id, cb=print, engine_ppm=engine_ppm)
    print(f"\nTotal lignes insérées : {n}")


if __name__ == "__main__":
    main()