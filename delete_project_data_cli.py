#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
delete_project_data_cli.py
==========================

Supprime proprement toutes les géométries / données associées à un projet :
  - toutes les lignes des tables ecocompensation_results.* pour le project_id,
  - l'entrée AOI correspondante dans ecocompensation.aoi,
  - l'entrée foncier éventuelle dans ecocompensation.foncier,
  - l'entrée projet dans ecocompensation.projects.

Usage :
    # Nettoyage d'un seul projet
    python delete_project_data_cli.py --project-id <uuid>

    # Purge globale (tous les projets + AOI + foncier + résultats)
    python delete_project_data_cli.py --all
"""

from __future__ import annotations

import argparse
import logging

from sqlalchemy import text

from db import get_engine


logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ID = "da8983d6-0a9f-4a9c-9108-f8e897ae27ca"


RESULT_TABLES: list[str] = [
    "ecocompensation_results.parcelles",
    "ecocompensation_results.ebc",
    "ecocompensation_results.mesures_compensatoire_surf",
    "ecocompensation_results.mesures_compensatoire_lin",
    "ecocompensation_results.mesures_compensatoire_pct",
    "ecocompensation_results.zone_de_vegetation",
    "ecocompensation_results.cesbio",
    "ecocompensation_results.carhab",
    "ecocompensation_results.zone_humide",
    "ecocompensation_results.troncons_hydro",
    "ecocompensation_results.surfaces_hydro",
    "ecocompensation_results.surfaces_elementaires",
    "ecocompensation_results.routes",
    "ecocompensation_results.voies_ferrees",
    "ecocompensation_results.fragmentation_polygons",
    "ecocompensation_results.zones_humides_probables",
    "ecocompensation_results.znieff",
    "ecocompensation_results.arrachage_vignes",
    "ecocompensation_results.fauna",
    "ecocompensation_results.natura2000",
    "ecocompensation_results.patrimoine_naturel",
    "ecocompensation_results.reserves_naturelles",
    "ecocompensation_results.sites_classes",
    "ecocompensation_results.prairies_sensibles",
    "ecocompensation_results.bd_topo_et_cesbio",
    "ecocompensation_results.remontee_de_nappes",
    "ecocompensation_results.sous_ensembles",
    "ecocompensation_results.unites_foncieres",
    "ecocompensation_results.cosia",
    "ecocompensation_results.parcelles_pool_runs",
    "ecocompensation_results.parcelles_pool",
    "ecocompensation_results.parcelles_pool_metrics",

]


def _get_project(engine, project_id: str) -> dict | None:
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT * FROM ecocompensation.projects WHERE id = :pid"),
            {"pid": project_id},
        ).mappings().one_or_none()
    return dict(row) if row else None


def _exists(engine, table: str, id_column: str, id_value: str) -> bool:
    with engine.begin() as conn:
        n = conn.execute(
            text(f"SELECT 1 FROM {table} WHERE {id_column} = :id"),
            {"id": id_value},
        ).scalar()
    return n is not None


def _verify_absent(
    engine,
    project_id: str,
    aoi_id: str | None,
    foncier_id: str | None,
) -> bool:
    """Vérifie que projet, aoi et foncier n'existent plus. Retourne True si tout est OK."""
    ok = True
    if _exists(engine, "ecocompensation.projects", "id", project_id):
        logger.warning("Vérification : le projet %s existe encore.", project_id)
        ok = False
    else:
        logger.info("Vérification : projet %s bien supprimé.", project_id)

    if aoi_id:
        if _exists(engine, "ecocompensation.aoi", "id", aoi_id):
            logger.warning("Vérification : l'AOI %s existe encore.", aoi_id)
            ok = False
        else:
            logger.info("Vérification : AOI %s bien supprimée.", aoi_id)

    if foncier_id:
        if _exists(engine, "ecocompensation.foncier", "id", foncier_id):
            logger.warning("Vérification : le foncier %s existe encore.", foncier_id)
            ok = False
        else:
            logger.info("Vérification : foncier %s bien supprimé.", foncier_id)

    return ok


