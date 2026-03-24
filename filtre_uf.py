#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
filtre_uf.py
============

Filtre les sous-ensembles d'unités foncières stockés dans
ecocompensation_results.sous_ensembles, en appliquant les mêmes critères
que vrai_filtre.py sur les parcelles seules.

Pré-requis :
  - aoi_to_unites_foncieres.py  doit avoir tourné
  - aoi_to_sous_ensembles.py    doit avoir tourné

Les colonnes de sous_ensembles utilisées :
  - geom_2154      : géométrie union précalculée
  - surface_ha     : surface union en ha
  - miller         : coefficient de Miller
  - dist_centre_m  : distance au centre AOI
  - subset_id      : identifiant unique
  - uf_id          : identifiant de l'UF parente
  - idus           : liste des IDU membres
  - k              : nombre de parcelles du sous-ensemble

Filtres :
  0. Bruts
  1. Exclusion GEOMCE
  2. Exclusion patrimoine naturel (Natura 2000)
  3. Surface ≥ min_area_ha (pré-filtré à la création)
  4. Miller ≥ miller_threshold
  5. ZDV intersecte
  6. Tronçon hydro intersecte / within_radius
  7. Surface hydro intersecte / within_radius
  8. Distance informative (pas de filtre éliminatoire)
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from sqlalchemy import text

from db import get_engine

# ─────────────────────────────────────────────
# Paramètres
# ─────────────────────────────────────────────

PROJECT_ID = "54987c59-ad94-46b2-9f20-aa679dbcf3a1"

TARGET_COUNT    = 50
RADIUS_START_KM = 10.0
RADIUS_MIN_KM   = 1.0

HydroMode = Literal["none", "intersect", "within_radius"]


@dataclass
class FiltreOptions:
    zdv_natures:            list[str]
    troncon_hydro_mode:     HydroMode
    troncon_hydro_radius_m: float
    surface_hydro_mode:     HydroMode
    surface_hydro_radius_m: float

    @staticmethod
    def defaut() -> "FiltreOptions":
        return FiltreOptions(
            zdv_natures=["Forêt ouverte"],
            troncon_hydro_mode="intersect",
            troncon_hydro_radius_m=500.0,
            surface_hydro_mode="within_radius",
            surface_hydro_radius_m=500.0,
        )


# ─────────────────────────────────────────────
# run()
# ─────────────────────────────────────────────

