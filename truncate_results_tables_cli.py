#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
truncate_results_tables_cli.py
==============================

Nettoie les tables de résultats du schéma ecocompensation_results :
  - mode global   : TRUNCATE de toutes les tables de résultats (optionnellement CASCADE),
  - mode ciblé    : DELETE uniquement pour un project_id/aoi_id défini en dur.

Dans les deux cas, le script peut aussi supprimer les AOI liées.

Usage:
    python truncate_results_tables_cli.py
    python truncate_results_tables_cli.py --dry-run
    python truncate_results_tables_cli.py --cascade
"""

from __future__ import annotations

import argparse
import logging
from collections import OrderedDict
from typing import Any

from sqlalchemy import text

from db import get_engine
from layer_runner import LAYER_REGISTRY


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CIBLAGE EN DUR (mode "suppression partielle")
# ---------------------------------------------------------------------------
# Si PROJECT_ID_TO_DELETE et/ou AOI_ID_TO_DELETE sont renseignés, le script passe
# en mode ciblé : DELETE des lignes concernées au lieu d'un TRUNCATE global.
PROJECT_ID_TO_DELETE: str | None = None
AOI_ID_TO_DELETE: str | None = None

# Si True, supprime l'AOI liée après nettoyage des résultats ciblés.
DELETE_LINKED_AOI_IN_TARGET_MODE: bool = True

# Si True, en mode global (TRUNCATE), supprime aussi les AOI liées aux project_id
# trouvés dans les tables de résultats avant nettoyage.
DELETE_LINKED_AOI_IN_GLOBAL_MODE: bool = True


# Expansion explicite du motif utilisé dans layer_runner
WILDCARD_TABLE_EXPANSIONS: dict[str, list[str]] = {
    "ecocompensation_results.mesures_compensatoire_*": [
        "ecocompensation_results.mesures_compensatoire_surf",
        "ecocompensation_results.mesures_compensatoire_lin",
        "ecocompensation_results.mesures_compensatoire_pct",
        "ecocompensation_results.mesures_compensatoire_commune",
    ]
}


def _dedupe_keep_order(items: list[str]) -> list[str]:
    return list(OrderedDict.fromkeys(items))


def build_result_tables() -> list[str]:
    tables: list[str] = []
    for layer in LAYER_REGISTRY:
        table_name = str(layer.get("table", "")).strip()
        if not table_name:
            continue
        if table_name in WILDCARD_TABLE_EXPANSIONS:
            tables.extend(WILDCARD_TABLE_EXPANSIONS[table_name])
        else:
            tables.append(table_name)
    return _dedupe_keep_order(tables)


def _table_exists(conn, table: str) -> bool:
    # to_regclass retourne NULL si la table n'existe pas.
    return conn.execute(text("SELECT to_regclass(:table_name)"), {"table_name": table}).scalar() is not None


def _column_exists(conn, table: str, column_name: str) -> bool:
    schema, name = table.split(".", 1)
    row = conn.execute(
        text(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = :schema
              AND table_name = :name
              AND column_name = :col
            LIMIT 1
            """
        ),
        {"schema": schema, "name": name, "col": column_name},
    ).scalar()
    return row is not None


def _delete_results_for_scope(conn, table: str, project_id: str | None, aoi_id: str | None) -> int:
    predicates: list[str] = []
    params: dict[str, Any] = {}

    if project_id and _column_exists(conn, table, "project_id"):
        predicates.append("project_id = :pid")
        params["pid"] = project_id
    if aoi_id and _column_exists(conn, table, "aoi_id"):
        predicates.append("aoi_id = :aid")
        params["aid"] = aoi_id

    if not predicates:
        return 0

    sql = f"DELETE FROM {table} WHERE " + " OR ".join(predicates)
    res = conn.execute(text(sql), params)
    return int(res.rowcount or 0)


def _collect_project_ids_in_results(conn, tables: list[str]) -> list[str]:
    project_ids: list[str] = []
    for table in tables:
        if not _table_exists(conn, table):
            continue
        if not _column_exists(conn, table, "project_id"):
            continue
        rows = conn.execute(
            text(f"SELECT DISTINCT project_id::text AS pid FROM {table} WHERE project_id IS NOT NULL")
        ).mappings().all()
        project_ids.extend([r["pid"] for r in rows if r.get("pid")])
    return _dedupe_keep_order(project_ids)


def _delete_aoi_for_project(conn, project_id: str) -> bool:
    proj = conn.execute(
        text("SELECT aoi_id::text AS aoi_id FROM ecocompensation.projects WHERE id = :pid"),
        {"pid": project_id},
    ).mappings().one_or_none()

    deleted = False

    if proj and proj.get("aoi_id"):
        res = conn.execute(
            text("DELETE FROM ecocompensation.aoi WHERE id = :aid"),
            {"aid": proj["aoi_id"]},
        )
        if (res.rowcount or 0) > 0:
            deleted = True

    if _column_exists(conn, "ecocompensation.aoi", "project_id"):
        res2 = conn.execute(
            text("DELETE FROM ecocompensation.aoi WHERE project_id = :pid"),
            {"pid": project_id},
        )
        if (res2.rowcount or 0) > 0:
            deleted = True

    return deleted