def delete_project_data(project_id: str) -> None:
    engine = get_engine()
    proj = _get_project(engine, project_id)

    if proj is None:
        logger.info("Projet %s introuvable (déjà supprimé ou id incorrect).", project_id)
        _verify_absent(engine, project_id, None, None)
        return

    aoi_id = proj.get("aoi_id")
    foncier_id = proj.get("foncier_id")

    logger.info("Nettoyage des données pour le projet %s", project_id)
    logger.info(" - aoi_id     = %s", aoi_id or "—")
    logger.info(" - foncier_id = %s", foncier_id or "—")

    # Suppression des lignes des tables de résultats par project_id
    for table in RESULT_TABLES:
        try:
            with engine.begin() as conn:
                res = conn.execute(
                    text(f"DELETE FROM {table} WHERE project_id = :pid"),
                    {"pid": project_id},
                )
            deleted = res.rowcount or 0
            logger.info(
                "Suppression des lignes dans %s pour project_id=%s -> %d entités supprimées",
                table,
                project_id,
                deleted,
            )
        except Exception as e:
            logger.warning("Impossible de supprimer dans %s : %s", table, e)

    # AOI (si le projet en avait une)
    if aoi_id:
        try:
            logger.info("Suppression de l'AOI %s", aoi_id)
            with engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM ecocompensation.aoi WHERE id = :aid"),
                    {"aid": str(aoi_id)},
                )
        except Exception as e:
            logger.warning("Impossible de supprimer l'AOI %s : %s", aoi_id, e)

    # Projet lui‑même
    try:
        logger.info("Suppression du projet %s", project_id)
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM ecocompensation.projects WHERE id = :pid"),
                {"pid": project_id},
            )
    except Exception as e:
        logger.warning("Impossible de supprimer le projet %s : %s", project_id, e)

    # Foncier éventuel (plus référencé par projects à ce stade)
    if foncier_id:
        try:
            logger.info("Suppression du foncier %s", foncier_id)
            with engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM ecocompensation.foncier WHERE id = :fid"),
                    {"fid": str(foncier_id)},
                )
        except Exception as e:
            logger.warning("Impossible de supprimer le foncier %s : %s", foncier_id, e)

    logger.info("Nettoyage terminé pour le projet %s", project_id)

    # Vérification finale : projet, AOI et foncier bien absents
    logger.info("Vérification de l'absence des entrées…")
    if _verify_absent(engine, project_id, str(aoi_id) if aoi_id else None, str(foncier_id) if foncier_id else None):
        logger.info("Tout est propre : projet, AOI et foncier supprimés.")
    else:
        logger.warning("Vérification : au moins une entrée existe encore.")


def _delete_all_rows(engine, table: str) -> None:
    try:
        with engine.begin() as conn:
            res = conn.execute(text(f"DELETE FROM {table}"))
        deleted = res.rowcount or 0
        logger.info("Purge %s -> %d ligne(s) supprimée(s)", table, deleted)
    except Exception as e:
        logger.warning("Impossible de purger %s : %s", table, e)


def delete_all_projects_data() -> None:
    """
    Purge globale :
      1) tables de résultats ecocompensation_results.*
      2) tables coeur liées aux projets (projects, aoi, foncier)
    Les tables sont conservées (aucun DROP TABLE).
    """
    engine = get_engine()

    logger.warning("⚠️ MODE PURGE GLOBALE activé : suppression de tous les projets et résultats.")

    # 1) Résultats thématiques (toutes lignes)
    for table in RESULT_TABLES:
        _delete_all_rows(engine, table)

    # 2) Tables coeur (ordre sûr vis-à-vis des dépendances)
    core_tables = [
        "ecocompensation.project_parcelles",
        "ecocompensation.projects",
        "ecocompensation.aoi",
        "ecocompensation.foncier",
    ]
    for table in core_tables:
        _delete_all_rows(engine, table)

    logger.info("Purge globale terminée. Toutes les tables sont conservées, mais vidées.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Supprime les données d'un projet ou purge l'ensemble des projets."
    )
    parser.add_argument(
        "--project-id",
        default=PROJECT_ID,
        help=f"UUID du projet à supprimer (défaut: {PROJECT_ID})",
    )
    parser.add_argument(
        "--all",
        "--all-projects",
        dest="all_projects",
        action="store_true",
        help="Purge globale : vide toutes les tables de résultats + projects/aoi/foncier (sans supprimer les tables).",
    )
    args = parser.parse_args()

    if args.all_projects:
        delete_all_projects_data()
        return

    delete_project_data(args.project_id)


if __name__ == "__main__":
    main()

