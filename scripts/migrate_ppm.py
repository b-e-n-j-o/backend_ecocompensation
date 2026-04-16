#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
migrate_ppm.py
==============

Migration de public.parcelles_personnes_morales (base PPM / KERELIA)
vers ecocompensation.parcelles_personnes_morales (base ECOCOMP).

Stratégie :
  - Lecture par batch côté source via SQLAlchemy
  - Insertion via COPY (psycopg natif) côté destination → stable et rapide
  - Géométrie transmise en EWKT (ST_AsEWKT) pour éviter tout pb d'encodage WKB

Usage :
    python migrate_ppm.py [--batch-size 5000] [--dry-run] [--resume-from-offset N]
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from urllib.parse import quote_plus
import psycopg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

DEPT_FILTER = ("33",)
DEPT_WHERE = "WHERE LEFT(code_insee, 2) IN ({})".format(
    ", ".join(f"'{d}'" for d in DEPT_FILTER)
)

SOURCE_TABLE = "public.parcelles_personnes_morales"
DEST_SCHEMA  = "ecocompensation"
DEST_TABLE   = f"{DEST_SCHEMA}.parcelles_personnes_morales"

COLS_NOGEOM = ["idu", "code_insee", "section", "numero",
               "siren", "denomination", "forme_juridique", "contenance"]

DDL = f"""
CREATE SCHEMA IF NOT EXISTS {DEST_SCHEMA};

CREATE TABLE IF NOT EXISTS {DEST_TABLE} (
    idu             text,
    code_insee      text,
    section         text,
    numero          text,
    siren           text,
    denomination    text,
    forme_juridique text,
    contenance      double precision,
    geom_2154       geometry(Geometry, 2154)
);

CREATE INDEX IF NOT EXISTS ppm_ecocomp_geom_idx
    ON {DEST_TABLE} USING GIST (geom_2154);
CREATE INDEX IF NOT EXISTS ppm_ecocomp_siren_idx
    ON {DEST_TABLE} USING BTREE (siren);
CREATE INDEX IF NOT EXISTS ppm_ecocomp_idu_idx
    ON {DEST_TABLE} USING BTREE (idu);
"""


# ── Connexions ────────────────────────────────────────────────────────────────

def _make_engine_src():
    host = os.environ["SUPABASE_PPM_HOST"]
    port = os.environ.get("SUPABASE_PPM_PORT", "5432")
    db   = os.environ["SUPABASE_PPM_DB"]
    user = os.environ["SUPABASE_PPM_USER"]
    pwd  = os.environ["SUPABASE_PPM_PASSWORD"]
    url  = f"postgresql+psycopg://{user}:{quote_plus(pwd)}@{host}:{port}/{db}"
    logger.info(f"  source  -> {host}:{port}/{db}")
    return create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=0)


def _make_dsn_dst() -> str:
    host = os.environ["SUPABASE_HOST"]
    port = os.environ.get("SUPABASE_PORT", "6543")
    db   = os.environ["SUPABASE_DB"]
    user = os.environ["SUPABASE_USER"]
    pwd  = os.environ["SUPABASE_PASSWORD"]
    logger.info(f"  dest    -> {host}:{port}/{db}")
    return f"host={host} port={port} dbname={db} user={user} password={pwd} sslmode=require"


# ── DDL ───────────────────────────────────────────────────────────────────────

def setup_dest(dsn_dst: str, dry_run: bool):
    if dry_run:
        logger.info("DRY-RUN : DDL ignore")
        return
    logger.info("Creation de la table destination si besoin...")
    with psycopg.connect(dsn_dst) as conn:
        for stmt in DDL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(stmt)
        conn.commit()
    logger.info("DDL OK")


# ── Comptage ──────────────────────────────────────────────────────────────────

def count_source(engine_src) -> int:
    with engine_src.connect() as conn:
        return conn.execute(
            text(f"SELECT COUNT(*) FROM {SOURCE_TABLE} {DEPT_WHERE}")
        ).scalar_one()


