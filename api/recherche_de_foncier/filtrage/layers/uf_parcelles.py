#!/usr/bin/env python3
"""
uf_parcelles.py
===============
Vue « parcelles » des unités foncières personnes morales (UF PM).

Au lieu d'afficher N sous-ensembles par UF, on présente la liste des parcelles qui participent
à au moins une combinaison retenue, avec leur fréquence de présence :
    présence = nb de combinaisons retenues contenant la parcelle / nb de combinaisons retenues
    1.0  -> parcelle du « noyau » (dans toutes les combinaisons)
    <1.0 -> parcelle « optionnelle » (dans certaines configurations seulement)
Les sous-ensembles restent dans le JSON (onglet « Combinaisons » du front) : rien n'est perdu.

Fonctions :
    summarize_uf_parcelles(uf, surfaces, in_pool)      pur, testé
    attach_uf_parcelles(engine, project_id, result)     ajoute `parcelles` + agrégats à chaque UF
    build_uf_parcelles_geojson(engine, project_id, results_uf)   FeatureCollection EPSG:4326

CLI (diagnostic) :
    python uf_parcelles.py --project-id <uuid> [--geojson out.geojson] [-v]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from collections import Counter

from sqlalchemy import create_engine, text

log = logging.getLogger("uf_parcelles")

UF_TABLE = "ecocompensation_results.unites_foncieres"
POOL_TABLE = "ecocompensation_results.parcelles"


def summarize_uf_parcelles(uf: dict, surfaces: dict[str, float], in_pool: set[str]) -> dict:
    """Agrège les sous-ensembles d'une UF en liste de parcelles + indicateurs de synthèse."""
    subsets = uf.get("sous_ensembles") or []
    n = len(subsets)
    counts: Counter[str] = Counter()
    for ss in subsets:
        for idu in set(ss.get("idus") or []):
            counts[idu] += 1

    parcelles = []
    for idu, c in counts.items():
        raw_surface = surfaces.get(idu)
        parcelles.append({
            "idu": idu,
            "surface_ha": round(float(raw_surface), 4) if raw_surface is not None else None,
            "nb_combinaisons": c,
            "presence": round(c / n, 4) if n else 0.0,
            "noyau": n > 0 and c == n,
            "in_pool": idu in in_pool,
        })
    parcelles.sort(key=lambda p: (-p["presence"], -(p["surface_ha"] or 0), p["idu"]))

    best = None
    for ss in subsets:
        sc = ss.get("score_eco") if isinstance(ss.get("score_eco"), dict) else None
        key = ((sc or {}).get("total_score") or 0, ss.get("surface_ha") or 0)
        if best is None or key > best[0]:
            best = (key, ss)
    best_ss = best[1] if best else None

    known = [p["surface_ha"] for p in parcelles if p["surface_ha"] is not None]
    return {
        "parcelles": parcelles,
        "nb_combinaisons": n,
        "nb_parcelles_retenues": len(parcelles),
        "nb_parcelles_noyau": sum(1 for p in parcelles if p["noyau"]),
        "surface_ha_parcelles": round(sum(known), 4) if known else None,
        "best_score_eco": (best_ss or {}).get("score_eco"),
        "best_subset_id": (best_ss or {}).get("subset_id"),
    }


def _all_idus(unites: list[dict]) -> list[str]:
    return sorted({idu for uf in unites for ss in uf.get("sous_ensembles") or [] for idu in ss.get("idus") or []})


def _surfaces(conn, project_id: str, uf_ids: list[str]) -> dict[tuple[str, str], float]:
    rows = conn.execute(text(f"""
        SELECT uf_id, idu, surface_ha FROM {UF_TABLE}
        WHERE project_id = CAST(:pid AS uuid) AND uf_id = ANY(CAST(:ufs AS text[]))"""),
        {"pid": project_id, "ufs": uf_ids}).mappings().all()
    return {(r["uf_id"], r["idu"]): r["surface_ha"] for r in rows}


def _in_pool(conn, project_id: str, idus: list[str]) -> set[str]:
    if not idus:
        return set()
    rows = conn.execute(text(f"""
        SELECT DISTINCT idu FROM {POOL_TABLE}
        WHERE project_id = CAST(:pid AS uuid) AND idu = ANY(CAST(:idus AS text[]))"""),
        {"pid": project_id, "idus": idus}).all()
    return {r[0] for r in rows}


