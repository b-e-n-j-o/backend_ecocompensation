#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AOI -> ecocompensation_results.bd_topo_et_cesbio

Copie les entites de la couche hybride ``ecocompensation.vegetation_sur_cesbio``
intersectant l'AOI dans le schema de resultats projet.

La table source est supposee construite en amont avec priorite BD TOPO sur CESBIO:
- source='bdtopo' : ``nature`` renseigne, ``libelle`` NULL
- source='cesbio' : ``libelle`` renseigne, ``nature`` NULL
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv
from sqlalchemy import create_engine, text


def run(engine, project_id: str, aoi_id: str, cb=None) -> int:
    log = cb or (lambda msg: None)

    with engine.begin() as conn:
        row_aoi = conn.execute(
            text(
                """
                SELECT id,
                       ST_AsText(geom_2154) AS wkt_aoi,
                       ST_Area(geom_2154)   AS area_m2
                FROM ecocompensation.aoi
                WHERE id = :aid;
                """
            ),
            {"aid": aoi_id},
        ).mappings().one_or_none()

    if row_aoi is None:
        log(f"[AOI] AOI id={aoi_id} introuvable, execution annulee.")
        return 0

    aoi_wkt = row_aoi["wkt_aoi"]
    log(f"[AOI] AOI id={aoi_id}, surface ~ {row_aoi['area_m2']/10_000:.2f} ha")

    with engine.begin() as conn:
        reg = conn.execute(
            text("SELECT to_regclass('ecocompensation.vegetation_sur_cesbio') IS NOT NULL")
        ).scalar_one()
        if not reg:
            log("[HYBRIDE] Table ecocompensation.vegetation_sur_cesbio absente - rien a inserer.")
            return 0

    with engine.begin() as conn:
        log("[HYBRIDE] Comptage des entites intersectant l'AOI...")
        t0 = time.perf_counter()
        count = conn.execute(
            text(
                """
                SELECT count(*)
                FROM ecocompensation.vegetation_sur_cesbio h
                WHERE ST_Intersects(h.geom, ST_GeomFromText(:wkt_aoi, 2154));
                """
            ),
            {"wkt_aoi": aoi_wkt},
        ).scalar_one()
        t1 = time.perf_counter()

    log(f"[HYBRIDE] {count} entites intersectent l'AOI (en {t1 - t0:.2f} s).")
    if count == 0:
        log("[HYBRIDE] Rien a inserer, fin.")
        return 0

    log("[RESULTS] Insertion dans ecocompensation_results.bd_topo_et_cesbio...")
    t2 = time.perf_counter()

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE SCHEMA IF NOT EXISTS ecocompensation_results;

                CREATE TABLE IF NOT EXISTS ecocompensation_results.bd_topo_et_cesbio (
                    id uuid NOT NULL DEFAULT gen_random_uuid(),
                    project_id uuid NULL,
                    aoi_id uuid NULL,
                    source text NULL,
                    nature text NULL,
                    libelle text NULL,
                    libelle_prio text NULL,
                    geom_2154 geometry(Geometry, 2154) NOT NULL,
                    created_at timestamptz NULL DEFAULT now(),
                    PRIMARY KEY (id)
                );

                ALTER TABLE ecocompensation_results.bd_topo_et_cesbio
                    ADD COLUMN IF NOT EXISTS libelle_prio text NULL;

                CREATE INDEX IF NOT EXISTS idx_ecocomp_results_bdtopo_cesbio_geom
                    ON ecocompensation_results.bd_topo_et_cesbio USING GIST (geom_2154);

                CREATE INDEX IF NOT EXISTS idx_ecocomp_results_bdtopo_cesbio_project
                    ON ecocompensation_results.bd_topo_et_cesbio (project_id);
                """
            )
        )

        res = conn.execute(
            text(
                """
                INSERT INTO ecocompensation_results.bd_topo_et_cesbio (
                    project_id,
                    aoi_id,
                    source,
                    nature,
                    libelle,
                    libelle_prio,
                    geom_2154
                )
                SELECT
                    CAST(:pid AS uuid) AS project_id,
                    CAST(:aid AS uuid) AS aoi_id,
                    q.source,
                    q.nature,
                    q.libelle,
                    q.libelle_prio,
                    q.geom_clip AS geom_2154
                FROM (
                    SELECT
                        h.source,
                        h.nature,
                        h.libelle,
                        COALESCE(h.libelle_prio, h.nature, h.libelle) AS libelle_prio,
                        ST_Intersection(
                            h.geom,
                            ST_GeomFromText(:wkt_aoi, 2154)
                        ) AS geom_clip
                    FROM ecocompensation.vegetation_sur_cesbio h
                    WHERE ST_Intersects(h.geom, ST_GeomFromText(:wkt_aoi, 2154))
                ) AS q
                WHERE q.geom_clip IS NOT NULL
                  AND NOT ST_IsEmpty(q.geom_clip);
                """
            ),
            {"pid": project_id, "aid": aoi_id, "wkt_aoi": aoi_wkt},
        )

    t3 = time.perf_counter()
    rows_inserted = res.rowcount if res.rowcount is not None else count
    log(
        f"[RESULTS] {rows_inserted} entites inserees dans ecocompensation_results.bd_topo_et_cesbio "
        f"pour project_id={project_id} (en {t3 - t2:.2f} s)."
    )
    return rows_inserted


def main() -> None:
    BASE_DIR = Path(__file__).resolve().parent.parent
    load_dotenv(BASE_DIR / ".env")

    supabase_host = os.getenv("SUPABASE_HOST")
    supabase_port = os.getenv("SUPABASE_PORT", "6543")
    supabase_db = os.getenv("SUPABASE_DB", "postgres")
    supabase_user = os.getenv("SUPABASE_USER")
    supabase_password = os.getenv("SUPABASE_PASSWORD")

    if not all([supabase_host, supabase_db, supabase_user, supabase_password]):
        raise RuntimeError("Variables de connexion manquantes dans le .env.")

    password_quoted = quote_plus(supabase_password)
    db_url = (
        f"postgresql+psycopg://{supabase_user}:{password_quoted}"
        f"@{supabase_host}:{supabase_port}/{supabase_db}"
    )
    engine = create_engine(db_url)

    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT id, aoi_id FROM ecocompensation.projects "
                "WHERE aoi_id IS NOT NULL ORDER BY created_at DESC LIMIT 1;"
            )
        ).mappings().one_or_none()
    if not row:
        print("Aucun projet avec AOI trouve.")
        return

    project_id = str(row["id"])
    aoi_id = str(row["aoi_id"])

    n = run(engine, project_id, aoi_id, cb=print)
    print(f"Total insere : {n}")


if __name__ == "__main__":
    main()