def run(
    engine,
    project_id: str,
    aoi_id: str,
    cx: float,
    cy: float,
    options: FiltreOptions,
    *,
    return_results: bool = False,
    miller_threshold: float = 0.39,
    min_area_ha: float = 7.0,
    target_count: int = TARGET_COUNT,
    radius_start_km: float = RADIUS_START_KM,
    radius_min_km: float = RADIUS_MIN_KM,
):
    funnel: list[dict] = []

    tables_to_check = [
        "ecocompensation_results.sous_ensembles",
        "ecocompensation_results.mesures_compensatoire_surf",
        "ecocompensation_results.mesures_compensatoire_lin",
        "ecocompensation_results.mesures_compensatoire_pct",
        "ecocompensation_results.mesures_compensatoire_commune",
        "ecocompensation_results.patrimoine_naturel",
        "ecocompensation_results.zone_de_vegetation",
        "ecocompensation_results.troncons_hydro",
        "ecocompensation_results.surfaces_hydro",
    ]
    exists: dict[str, bool] = {}
    with engine.begin() as conn:
        for t in tables_to_check:
            exists[t] = conn.execute(
                text("SELECT to_regclass(:r) IS NOT NULL").execution_options(no_prepare=True),
                {"r": t},
            ).scalar_one()

    def has(table: str) -> bool:
        return exists.get(table, False)

    if not has("ecocompensation_results.sous_ensembles"):
        print("❌ Table sous_ensembles introuvable. Lance d'abord aoi_to_sous_ensembles.py.")
        return None if not return_results else ([], 0.0, funnel)

    where_clauses = ["s.project_id = :project_id"]
    params = {
        "project_id":             project_id,
        "aoi_id_str":             aoi_id,
        "cx":                     cx,
        "cy":                     cy,
        "radius_m":               0.0,
        "min_area_ha":            min_area_ha,
        "miller_th":              miller_threshold,
        "zdv_natures":            options.zdv_natures,
        "troncon_hydro_radius_m": options.troncon_hydro_radius_m,
        "surface_hydro_radius_m": options.surface_hydro_radius_m,
    }

    def log(msg: str) -> None:
        if not return_results:
            print(msg)

    def count_with_clauses(clauses: list[str], extra_params: dict | None = None) -> int:
        where_sql = " AND ".join(f"({c})" for c in clauses)
        p = {**params, **(extra_params or {})}
        with engine.begin() as conn:
            return conn.execute(
                text(f"SELECT COUNT(*) FROM ecocompensation_results.sous_ensembles s WHERE {where_sql}"),
                p,
            ).scalar_one()

    # 0) Bruts
    n0 = count_with_clauses(where_clauses)
    log(f"0) Sous-ensembles bruts                             → {n0}")
    funnel.append({"step": 0, "label": "Sous-ensembles bruts", "count": n0})
    if n0 == 0:
        log("⚠️ Aucun sous-ensemble. Lance aoi_to_sous_ensembles.py.")
        return None if not return_results else ([], 0.0, funnel)

    # 1) GEOMCE
    geomce_tables = [
        ("ecocompensation_results.mesures_compensatoire_surf",    "ms"),
        ("ecocompensation_results.mesures_compensatoire_lin",     "ml"),
        ("ecocompensation_results.mesures_compensatoire_pct",     "mp"),
        ("ecocompensation_results.mesures_compensatoire_commune", "mc"),
    ]
    for table, alias in geomce_tables:
        if has(table):
            where_clauses.append(f"""
                NOT EXISTS (
                    SELECT 1 FROM {table} {alias}
                    WHERE {alias}.project_id = :project_id
                      AND s.geom_2154 && {alias}.geom_2154
                      AND ST_Intersects(s.geom_2154, {alias}.geom_2154)
                )
            """)
    n1 = count_with_clauses(where_clauses)
    log(f"1) Exclusion GEOMCE                                 → {n1}")
    funnel.append({"step": 1, "label": "Après exclusion GEOMCE", "count": n1})

    # 2) Patrimoine naturel (Natura 2000)
    if has("ecocompensation_results.patrimoine_naturel"):
        where_clauses.append("""
            NOT EXISTS (
                SELECT 1 FROM ecocompensation_results.patrimoine_naturel pn
                WHERE pn.project_id = :project_id
                  AND pn.type_patrimoine = 'Natura 2000'
                  AND s.geom_2154 && pn.geom_2154
                  AND ST_Intersects(s.geom_2154, pn.geom_2154)
            )
        """)
    n2 = count_with_clauses(where_clauses)
    log(f"2) Exclusion patrimoine (Natura 2000)               → {n2}")
    funnel.append({"step": 2, "label": "Après exclusion patrimoine Natura 2000", "count": n2})

    # 3) Surface — informatif (déjà pré-filtré à la création)
    n3 = count_with_clauses(where_clauses)
    log(f"3) Surface ≥ {min_area_ha} ha (pré-filtré)                → {n3} (informatif)")
    funnel.append({"step": 3, "label": f"Surface ≥ {min_area_ha} ha (pré-filtré)", "count": n3})

    # 4) Miller
    miller_clause = "s.miller >= :miller_th"
    n4 = count_with_clauses(where_clauses + [miller_clause])
    log(f"4) Miller ≥ {miller_threshold}                             → {n4}")
    funnel.append({"step": 4, "label": f"Après Miller ≥ {miller_threshold}", "count": n4})
    if n4 == 0:
        log("⚠️ Aucun sous-ensemble après Miller, arrêt.")
        return None if not return_results else ([], 0.0, funnel)
    numeric_clauses = [miller_clause]

    # 5) ZDV
    if options.zdv_natures and has("ecocompensation_results.zone_de_vegetation"):
        where_clauses.append("""
            EXISTS (
                SELECT 1 FROM ecocompensation_results.zone_de_vegetation z
                WHERE (z.project_id = :project_id OR z.aoi_id = :aoi_id_str)
                  AND s.geom_2154 && z.geom_2154
                  AND ST_Intersects(s.geom_2154, z.geom_2154)
                  AND z.nature = ANY(:zdv_natures)
            )
        """)
        n5 = count_with_clauses(where_clauses + numeric_clauses)
        log(f"5) ZDV intersecte {options.zdv_natures}      → {n5}")
    else:
        n5 = count_with_clauses(where_clauses + numeric_clauses)
        log(f"5) ZDV (ignorée ou table absente)                    → {n5}")
    funnel.append({"step": 5, "label": "Après filtre ZDV", "count": n5})

    # 6) Tronçon hydro
    if options.troncon_hydro_mode != "none" and has("ecocompensation_results.troncons_hydro"):
        if options.troncon_hydro_mode == "intersect":
            where_clauses.append("""
                EXISTS (
                    SELECT 1 FROM ecocompensation_results.troncons_hydro t
                    WHERE (t.project_id = :project_id OR t.aoi_id = :aoi_id_str)
                      AND s.geom_2154 && t.geom_2154
                      AND ST_Intersects(s.geom_2154, t.geom_2154)
                )
            """)
            label6 = "6) Tronçon hydro intersecte"
        else:
            where_clauses.append("""
                EXISTS (
                    SELECT 1 FROM ecocompensation_results.troncons_hydro t
                    WHERE (t.project_id = :project_id OR t.aoi_id = :aoi_id_str)
                      AND ST_DWithin(s.geom_2154, t.geom_2154, :troncon_hydro_radius_m)
                )
            """)
            label6 = f"6) Tronçon hydro ≤ {options.troncon_hydro_radius_m:.0f} m"
        n6 = count_with_clauses(where_clauses + numeric_clauses)
        log(f"{label6:<55} → {n6}")
    else:
        n6 = count_with_clauses(where_clauses + numeric_clauses)
        log(f"6) Tronçon hydro (ignoré ou table absente)           → {n6}")
    funnel.append({"step": 6, "label": "Après filtre tronçon hydro", "count": n6})

    # 7) Surface hydro
    if options.surface_hydro_mode != "none" and has("ecocompensation_results.surfaces_hydro"):
        if options.surface_hydro_mode == "intersect":
            where_clauses.append("""
                EXISTS (
                    SELECT 1 FROM ecocompensation_results.surfaces_hydro sh
                    WHERE (sh.project_id = :project_id OR sh.aoi_id = :aoi_id_str)
                      AND s.geom_2154 && sh.geom_2154
                      AND ST_Intersects(s.geom_2154, sh.geom_2154)
                )
            """)
            label7 = "7) Surface hydro intersecte"
        else:
            where_clauses.append("""
                EXISTS (
                    SELECT 1 FROM ecocompensation_results.surfaces_hydro sh
                    WHERE (sh.project_id = :project_id OR sh.aoi_id = :aoi_id_str)
                      AND ST_DWithin(s.geom_2154, sh.geom_2154, :surface_hydro_radius_m)
                )
            """)
            label7 = f"7) Surface hydro ≤ {options.surface_hydro_radius_m:.0f} m"
        n7 = count_with_clauses(where_clauses + numeric_clauses)
        log(f"{label7:<55} → {n7}")
    else:
        n7 = count_with_clauses(where_clauses + numeric_clauses)
        log(f"7) Surface hydro (ignorée ou table absente)          → {n7}")
    funnel.append({"step": 7, "label": "Après filtre surface hydro", "count": n7})

    # 8) Distance — informatif uniquement
    n8 = count_with_clauses(where_clauses + numeric_clauses)
    log(f"8) Distance (informative)                            → {n8} candidats retenus")
    funnel.append({"step": 8, "label": "Candidats retenus", "count": n8})
    final_radius_km = 0.0
    log(f"\n🎯 Filtre UF terminé : {n8} sous-ensembles candidats.")

    if not return_results:
        return None

    # Récupération résultats
    spatial_filter_sql = " AND ".join(f"({c})" for c in (where_clauses + numeric_clauses)[1:])
    if not spatial_filter_sql.strip():
        spatial_filter_sql = "TRUE"

    if has("ecocompensation_results.surfaces_hydro"):
        dist_hydro_sub = """
            (SELECT MIN(ST_Distance(sh.geom_2154, s.geom_2154))
             FROM ecocompensation_results.surfaces_hydro sh
             WHERE sh.project_id = :project_id
               AND ST_DWithin(s.geom_2154, sh.geom_2154, :surface_hydro_radius_m)
            ) AS dist_surface_hydro_m
        """
    else:
        dist_hydro_sub = "NULL::double precision AS dist_surface_hydro_m"

    sql_final = f"""
        SELECT
            s.subset_id,
            s.uf_id,
            s.idus,
            s.k,
            s.surface_ha,
            s.miller,
            s.dist_centre_m AS distance_centre_m,
            s.denomination,
            s.siren,
            {dist_hydro_sub}
        FROM ecocompensation_results.sous_ensembles s
        WHERE s.project_id = :project_id
          AND {spatial_filter_sql};
    """
    with engine.begin() as conn:
        rows = conn.execute(text(sql_final), params).mappings().all()

    resultats = [dict(r) for r in rows]
    return resultats, final_radius_km, funnel


