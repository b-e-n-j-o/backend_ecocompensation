#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_enrich_veg_staging.py
=========================

Script de diagnostic dédié "veg tagging" :
  1) lit les parcelles candidates déjà présentes dans ecocompensation_results.parcelles
     pour un project_id
  2) construit une table de staging veg filtrée sur la bbox des parcelles (+ marge)
  3) exécute l'UPDATE de tagging veg_libelles en une passe
  4) loggue précisément timings + compteurs + erreurs

Objectif : reproduire/profiler les lenteurs/timeout hors FastAPI / orchestrateur.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass

from sqlalchemy import text

from db import get_engine


@dataclass
class Cfg:
    project_id: str
    margin_m: int = 500
    statement_timeout: str = "90s"
    work_mem: str | None = None
    keep_staging: bool = False
    explain: bool = False


def _now() -> float:
    return time.perf_counter()


def _log(msg: str) -> None:
    print(msg, flush=True)


def _staging_table(project_id: str) -> str:
    safe = project_id.lower().replace("-", "_")
    return f"ecocompensation_staging.veg_{safe}"


def _count_candidates(engine, project_id: str) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM ecocompensation_results.parcelles "
                    "WHERE project_id = :pid"
                ),
                {"pid": project_id},
            ).scalar_one()
        )


