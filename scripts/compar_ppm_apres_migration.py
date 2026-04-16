#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_ppm.py
============

Compare les IDUs de la table PPM source (base KERELIA, dept 33)
avec ceux de la table destination (base ECOCOMP).

Identifie :
  - Manquants en destination (à réimporter)
  - En trop en destination (doublons / erreurs)
  - Taux de couverture

Usage :
    python audit_ppm.py [--export-missing missing_idus.csv]
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from urllib.parse import quote_plus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

SOURCE_TABLE = "public.parcelles_personnes_morales"
SOURCE_FILTER = "WHERE LEFT(code_insee, 2) = '33'"

DEST_TABLE = "public.parcelles_personnes_morales_tmp"


# ── Connexions ────────────────────────────────────────────────────────────────

def _make_engine_src():
    host = os.environ["SUPABASE_PPM_HOST"]
    port = os.environ.get("SUPABASE_PPM_PORT", "5432")
    db   = os.environ["SUPABASE_PPM_DB"]
    user = os.environ["SUPABASE_PPM_USER"]
    pwd  = os.environ["SUPABASE_PPM_PASSWORD"]
    url  = f"postgresql+psycopg://{user}:{quote_plus(pwd)}@{host}:{port}/{db}"
    logger.info(f"  source  -> {host}:{port}/{db} / {SOURCE_TABLE}")
    return create_engine(url, pool_pre_ping=True)


def _make_engine_dst():
    host = os.environ["SUPABASE_HOST"]
    port = os.environ.get("SUPABASE_PORT", "6543")
    db   = os.environ["SUPABASE_DB"]
    user = os.environ["SUPABASE_USER"]
    pwd  = os.environ["SUPABASE_PASSWORD"]
    url  = f"postgresql+psycopg://{user}:{quote_plus(pwd)}@{host}:{port}/{db}"
    logger.info(f"  dest    -> {host}:{port}/{db} / {DEST_TABLE}")
    return create_engine(url, pool_pre_ping=True)


# ── Audit ─────────────────────────────────────────────────────────────────────

def load_idus(engine, table: str, where: str = "") -> set[str]:
    """Charge tous les IDUs d'une table en mémoire."""
    logger.info(f"  Chargement IDUs depuis {table} {where}...")
    with engine.connect() as conn:
        # Si where contient déjà WHERE, on utilise AND pour la condition suivante
        and_or_where = "AND" if where.strip().upper().startswith("WHERE") else "WHERE"
        rows = conn.execute(
            text(f"SELECT idu FROM {table} {where} {and_or_where} idu IS NOT NULL")
        ).scalars().all()
    result = set(rows)
    logger.info(f"  -> {len(result):,} IDUs distincts chargés")
    return result


def audit(engine_src, engine_dst, export_missing: str | None):

    # ── Comptages bruts ───────────────────────────────────────────────────────
    with engine_src.connect() as conn:
        count_src = conn.execute(
            text(f"SELECT COUNT(*) FROM {SOURCE_TABLE} {SOURCE_FILTER}")
        ).scalar_one()

    with engine_dst.connect() as conn:
        count_dst = conn.execute(
            text(f"SELECT COUNT(*) FROM {DEST_TABLE}")
        ).scalar_one()

    logger.info("=" * 60)
    logger.info(f"Comptage source  (PPM  / dept 33) : {count_src:>10,}")
    logger.info(f"Comptage dest    (ECOCOMP tmp)     : {count_dst:>10,}")
    logger.info(f"Delta brut                         : {count_dst - count_src:>+10,}")
    logger.info("=" * 60)

    # ── Chargement IDUs ───────────────────────────────────────────────────────
    logger.info("Chargement des IDUs source...")
    idus_src = load_idus(engine_src, SOURCE_TABLE, SOURCE_FILTER)

    logger.info("Chargement des IDUs destination...")
    idus_dst = load_idus(engine_dst, DEST_TABLE)

    # ── Comparaison ───────────────────────────────────────────────────────────
    missing  = idus_src - idus_dst   # dans source mais pas dans dest
    extra    = idus_dst - idus_src   # dans dest mais pas dans source
    common   = idus_src & idus_dst

    coverage = len(common) / len(idus_src) * 100 if idus_src else 0.0

    logger.info("=" * 60)
    logger.info(f"IDUs communs     : {len(common):>10,}")
    logger.info(f"IDUs manquants   : {len(missing):>10,}  (dans source, absent de dest)")
    logger.info(f"IDUs en trop     : {len(extra):>10,}  (dans dest, absent de source)")
    logger.info(f"Taux couverture  : {coverage:>9.2f}%")
    logger.info("=" * 60)

    # ── Doublons dans destination ─────────────────────────────────────────────
    logger.info("Vérification doublons dans destination...")
    with engine_dst.connect() as conn:
        dup_rows = conn.execute(text(f"""
            SELECT idu, COUNT(*) AS n
            FROM {DEST_TABLE}
            WHERE idu IS NOT NULL
            GROUP BY idu
            HAVING COUNT(*) > 1
            ORDER BY n DESC
            LIMIT 20
        """)).mappings().all()

    if dup_rows:
        logger.warning(f"  {len(dup_rows)} IDUs en doublon (top 20) :")
        for r in dup_rows:
            logger.warning(f"    {r['idu']} -> {r['n']}x")
    else:
        logger.info("  Aucun doublon détecté.")

    # ── Export IDUs manquants ─────────────────────────────────────────────────
    if missing and export_missing:
        path = Path(export_missing)
        with open(path, "w") as f:
            f.write("idu\n")
            for idu in sorted(missing):
                f.write(f"{idu}\n")
        logger.info(f"IDUs manquants exportés -> {path} ({len(missing):,} lignes)")

    return {
        "count_src": count_src,
        "count_dst": count_dst,
        "missing": len(missing),
        "extra": len(extra),
        "coverage_pct": round(coverage, 2),
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Audit migration PPM -> ECOCOMP")
    parser.add_argument(
        "--export-missing",
        default=None,
        help="Chemin CSV pour exporter les IDUs manquants (ex: missing.csv)",
    )
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parent / ".env")

    logger.info("Connexion source (PPM)...")
    engine_src = _make_engine_src()

    logger.info("Connexion destination (ECOCOMP)...")
    engine_dst = _make_engine_dst()

    result = audit(engine_src, engine_dst, args.export_missing)

    if result["missing"] == 0 and result["extra"] == 0:
        logger.info("Migration COMPLETE - aucun ecart detecte.")
    elif result["missing"] > 0:
        logger.warning(
            f"Migration INCOMPLETE - {result['missing']:,} IDUs manquants. "
            f"Relancez migrate_ppm.py avec --resume ou reimportez les IDUs manquants."
        )


if __name__ == "__main__":
    main()