def count_by_dept(engine_src):
    with engine_src.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT LEFT(code_insee, 2) AS dept, COUNT(*) AS n
            FROM {SOURCE_TABLE} {DEPT_WHERE}
            GROUP BY LEFT(code_insee, 2) ORDER BY dept
        """)).mappings().all()
    total = 0
    logger.info("Repartition par departement :")
    for r in rows:
        logger.info(f"    {r['dept']} -> {r['n']:>10,}")
        total += r["n"]
    logger.info(f"    TOTAL -> {total:>10,}")


# ── Helpers CSV ───────────────────────────────────────────────────────────────

def _esc(v) -> str:
    if v is None:
        return r"\N"
    return str(v).replace("\\", "\\\\").replace("\t", " ").replace("\n", " ")

def _esc_float(v) -> str:
    if v is None:
        return r"\N"
    return str(float(v))


# ── Migration ─────────────────────────────────────────────────────────────────

def migrate(engine_src, dsn_dst: str, batch_size: int, dry_run: bool, resume_from_offset: int):

    setup_dest(dsn_dst, dry_run)

    total_src = count_source(engine_src)
    logger.info(f"Source : {total_src:,} lignes filtrees")
    count_by_dept(engine_src)

    if resume_from_offset > 0:
        logger.info(f"Reprise depuis offset {resume_from_offset:,}")

    offset = resume_from_offset
    total_inserted = 0
    batch_num = 0
    t_global = time.perf_counter()

    dst_conn = None if dry_run else psycopg.connect(dsn_dst)

    # Table temporaire creee une seule fois
    TMP = "_migrate_ppm_tmp"
    if not dry_run:
        dst_conn.execute(f"""
            CREATE TEMP TABLE IF NOT EXISTS {TMP} (
                idu text, code_insee text, section text, numero text,
                siren text, denomination text, forme_juridique text,
                contenance double precision, geom_ewkt text
            )
        """)
        dst_conn.commit()

    try:
        while True:
            batch_num += 1
            t_batch = time.perf_counter()

            cols_select = ", ".join(
                "ST_AsEWKT(geom_2154) AS geom_2154" if c == "geom_2154" else c
                for c in [*COLS_NOGEOM, "geom_2154"]
            )

            with engine_src.connect() as conn:
                rows = conn.execute(
                    text(f"""
                        SELECT {cols_select}
                        FROM {SOURCE_TABLE}
                        {DEPT_WHERE}
                        ORDER BY idu NULLS LAST
                        LIMIT :lim OFFSET :off
                    """),
                    {"lim": batch_size, "off": offset},
                ).mappings().all()

            if not rows:
                logger.info("Plus de donnees, migration terminee.")
                break

            n = len(rows)
            logger.info(
                f"  Batch {batch_num} | offset {offset:,} -> {offset + n - 1:,} ({n:,} lignes)"
            )

            if not dry_run:
                # Vider la table temporaire
                dst_conn.execute(f"TRUNCATE {TMP}")

                # COPY vers table temporaire
                buf = io.StringIO()
                for r in rows:
                    line = "\t".join([
                        _esc(r["idu"]),
                        _esc(r["code_insee"]),
                        _esc(r["section"]),
                        _esc(r["numero"]),
                        _esc(r["siren"]),
                        _esc(r["denomination"]),
                        _esc(r["forme_juridique"]),
                        _esc_float(r["contenance"]),
                        _esc(r["geom_2154"]),
                    ])
                    buf.write(line + "\n")
                buf.seek(0)

                with dst_conn.cursor() as cur:
                    with cur.copy(f"COPY {TMP} FROM STDIN") as copy:
                        copy.write(buf.read())

                # INSERT avec conversion geom
                dst_conn.execute(f"""
                    INSERT INTO {DEST_TABLE}
                        (idu, code_insee, section, numero, siren,
                         denomination, forme_juridique, contenance, geom_2154)
                    SELECT idu, code_insee, section, numero, siren,
                           denomination, forme_juridique, contenance,
                           ST_GeomFromEWKT(geom_ewkt)
                    FROM {TMP}
                    WHERE geom_ewkt IS NOT NULL AND geom_ewkt <> ''
                """)
                dst_conn.commit()
                total_inserted += n

            elapsed = time.perf_counter() - t_batch
            pct = min(100.0, (offset + n) / total_src * 100)
            logger.info(
                f"  OK Batch {batch_num} en {elapsed:.1f}s | "
                f"total : {total_inserted:,} | {pct:.1f}%"
            )

            offset += n
            if n < batch_size:
                break

    finally:
        if dst_conn:
            dst_conn.close()

    elapsed_total = time.perf_counter() - t_global
    logger.info(
        f"Migration terminee en {elapsed_total:.1f}s | "
        f"{total_inserted:,} / {total_src:,} lignes inserees"
    )
    return total_inserted


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Migration PPM -> Ecocompensation (COPY)")
    parser.add_argument("--batch-size",         type=int, default=5_000)
    parser.add_argument("--dry-run",            action="store_true")
    parser.add_argument("--resume-from-offset", type=int, default=0)
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parent / ".env")

    logger.info("Connexion source (PPM)...")
    engine_src = _make_engine_src()

    logger.info("Connexion destination (ECOCOMP)...")
    dsn_dst = _make_dsn_dst()

    migrate(
        engine_src,
        dsn_dst,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        resume_from_offset=args.resume_from_offset,
    )


if __name__ == "__main__":
    main()