# ─────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────

def score_sous_ensemble(r: dict) -> tuple[int, list[dict]]:
    pts = 0
    details = []

    d = r.get("distance_centre_m") or 999999
    if d < 2000:
        pts += 3; details.append({"critere": "Distance", "points": 3, "raison": f"< 2 km ({d/1000:.1f} km)"})
    elif d < 5000:
        pts += 2; details.append({"critere": "Distance", "points": 2, "raison": f"2–5 km ({d/1000:.1f} km)"})
    else:
        pts += 1; details.append({"critere": "Distance", "points": 1, "raison": f"> 5 km ({d/1000:.1f} km)"})

    surf = float(r.get("surface_ha") or 0)
    if surf >= 20.0:
        pts += 1; details.append({"critere": "Surface", "points": 1, "raison": f"≥ 20 ha ({surf:.1f} ha)"})
    else:
        details.append({"critere": "Surface", "points": 0, "raison": f"< 20 ha ({surf:.1f} ha)"})

    miller = float(r.get("miller") or 0)
    if miller >= 0.5:
        pts += 1; details.append({"critere": "Miller", "points": 1, "raison": f"≥ 0.5 ({miller:.3f})"})
    else:
        details.append({"critere": "Miller", "points": 0, "raison": f"< 0.5 ({miller:.3f})"})

    dh = r.get("dist_surface_hydro_m")
    if dh is not None and dh < 100:
        pts += 1; details.append({"critere": "Hydro", "points": 1, "raison": f"< 100 m ({dh:.0f} m)"})
    else:
        details.append({"critere": "Hydro", "points": 0, "raison": f"{dh:.0f} m" if dh else "—"})

    return pts, details


