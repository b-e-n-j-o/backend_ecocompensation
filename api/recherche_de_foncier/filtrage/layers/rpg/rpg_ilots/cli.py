#!/usr/bin/env python3
"""
Lancement en ligne de commande de la chaîne îlots RPG pour un projet existant.

    python -m api.recherche_de_foncier.filtrage.layers.rpg_ilots.cli \
        --project-id <uuid> --aoi-id <uuid> --min-area-ha 7 \
        --cesbio "Prairies" "Landes ligneuses" --fauna "Cuivré des marais:1000" \
        [--steps build enrich pool] [--no-profiling] [--no-save] [--out resultat.json] [-v]

Connexion : --pg-url, sinon PG_URL, sinon pg_url_depuis_env() si tu l'exposes (voir _engine()).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time

from sqlalchemy import create_engine

from . import build, enrich, pool, runner
from .params import IlotsRpgParams

log = logging.getLogger("rpg_ilots")


def _engine(url: str | None):
    url = url or os.environ.get("PG_URL") or os.environ.get("DATABASE_URL")
    if url:
        return create_engine(url)
    from db import get_engine
    return get_engine()


def _fauna(arg: str) -> dict:
    species, _, dist = arg.rpartition(":")
    if not species:
        raise argparse.ArgumentTypeError("format attendu : 'Espèce:distance_m'")
    return {"species": species, "dist_m": float(dist)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project-id", required=True)
    ap.add_argument("--aoi-id", help="requis pour l'étape build")
    ap.add_argument("--steps", nargs="+", choices=["build", "enrich", "pool"], default=["build", "enrich", "pool"])
    ap.add_argument("--millesime", default="2025")
    ap.add_argument("--min-area-ha", type=float, default=0.0)
    ap.add_argument("--min-parcelles", type=int, default=2)
    ap.add_argument("--miller", type=float, default=0.0)
    ap.add_argument("--cesbio", nargs="*", default=[])
    ap.add_argument("--fauna", nargs="*", type=_fauna, default=[], help="'Espèce:distance_m'")
    ap.add_argument("--zh", action="store_true", help="mode zones humides intra (study_type)")
    ap.add_argument("--min-zh-ha", type=float, default=0.0)
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--no-profiling", action="store_true", help="sans score éco / prospect PM")
    ap.add_argument("--no-save", action="store_true", help="ne pas écrire projects.last_results_ilots_rpg")
    ap.add_argument("--out", help="écrit le JSON de résultat dans ce fichier")
    ap.add_argument("--pg-url")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S")
    if "build" in a.steps and not a.aoi_id:
        ap.error("--aoi-id requis pour l'étape build")

    p = IlotsRpgParams(millesime=a.millesime, min_area_ha=a.min_area_ha, min_parcelles=a.min_parcelles,
                       miller_thresh=a.miller, cesbio_libelles=a.cesbio, fauna_criteria=a.fauna,
                       study_type="zones_humides_intra" if a.zh else "faune_buffer",
                       min_zone_humide_ha=a.min_zh_ha, limit=a.limit)
    log.info("Paramètres : %s", p)
    eng = _engine(a.pg_url)
    t0 = time.time()
    if "build" in a.steps:
        build.run(eng, a.project_id, a.aoi_id, p, log.info)
    if "enrich" in a.steps:
        enrich.run(eng, a.project_id, p.fauna_species, log.info)
    if "pool" in a.steps:
        res = pool.build_response(eng, a.project_id, p, profiling=not a.no_profiling)
        log.info("Pool : %d îlots retenus sur %d candidats", res["total_ilots"], res["total_candidats"])
        for il in res["ilots"][:10]:
            log.info("  #%-3d îlot %-10s %5.1f ha  %3d parcelles  %-3s %-4s  pm_unique=%s  score=%s",
                     il["rang"], il["num_ilot"], il["surface_ha"], il["nb_parcelles"],
                     il["cat_dominante"] or "-", il["code_cultu_dominant"] or "-", il["pm_unique"],
                     (il.get("score_eco") or {}).get("total_score", "-"))
        if not a.no_save:
            runner.save_results(eng, a.project_id, res)
            log.info("Résultat écrit dans projects.last_results_ilots_rpg")
        if a.out:
            with open(a.out, "w", encoding="utf-8") as f:
                json.dump(res, f, ensure_ascii=False, indent=2, default=str)
            log.info("JSON écrit : %s", a.out)
    log.info("Terminé en %.1fs", time.time() - t0)


if __name__ == "__main__":
    main()