def _ensure_schema(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS ecocompensation_staging"))


def _drop_staging(engine, staging_table: str) -> None:
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {staging_table}"))


def _create_staging(
    engine,
    *,
    staging_table: str,
    project_id: str,
    margin_m: int,
    statement_timeout: str,
) -> None:
    # IMPORTANT: 1 statement par execute (psycopg / Supabase / pgBouncer)
    with engine.begin() as conn:
        conn.execute(text(f"SET LOCAL statement_timeout = '{statement_timeout}'"))
        conn.execute(text(f"DROP TABLE IF EXISTS {staging_table}"))
        conn.execute(
            text(
                f"""
                CREATE TABLE {staging_table} AS
                SELECT v.libelle_prio, v.geom
                FROM ecocompensation.vegetation_sur_cesbio v
                WHERE v.libelle_prio IS NOT NULL
                  AND ST_Intersects(
                        v.geom,
                        (
                            SELECT ST_Buffer(
                                ST_Envelope(ST_Collect(p.geom_2154)),
                                :margin_m
                            )
                            FROM ecocompensation_results.parcelles p
                            WHERE p.project_id = :pid
                        )
                      )
                """
            ),
            {"pid": project_id, "margin_m": margin_m},
        )

        staging_index = f"idx_{staging_table.split('.', 1)[1]}_geom"
        conn.execute(
            text(f"CREATE INDEX IF NOT EXISTS {staging_index} ON {staging_table} USING GIST (geom)")
        )
        conn.execute(text(f"ANALYZE {staging_table}"))


def _count_staging(engine, staging_table: str) -> int:
    with engine.begin() as conn:
        return int(conn.execute(text(f"SELECT COUNT(*) FROM {staging_table}")).scalar_one())


def _explain_tag_plan(engine, *, project_id: str, staging_table: str) -> None:
    sql = f"""
    EXPLAIN (ANALYZE, BUFFERS)
    SELECT p.idu, array_agg(DISTINCT v.libelle_prio)
    FROM ecocompensation_results.parcelles p
    JOIN {staging_table} v
        ON v.geom && p.geom_2154
       AND ST_Intersects(v.geom, p.geom_2154)
    WHERE p.project_id = :pid
    GROUP BY p.idu
    """
    with engine.begin() as conn:
        rows = conn.execute(text(sql), {"pid": project_id}).fetchall()
    _log("[EXPLAIN] plan jointure parcelles × staging :")
    for row in rows:
        _log(f"  {row[0]}")


_SQL_TAG_TEMPLATE = """
WITH hits AS (
    SELECT DISTINCT
        p.idu,
        v.libelle_prio
    FROM ecocompensation_results.parcelles p
    JOIN {staging_table} v
        ON v.geom && p.geom_2154
       AND ST_Intersects(v.geom, p.geom_2154)
    WHERE p.project_id = :pid
      AND v.libelle_prio IS NOT NULL
),
veg_agg AS (
    SELECT
        idu,
        array_agg(libelle_prio) AS veg_libelles
    FROM hits
    GROUP BY idu
)
UPDATE ecocompensation_results.parcelles p
SET    veg_libelles = COALESCE(va.veg_libelles, '{{}}')
FROM   veg_agg va
WHERE  p.project_id = :pid
  AND  p.idu        = va.idu;
"""


def _tag_all(
    engine,
    *,
    project_id: str,
    staging_table: str,
    statement_timeout: str,
    work_mem: str | None,
) -> int:
    sql = text(_SQL_TAG_TEMPLATE.format(staging_table=staging_table))
    with engine.begin() as conn:
        conn.execute(text(f"SET LOCAL statement_timeout = '{statement_timeout}'"))
        if work_mem:
            conn.execute(text(f"SET LOCAL work_mem = '{work_mem}'"))
        res = conn.execute(sql, {"pid": project_id})
        return res.rowcount or 0


def _count_nonempty(engine, project_id: str) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM ecocompensation_results.parcelles "
                    "WHERE project_id = :pid AND cardinality(veg_libelles) > 0"
                ),
                {"pid": project_id},
            ).scalar_one()
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("project_id", help="UUID du projet")
    ap.add_argument(
        "--margin-m",
        type=int,
        default=500,
        help="Marge autour de la bbox des parcelles candidates (staging veg)",
    )
    ap.add_argument(
        "--statement-timeout",
        default="90s",
        help="SET LOCAL statement_timeout (ex: 60s, 120s)",
    )
    ap.add_argument(
        "--work-mem",
        default=None,
        help="SET LOCAL work_mem (ex: 64MB, 256MB) pour éviter le spill sur DISTINCT/agg",
    )
    ap.add_argument("--keep-staging", action="store_true", help="Ne pas DROP la table staging (debug)")
    ap.add_argument(
        "--explain",
        action="store_true",
        help="Affiche EXPLAIN (ANALYZE, BUFFERS) du join avant l'UPDATE",
    )
    args = ap.parse_args()

    cfg = Cfg(
        project_id=args.project_id,
        margin_m=args.margin_m,
        statement_timeout=args.statement_timeout,
        work_mem=args.work_mem,
        keep_staging=bool(args.keep_staging),
        explain=bool(args.explain),
    )

    engine = get_engine()

    _log(f"[CFG] project_id={cfg.project_id}")
    _log(
        f"[CFG] margin_m={cfg.margin_m} statement_timeout={cfg.statement_timeout} "
        f"explain={cfg.explain}"
    )

    t_global = _now()

    n = _count_candidates(engine, cfg.project_id)
    _log(f"[CANDIDATES] {n:,} parcelles candidates")
    if n == 0:
        return

    staging_table = _staging_table(cfg.project_id)
    _log(f"[STAGING] table={staging_table}")

    _ensure_schema(engine)
    if not cfg.keep_staging:
        _drop_staging(engine, staging_table)

    _log("[STAGING] build staging (bbox parcelles + marge)…")
    t0 = _now()
    _create_staging(
        engine,
        staging_table=staging_table,
        project_id=cfg.project_id,
        margin_m=cfg.margin_m,
        statement_timeout=cfg.statement_timeout,
    )
    _log(f"[STAGING] ✓ créé en {(_now() - t0):.2f}s")

    n_st = _count_staging(engine, staging_table)
    _log(f"[STAGING] rows={n_st:,}")

    if cfg.explain:
        _explain_tag_plan(engine, project_id=cfg.project_id, staging_table=staging_table)

    _log("[TAG] UPDATE veg_libelles (une passe)…")
    t1 = _now()
    updated_total = _tag_all(
        engine,
        project_id=cfg.project_id,
        staging_table=staging_table,
        statement_timeout=cfg.statement_timeout,
        work_mem=cfg.work_mem,
    )
    _log(f"[TAG] ✓ done en {(_now() - t1):.2f}s updated_total={updated_total:,}")

    n_nonempty = _count_nonempty(engine, cfg.project_id)
    _log(f"[RESULT] veg_libelles non-vide: {n_nonempty:,}/{n:,}")

    if not cfg.keep_staging:
        _log("[STAGING] drop staging…")
        _drop_staging(engine, staging_table)
        _log("[STAGING] ✓ dropped")

    _log(f"[DONE] elapsed_total={(_now() - t_global):.2f}s")


if __name__ == "__main__":
    main()
