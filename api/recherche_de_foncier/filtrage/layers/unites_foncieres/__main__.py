"""
CLI — unités foncières d'un projet existant, avec la config de son dernier filtre.

    python -m api.recherche_de_foncier.filtrage.layers.unites_foncieres --project-id <uuid> -v
    python -m api.recherche_de_foncier.filtrage.layers.unites_foncieres --project-id <uuid> \\
        --siren 351397096 --expect 331220000D2368 331220000D2369 331220000D2370 331220000D2381 331220000D2384

--siren : ne traite qu'une personne morale (n'efface que ses lignes, n'écrit pas last_results_uf).
"""
from __future__ import annotations

import argparse
import logging

from dotenv import find_dotenv, load_dotenv
from sqlalchemy import text

from . import tables
from .params import load_project_config, params_from_config
from .runner import run_sync

log = logging.getLogger("uf.cli")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project-id", required=True)
    ap.add_argument("--siren", help="limiter à une personne morale (debug)")
    ap.add_argument("--expect", nargs="*", help="IDU qui doivent se trouver dans un même terrain retenu")
    ap.add_argument("--cesbio-terrain", action="store_true",
                    help="CESBIO sur le terrain (au moins une parcelle) au lieu de chaque parcelle")
    ap.add_argument("--miller-terrain", action="store_true", help="Miller bloquant sur l'union du terrain")
    ap.add_argument("--no-save", action="store_true", help="ne pas écrire last_results_uf")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s", datefmt="%H:%M:%S")

    load_dotenv(find_dotenv())
    from db import get_engine
    engine = get_engine()
    try:
        from db import get_engine_ppm
        engine_ppm = get_engine_ppm()
    except Exception:
        engine_ppm = None

    with engine.connect() as conn:
        aoi_id = conn.execute(text("SELECT aoi_id FROM ecocompensation.projects WHERE id = CAST(:p AS uuid)"),
                              {"p": a.project_id}).scalar()
    if not aoi_id:
        raise SystemExit(f"Projet sans AOI : {a.project_id}")

    params = params_from_config(load_project_config(engine, a.project_id), siren=a.siren,
                                cesbio_par_parcelle=not a.cesbio_terrain,
                                miller_sur_terrain=a.miller_terrain)
    save = not (a.no_save or a.siren)
    result = run_sync(engine, a.project_id, str(aoi_id), params, log.info, engine_ppm=engine_ppm, save=save)

    log.info("─" * 90)
    for u in result["unites_foncieres"][:20]:
        log.info("#%-3s %-18s %-30.30s %4d parc. %8.2f ha  score %s/%s  %s",
                 u["rang"], u["uf_id"], u["denomination"] or "", u["nb_parcelles"], u["surface_ha"],
                 u["score_eco"]["total_score"], u["score_eco"]["max_score"],
                 "" if u["verdict"] else f"✗ {u['motif_rejet']}")
    log.info("Diagnostic : %s", result["diagnostic"])
    if not save:
        log.info("(last_results_uf non écrit)")

    if a.expect:
        with engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT uf_id, nb_parcelles, surface_ha, verdict, motif_rejet FROM {tables.T}
                WHERE project_id = CAST(:p AS uuid) AND idus @> CAST(:e AS text[])"""),
                {"p": a.project_id, "e": a.expect}).mappings().all()
            if rows:
                for r in rows:
                    log.info("ATTENDU : dans %s (%d parcelles, %.2f ha) -> %s", r["uf_id"], r["nb_parcelles"],
                             r["surface_ha"], "RETENU" if r["verdict"] else f"REJETÉ ({r['motif_rejet']})")
            else:
                for r in conn.execute(text(f"""
                        SELECT idu, eliminee_par, uf_id FROM {tables.P}
                        WHERE project_id = CAST(:p AS uuid) AND idu = ANY(CAST(:e AS text[]))"""),
                        {"p": a.project_id, "e": a.expect}).mappings():
                    log.info("ATTENDU absent : %s eliminee_par=%s uf_id=%s", r["idu"], r["eliminee_par"], r["uf_id"])


if __name__ == "__main__":
    main()
