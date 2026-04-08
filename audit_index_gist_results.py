#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_gist_indexes.py
=====================
Vérifie que chaque colonne de géométrie dans le schéma
``ecocompensation_results`` dispose d'un index GiST.
Crée automatiquement les index manquants.

Usage :
    python audit_gist_indexes.py [--dry-run]

Options :
    --dry-run   Affiche les CREATE INDEX sans les exécuter.
"""

import argparse
import sys

from sqlalchemy import text
from db import get_engine  # adapte si ton module s'appelle autrement

SCHEMA = "ecocompensation_results"

# Requête : toutes les colonnes de géométrie du schéma cible
GEOM_COLS_SQL = """
SELECT
    c.table_name,
    c.column_name
FROM information_schema.columns c
WHERE c.table_schema = :schema
  AND c.udt_name IN ('geometry', 'geography')
ORDER BY c.table_name, c.column_name;
"""

# Requête : index GiST existants sur ce schéma
EXISTING_GIST_SQL = """
SELECT
    t.relname  AS table_name,
    a.attname  AS column_name
FROM pg_index      ix
JOIN pg_class      t  ON t.oid  = ix.indrelid
JOIN pg_class      i  ON i.oid  = ix.indexrelid
JOIN pg_am         am ON am.oid = i.relam
JOIN pg_attribute  a  ON a.attrelid = t.oid AND a.attnum = ANY(ix.indkey)
JOIN pg_namespace  ns ON ns.oid = t.relnamespace
WHERE ns.nspname = :schema
  AND am.amname  = 'gist';
"""


def main(dry_run: bool = False) -> None:
    engine = get_engine()

    with engine.begin() as conn:
        geom_cols = conn.execute(text(GEOM_COLS_SQL), {"schema": SCHEMA}).mappings().all()
        gist_rows = conn.execute(text(EXISTING_GIST_SQL), {"schema": SCHEMA}).mappings().all()

    # Index existants sous forme d'ensemble (table, colonne)
    indexed: set[tuple[str, str]] = {
        (r["table_name"], r["column_name"]) for r in gist_rows
    }

    missing = [
        (r["table_name"], r["column_name"])
        for r in geom_cols
        if (r["table_name"], r["column_name"]) not in indexed
    ]

    print(f"Schéma analysé         : {SCHEMA}")
    print(f"Colonnes géométriques  : {len(geom_cols)}")
    print(f"Index GiST existants   : {len(indexed)}")
    print(f"Index manquants        : {len(missing)}")

    if not missing:
        print("\n✅ Tous les index GiST sont en place, rien à faire.")
        return

    print()
    created = 0
    errors = 0

    with engine.begin() as conn:
        for table, col in missing:
            # Nom d'index déterministe et lisible
            idx_name = f"idx_gist_{table}_{col}"
            ddl = (
                f'CREATE INDEX IF NOT EXISTS "{idx_name}" '
                f'ON "{SCHEMA}"."{table}" USING GIST ("{col}");'
            )

            if dry_run:
                print(f"[DRY-RUN] {ddl}")
            else:
                print(f"  ⏳ Création : {idx_name} ... ", end="", flush=True)
                try:
                    conn.execute(text(ddl))
                    print("✅")
                    created += 1
                except Exception as exc:
                    print(f"❌  {exc}")
                    errors += 1

    if not dry_run:
        print(f"\nBilan : {created} index créés, {errors} erreur(s).")
    else:
        print(f"\n[DRY-RUN] {len(missing)} index seraient créés.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audit index GiST — ecocompensation_results")
    parser.add_argument("--dry-run", action="store_true", help="Affiche sans exécuter")
    args = parser.parse_args()
    main(dry_run=args.dry_run)