"""
Îlots agricoles RPG comme unités foncières KerFoncier (module dédié, indépendant des UF personnes morales).

Chaîne (après le filtre parcelles, en tâche de fond) :
    build.py   AOI -> îlots RPG -> UF = parcelles cadastrales membres (une ligne par îlot, pas de sous-ensembles)
    enrich.py  végétation CESBIO + distances faune par îlot
    pool.py    filtre sur colonnes (Miller, CESBIO, faune, ZH) + score éco + prospect PM -> JSON
    runner.py  branchement dans filter_orchestrator (projects.last_results_ilots_rpg)
    cli.py     lancement en ligne de commande, étape par étape

Prérequis en base (construits hors appli, une fois par millésime / département) :
    ecocompensation.rpg{millesime}_ilots          (ingest_rpg.py + build_rpg_ilots_cadastre.py)
    ecocompensation.rpg{millesime}_ilot_membre    (build_rpg_ilots_cadastre.py)
"""
from .params import IlotsRpgParams  # noqa: F401

DEFAULT_MILLESIME = "2025"