def run_filter_uf_and_score(
    engine,
    project_id: str,
    aoi_id: str,
    cx: float,
    cy: float,
    options: "FiltreOptions",
    opts_dto,
) -> dict:
    """
    Enchaîne run() (filtre UF) + scoring groupé par UF.
    Retourne le dict complet attendu par la route POST /filter/uf.

    :param opts_dto: FiltreOptionsDTO — mêmes poids/seuils que pour les parcelles.
    :return: {total_uf, total_sous_ensembles, unites_foncieres, funnel}
    """
    result = run(
        engine,
        project_id,
        aoi_id,
        cx,
        cy,
        options,
        return_results=True,
        miller_threshold=opts_dto.miller_threshold,
        min_area_ha=opts_dto.min_area_ha,
    )

    if result is None:
        return {"unites_foncieres": [], "total_uf": 0, "total_sous_ensembles": 0, "funnel": []}

    resultats, final_radius_km, funnel = result

    if not resultats:
        return {"unites_foncieres": [], "total_uf": 0, "total_sous_ensembles": 0, "funnel": funnel}

    # Groupement par UF + scoring
    by_uf: dict[str, list[dict]] = defaultdict(list)
    for r in resultats:
        pts, details = score_sous_ensemble(r)
        by_uf[r["uf_id"]].append(
            {
                "subset_id": r["subset_id"],
                "k": r["k"],
                "idus": r["idus"],
                "surface_ha": round(float(r["surface_ha"] or 0), 2),
                "miller": round(float(r["miller"] or 0), 4),
                "distance_centre_km": round(float(r["distance_centre_m"] or 0) / 1000, 3),
                "dist_hydro_m": float(r["dist_surface_hydro_m"])
                if r.get("dist_surface_hydro_m") is not None
                else None,
                "denomination": r.get("denomination"),
                "siren": r.get("siren"),
                "score": pts,
                "score_details": details,
            }
        )

    # Trier sous-ensembles de chaque UF
    for uf_id in by_uf:
        by_uf[uf_id].sort(key=lambda x: (-x["score"], x["distance_centre_km"]))

    # Trier UF par meilleur sous-ensemble
    uf_sorted = sorted(by_uf.items(), key=lambda kv: (-kv[1][0]["score"], kv[1][0]["distance_centre_km"]))

    unites_foncieres = []
    for rang, (uf_id, subsets) in enumerate(uf_sorted, 1):
        all_idus = sorted({idu for s in subsets for idu in s["idus"]})
        best = subsets[0]
        unites_foncieres.append(
            {
                "rang": rang,
                "uf_id": uf_id,
                "nb_parcelles": len(all_idus),
                "idus": all_idus,
                "best_score": best["score"],
                "best_surface_ha": best["surface_ha"],
                "best_miller": best["miller"],
                "distance_centre_km": best["distance_centre_km"],
                "denomination": best.get("denomination"),
                "siren": best.get("siren"),
                "sous_ensembles": subsets,
            }
        )

    # Les X meilleures UF (après scoring / tri), même paramètre target_count que les parcelles.
    # target_count ≤ 0 = pas de limite.
    limit = opts_dto.target_count
    if limit > 0:
        unites_foncieres = unites_foncieres[:limit]
        for r, uf in enumerate(unites_foncieres, 1):
            uf["rang"] = r

    total_ss = sum(len(uf["sous_ensembles"]) for uf in unites_foncieres)

    return {
        "total_uf": len(unites_foncieres),
        "total_sous_ensembles": total_ss,
        "unites_foncieres": unites_foncieres,
        "funnel": funnel,
    }


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    load_dotenv(Path(__file__).parent / ".env")
    engine = get_engine()

    with engine.begin() as conn:
        row = conn.execute(text("""
            SELECT ST_X(ST_Centroid(a.geom_2154)) AS cx,
                   ST_Y(ST_Centroid(a.geom_2154)) AS cy,
                   p.aoi_id
            FROM ecocompensation.projects p
            JOIN ecocompensation.aoi a ON a.id = p.aoi_id
            WHERE p.id = :pid
        """), {"pid": PROJECT_ID}).one_or_none()

    if not row:
        print("❌ Projet introuvable.")
        return

    cx, cy = float(row[0]), float(row[1])
    aoi_id = str(row[2])
    options = FiltreOptions.defaut()

    print(f"🔗 Projet : {PROJECT_ID}")
    print(f"   Centre AOI : x={cx:.0f}, y={cy:.0f}")
    print(f"   ZDV        : {options.zdv_natures}")
    print(f"   Tronçon    : {options.troncon_hydro_mode}")
    print(f"   Surface    : {options.surface_hydro_mode} ({options.surface_hydro_radius_m:.0f} m)")
    print(f"   Miller ≥   : 0.39  |  Surface ≥ : 7 ha\n")

    # Diagnostic
    with engine.begin() as conn:
        zdv_rows = conn.execute(text("""
            SELECT DISTINCT nature, COUNT(*) AS n
            FROM ecocompensation_results.zone_de_vegetation
            WHERE project_id = :project_id
            GROUP BY nature ORDER BY n DESC
        """), {"project_id": PROJECT_ID}).all()
        n_troncons = conn.execute(text("""
            SELECT COUNT(*) FROM ecocompensation_results.troncons_hydro
            WHERE project_id = :project_id
        """), {"project_id": PROJECT_ID}).scalar_one()
        n_ss = conn.execute(text("""
            SELECT COUNT(*) FROM ecocompensation_results.sous_ensembles
            WHERE project_id = :project_id
        """), {"project_id": PROJECT_ID}).scalar_one()

    print("🌲 Natures ZDV en base :")
    for nat, n in zdv_rows:
        marker = " ✓ (filtre actif)" if nat in options.zdv_natures else ""
        print(f"   '{nat}' ({n} entités){marker}")
    if not zdv_rows:
        print("   ⚠️  Aucune ZDV en base pour ce projet.")
    print(f"\n💧 Tronçons hydro en base : {n_troncons}")
    print(f"📦 Sous-ensembles en base : {n_ss}\n")
    print("=== Filtre unités foncières ===\n")

    result = run(engine, PROJECT_ID, aoi_id, cx, cy, options, return_results=True)
    if result is None:
        return

    resultats, final_radius_km, funnel = result
    if not resultats:
        print("Aucun sous-ensemble candidat.")
        return

    # ── Groupement par UF ────────────────────────────────────────────────
    by_uf: dict[str, list[tuple]] = defaultdict(list)
    for r in resultats:
        pts, details = score_sous_ensemble(r)
        by_uf[r["uf_id"]].append((pts, r.get("distance_centre_m") or 999999, details, r))

    # Trier les sous-ensembles de chaque UF : score desc, distance asc
    for uf_id in by_uf:
        by_uf[uf_id].sort(key=lambda x: (-x[0], x[1]))

    # Trier les UF par meilleur sous-ensemble
    uf_sorted = sorted(
        by_uf.items(),
        key=lambda kv: (-kv[1][0][0], kv[1][0][1])
    )

    print(f"\n{'='*110}")
    print(f"CLASSEMENT DES UNITÉS FONCIÈRES CANDIDATES")
    print(f"{'='*110}")
    print(f"UF distinctes : {len(uf_sorted)}  |  Sous-ensembles totaux : {len(resultats)}\n")

    all_subset_ids_for_geojson = []

    for rang_uf, (uf_id, subsets) in enumerate(uf_sorted, 1):
        best_pts, best_dist, best_details, best_r = subsets[0]

        # Toutes les parcelles membres (union de tous les sous-ensembles de cette UF)
        all_idus = sorted({idu for _, _, _, r in subsets for idu in r["idus"]})

        print(f"{'─'*110}")
        print(
            f"#{rang_uf:<3} UF {uf_id}"
            f"  |  {len(all_idus)} parcelles membres"
            f"  |  Meilleur score : {best_pts} pts"
            f"  |  Dist centre : {best_dist/1000:.2f} km"
            f"  |  {len(subsets)} sous-ensembles candidats"
        )
        print(f"     Parcelles : {', '.join(all_idus)}")
        print()
        print(f"     {'k':<5} {'Score':<7} {'Surf (ha)':<11} {'Miller':<8} {'Hydro':<10} IDUs")
        print(f"     {'─'*95}")

        for pts, dist, details, r in subsets:
            h = f"{r['dist_surface_hydro_m']:.0f} m" if r.get("dist_surface_hydro_m") is not None else "—"
            idus_str = ", ".join(r["idus"][:4])
            if len(r["idus"]) > 4:
                idus_str += f" +{len(r['idus'])-4}"
            print(f"     k={r['k']:<4} {pts:<7} {r['surface_ha']:<11.2f} {r['miller']:<8.3f} {h:<10} {idus_str}")
            all_subset_ids_for_geojson.append(r["subset_id"])

        print()

    print(f"{'='*110}")
    print(f"Total : {len(uf_sorted)} UF candidates  |  {len(resultats)} sous-ensembles éligibles")

    # ── Export GeoJSON (tous les sous-ensembles candidats) ───────────────
    if all_subset_ids_for_geojson:
        with engine.begin() as conn:
            geom_rows = conn.execute(
                text("""
                    SELECT subset_id, ST_AsGeoJSON(geom_2154) AS geom_geojson
                    FROM ecocompensation_results.sous_ensembles
                    WHERE project_id = :pid
                      AND subset_id = ANY(:subset_ids)
                """),
                {"pid": PROJECT_ID, "subset_ids": all_subset_ids_for_geojson},
            ).mappings().all()

        geom_by_id = {row["subset_id"]: row["geom_geojson"] for row in geom_rows}
        features = []
        for rang_uf, (uf_id, subsets) in enumerate(uf_sorted, 1):
            for rank_ss, (pts, dist, details, r) in enumerate(subsets, 1):
                sid = r["subset_id"]
                geom_json = geom_by_id.get(sid)
                if not geom_json:
                    continue
                features.append({
                    "type": "Feature",
                    "geometry": json.loads(geom_json),
                    "properties": {
                        "rang_uf":             rang_uf,
                        "rang_ss":             rank_ss,
                        "score":               pts,
                        "subset_id":           sid,
                        "uf_id":               uf_id,
                        "k":                   r["k"],
                        "surface_ha":          float(r["surface_ha"]),
                        "miller":              float(r["miller"]),
                        "distance_centre_km":  round(float(r["distance_centre_m"]) / 1000, 3),
                        "dist_hydro_m":        float(r["dist_surface_hydro_m"]) if r.get("dist_surface_hydro_m") is not None else None,
                        "idus":                r["idus"],
                        "idus_str":            ", ".join(r["idus"]),
                    },
                })

        fc = {"type": "FeatureCollection", "features": features}
        out_path = Path(__file__).parent / "filtre_uf_resultats.geojson"
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(fc, f, ensure_ascii=False)
        print(f"\n📁 GeoJSON exporté : {out_path}  ({len(features)} features)")


if __name__ == "__main__":
    main()