def _target_mode_enabled() -> bool:
    return bool(PROJECT_ID_TO_DELETE or AOI_ID_TO_DELETE)


def truncate_result_tables(*, dry_run: bool = False, cascade: bool = False) -> None:
    engine = get_engine()
    tables = build_result_tables()

    if not tables:
        logger.warning("Aucune table détectée dans LAYER_REGISTRY.")
        return

    logger.info("Tables ciblées (%d) :", len(tables))
    for t in tables:
        logger.info(" - %s", t)

    if _target_mode_enabled():
        logger.info("Mode ciblé actif : project_id=%s | aoi_id=%s", PROJECT_ID_TO_DELETE, AOI_ID_TO_DELETE)

    if dry_run:
        logger.info("Mode --dry-run : aucune suppression exécutée.")
        return

    n_ok = 0
    n_missing = 0
    n_err = 0
    n_deleted_rows = 0

    if _target_mode_enabled():
        for table in tables:
            try:
                with engine.begin() as conn:
                    if not _table_exists(conn, table):
                        logger.warning("Table absente, ignorée: %s", table)
                        n_missing += 1
                        continue

                    deleted = _delete_results_for_scope(conn, table, PROJECT_ID_TO_DELETE, AOI_ID_TO_DELETE)
                    logger.info("DELETE ciblé OK: %s -> %d ligne(s)", table, deleted)
                    n_deleted_rows += deleted
                    n_ok += 1
            except Exception as e:
                logger.warning("DELETE ciblé impossible sur %s: %s", table, e)
                n_err += 1

        if DELETE_LINKED_AOI_IN_TARGET_MODE:
            try:
                with engine.begin() as conn:
                    deleted_aoi = 0
                    if PROJECT_ID_TO_DELETE and _delete_aoi_for_project(conn, PROJECT_ID_TO_DELETE):
                        deleted_aoi += 1
                    if AOI_ID_TO_DELETE:
                        res = conn.execute(
                            text("DELETE FROM ecocompensation.aoi WHERE id = :aid"),
                            {"aid": AOI_ID_TO_DELETE},
                        )
                        if (res.rowcount or 0) > 0:
                            deleted_aoi += 1
                    logger.info("Suppression AOI liée (mode ciblé): %d occurrence(s)", deleted_aoi)
            except Exception as e:
                logger.warning("Suppression AOI liée impossible en mode ciblé: %s", e)

        logger.info(
            "Nettoyage ciblé terminé: %d table(s) traitée(s), %d absente(s), %d erreur(s), %d ligne(s) supprimée(s).",
            n_ok,
            n_missing,
            n_err,
            n_deleted_rows,
        )
        return

    project_ids_found: list[str] = []
    if DELETE_LINKED_AOI_IN_GLOBAL_MODE:
        try:
            with engine.begin() as conn:
                project_ids_found = _collect_project_ids_in_results(conn, tables)
            logger.info("Project_id détectés avant TRUNCATE: %d", len(project_ids_found))
        except Exception as e:
            logger.warning("Collecte des project_id avant TRUNCATE impossible: %s", e)

    for table in tables:
        try:
            with engine.begin() as conn:
                if not _table_exists(conn, table):
                    logger.warning("Table absente, ignorée: %s", table)
                    n_missing += 1
                    continue

                sql = f"TRUNCATE TABLE {table} RESTART IDENTITY"
                if cascade:
                    sql += " CASCADE"
                conn.execute(text(sql))
                logger.info("TRUNCATE OK: %s", table)
                n_ok += 1
        except Exception as e:
            logger.warning("TRUNCATE impossible sur %s: %s", table, e)
            n_err += 1

    if DELETE_LINKED_AOI_IN_GLOBAL_MODE and project_ids_found:
        deleted_aoi_count = 0
        for pid in project_ids_found:
            try:
                with engine.begin() as conn:
                    if _delete_aoi_for_project(conn, pid):
                        deleted_aoi_count += 1
            except Exception as e:
                logger.warning("Suppression AOI liée impossible pour project_id=%s: %s", pid, e)
        logger.info("Suppression AOI liée (mode global): %d projet(s) traité(s)", deleted_aoi_count)

    logger.info(
        "Nettoyage terminé: %d table(s) vidée(s), %d absente(s), %d erreur(s).",
        n_ok, n_missing, n_err
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Nettoie les tables ecocompensation_results (TRUNCATE global ou DELETE ciblé)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Affiche les tables ciblées sans exécuter de TRUNCATE.",
    )
    parser.add_argument(
        "--cascade",
        action="store_true",
        help="Ajoute CASCADE au TRUNCATE (utile si contraintes bloquantes).",
    )
    args = parser.parse_args()

    truncate_result_tables(dry_run=args.dry_run, cascade=args.cascade)


if __name__ == "__main__":
    main()