def attach_uf_parcelles(engine, project_id: str, pool_result: dict) -> dict:
    """Ajoute à chaque UF : parcelles[], nb_combinaisons, nb_parcelles_retenues, nb_parcelles_noyau,
    surface_ha_parcelles, best_score_eco, best_subset_id. Ne retire rien."""
    unites = pool_result.get("unites_foncieres") or []
    if not unites:
        return pool_result
    uf_ids = [str(uf["uf_id"]) for uf in unites if uf.get("uf_id")]
    try:
        with engine.connect() as conn:
            surf = _surfaces(conn, project_id, uf_ids)
            pool = _in_pool(conn, project_id, _all_idus(unites))
    except Exception:
        log.exception("[uf_parcelles] lecture surfaces / pool échouée project_id=%s", project_id)
        surf, pool = {}, set()

    for uf in unites:
        uid = str(uf.get("uf_id"))
        surfaces = {idu: s for (u, idu), s in surf.items() if u == uid}
        uf.update(summarize_uf_parcelles(uf, surfaces, pool))
    log.info("[uf_parcelles] %d UF : %d parcelles retenues au total", len(unites),
             sum(uf["nb_parcelles_retenues"] for uf in unites))
    return pool_result


def build_uf_parcelles_geojson(engine, project_id: str, results_uf: dict) -> dict:
    """Parcelles (géométrie unites_foncieres.geom_2154) des UF de last_results_uf, en EPSG:4326.
    Propriétés : uf_id, uf_rang, idu, surface_ha, nb_combinaisons, nb_combinaisons_uf, presence,
    noyau, in_pool, score_ratio (meilleur score de l'UF, pour colorer l'emprise)."""
    unites = results_uf.get("unites_foncieres") or []
    if not unites:
        return {"type": "FeatureCollection", "features": []}
    uf_ids = [str(uf["uf_id"]) for uf in unites if uf.get("uf_id")]
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT DISTINCT ON (uf_id, idu) uf_id, idu, surface_ha,
                   ST_AsGeoJSON(ST_Transform(geom_2154, 4326), 6) AS geom
            FROM {UF_TABLE}
            WHERE project_id = CAST(:pid AS uuid) AND uf_id = ANY(CAST(:ufs AS text[]))
              AND geom_2154 IS NOT NULL"""),
            {"pid": project_id, "ufs": uf_ids}).mappings().all()
        pool = _in_pool(conn, project_id, _all_idus(unites))

    summary = {}
    for uf in unites:
        uid = str(uf.get("uf_id"))
        s = summarize_uf_parcelles(uf, {}, pool)
        best = s["best_score_eco"] or {}
        tot, mx = best.get("total_score"), best.get("max_score") or 6
        summary[uid] = {
            "rang": uf.get("rang"),
            "n": s["nb_combinaisons"],
            "by_idu": {p["idu"]: p for p in s["parcelles"]},
            "score_ratio": round(tot / mx, 4) if isinstance(tot, (int, float)) and mx else 0,
        }

    features = []
    for r in rows:
        s = summary.get(r["uf_id"])
        p = (s or {}).get("by_idu", {}).get(r["idu"])
        if not s or not p:      # parcelle de l'UF absente de toute combinaison retenue
            continue
        features.append({
            "type": "Feature",
            "id": f"{r['uf_id']}:{r['idu']}",
            "geometry": json.loads(r["geom"]) if isinstance(r["geom"], str) else r["geom"],
            "properties": {
                "uf_id": r["uf_id"],
                "uf_rang": s["rang"],
                "idu": r["idu"],
                "surface_ha": round(float(r["surface_ha"]), 4) if r["surface_ha"] is not None else None,
                "nb_combinaisons": p["nb_combinaisons"],
                "nb_combinaisons_uf": s["n"],
                "presence": p["presence"],
                "noyau": p["noyau"],
                "in_pool": p["in_pool"],
                "score_ratio": s["score_ratio"],
            },
        })
    return {"type": "FeatureCollection", "features": features}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project-id", required=True)
    ap.add_argument("--geojson", help="écrit le GeoJSON des parcelles d'UF dans ce fichier")
    ap.add_argument("--pg-url", default=os.environ.get("PG_URL"))
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S")
    if not a.pg_url:
        ap.error("--pg-url ou PG_URL requis")
    eng = create_engine(a.pg_url)
    with eng.connect() as conn:
        res = conn.execute(text("SELECT last_results_uf FROM ecocompensation.projects WHERE id = CAST(:p AS uuid)"),
                           {"p": a.project_id}).scalar()
    if isinstance(res, str):
        res = json.loads(res)
    if not res:
        raise SystemExit("Pas de last_results_uf pour ce projet")
    attach_uf_parcelles(eng, a.project_id, res)
    for uf in res["unites_foncieres"][:15]:
        log.info("UF #%-3s %-28s %3d parcelles (%d noyau) · %s ha · %d combinaisons",
                 uf.get("rang"), uf.get("uf_id"), uf["nb_parcelles_retenues"], uf["nb_parcelles_noyau"],
                 uf["surface_ha_parcelles"], uf["nb_combinaisons"])
    if a.geojson:
        gj = build_uf_parcelles_geojson(eng, a.project_id, res)
        with open(a.geojson, "w", encoding="utf-8") as f:
            json.dump(gj, f)
        log.info("GeoJSON : %d parcelles -> %s", len(gj["features"]), a.geojson)


if __name__ == "__main__":
    main()
