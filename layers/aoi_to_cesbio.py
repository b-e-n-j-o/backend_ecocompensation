#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AOI → ecocompensation_results.cesbio

Intersecte ``ecocompensation.cesbio`` avec l’AOI et recopie les polygones avec
``libelle_classe`` (colonne renseignée par la migration nomenclature, ou dérivée
du champ ``classe`` si besoin).
"""
from __future__ import annotations

import time
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from urllib.parse import quote_plus
import os

# Même logique que ajouter_nomenclature_cesbio.sql (CASE classe)
_LIBELLE_CASE = """
CASE c.classe
    WHEN 1  THEN 'Bâtis denses'
    WHEN 2  THEN 'Bâtis diffus'
    WHEN 3  THEN 'Zones industrielles et commerciales'
    WHEN 4  THEN 'Surfaces routes'
    WHEN 5  THEN 'Colza'
    WHEN 6  THEN 'Céréales à pailles'
    WHEN 7  THEN 'Protéagineux'
    WHEN 8  THEN 'Soja'
    WHEN 9  THEN 'Tournesol'
    WHEN 10 THEN 'Maïs'
    WHEN 11 THEN 'Riz'
    WHEN 12 THEN 'Tubercules/racines'
    WHEN 13 THEN 'Prairies'
    WHEN 14 THEN 'Vergers'
    WHEN 15 THEN 'Vignes'
    WHEN 16 THEN 'Forêts de feuillus'
    WHEN 17 THEN 'Forêts de conifères'
    WHEN 18 THEN 'Pelouses'
    WHEN 19 THEN 'Landes ligneuses'
    WHEN 20 THEN 'Surfaces minérales'
    WHEN 21 THEN 'Plages et dunes'
    WHEN 22 THEN 'Glaciers ou neiges'
    WHEN 23 THEN 'Eau'
    WHEN 24 THEN 'Autres'
    ELSE 'Inconnu'
END
"""


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
        log(f"[AOI] AOI id={aoi_id} introuvable, exécution annulée.")
        return 0

    aoi_wkt = row_aoi["wkt_aoi"]
    log(f"[AOI] AOI id={aoi_id}, surface ~ {row_aoi['area_m2']/10_000:.2f} ha")

    with engine.begin() as conn:
        reg = conn.execute(
            text("SELECT to_regclass('ecocompensation.cesbio') IS NOT NULL").execution_options(
                no_prepare=True
            )
        ).scalar_one()
        if not reg:
            log("[CESBIO] Table ecocompensation.cesbio absente — rien à insérer.")
            return 0

    with engine.begin() as conn:
        log("[CESBIO] Comptage des entités intersectant l'AOI...")
        t0 = time.perf_counter()
        count = conn.execute(
            text(
                """
                SELECT count(*)
                FROM ecocompensation.cesbio c
                WHERE ST_Intersects(c.geom, ST_GeomFromText(:wkt_aoi, 2154));
                """
            ),
            {"wkt_aoi": aoi_wkt},
        ).scalar_one()
        t1 = time.perf_counter()

    log(f"[CESBIO] {count} entités intersectent l'AOI (en {t1 - t0:.2f} s).")

    if count == 0:
        log("[CESBIO] Rien à insérer, fin.")
        return 0

    log("[RESULTS] Insertion dans ecocompensation_results.cesbio...")
    t2 = time.perf_counter()

    # Libellé dérivé de ``classe`` (aligné sur ajouter_nomenclature_cesbio.sql).
    libelle_sql = f"({_LIBELLE_CASE})::text"

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE SCHEMA IF NOT EXISTS ecocompensation_results;

                CREATE TABLE IF NOT EXISTS ecocompensation_results.cesbio (
                    id uuid NOT NULL DEFAULT gen_random_uuid(),
                    project_id uuid NULL,
                    aoi_id uuid NULL,
                    classe smallint NULL,
                    libelle_classe text NULL,
                    geom_2154 geometry(Geometry, 2154) NOT NULL,
                    created_at timestamptz NULL DEFAULT now(),
                    PRIMARY KEY (id)
                );

                CREATE INDEX IF NOT EXISTS idx_ecocomp_results_cesbio_geom
                    ON ecocompensation_results.cesbio USING GIST (geom_2154);

                CREATE INDEX IF NOT EXISTS idx_ecocomp_results_cesbio_project
                    ON ecocompensation_results.cesbio (project_id);
                """
            )
        )

        # libelle_classe : colonne source si présente (migration), sinon CASE
        res = conn.execute(
            text(
                f"""
                INSERT INTO ecocompensation_results.cesbio (
                    project_id,
                    aoi_id,
                    classe,
                    libelle_classe,
                    geom_2154
                )
                SELECT
                    CAST(:pid AS uuid) AS project_id,
                    CAST(:aid AS uuid) AS aoi_id,
                    c.classe,
                    {libelle_sql} AS libelle_classe,
                    c.geom AS geom_2154
                FROM ecocompensation.cesbio c
                WHERE ST_Intersects(c.geom, ST_GeomFromText(:wkt_aoi, 2154));
                """
            ),
            {"pid": project_id, "aid": aoi_id, "wkt_aoi": aoi_wkt},
        )

    t3 = time.perf_counter()
    rows_inserted = res.rowcount if res.rowcount is not None else count
    log(
        f"[RESULTS] {rows_inserted} entités insérées dans ecocompensation_results.cesbio "
        f"pour project_id={project_id} (en {t3 - t2:.2f} s)."
    )
    return rows_inserted


def main() -> None:
    BASE_DIR = Path(__file__).resolve().parent.parent
    load_dotenv(BASE_DIR / ".env")

    SUPABASE_HOST = os.getenv("SUPABASE_HOST")
    SUPABASE_PORT = os.getenv("SUPABASE_PORT", "6543")
    SUPABASE_DB = os.getenv("SUPABASE_DB", "postgres")
    SUPABASE_USER = os.getenv("SUPABASE_USER")
    SUPABASE_PASSWORD = os.getenv("SUPABASE_PASSWORD")

    if not all([SUPABASE_HOST, SUPABASE_DB, SUPABASE_USER, SUPABASE_PASSWORD]):
        raise RuntimeError("Variables de connexion manquantes dans le .env.")

    password_quoted = quote_plus(SUPABASE_PASSWORD)
    db_url = (
        f"postgresql+psycopg://{SUPABASE_USER}:{password_quoted}"
        f"@{SUPABASE_HOST}:{SUPABASE_PORT}/{SUPABASE_DB}"
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
        print("Aucun projet avec AOI trouvé.")
        return
    project_id = str(row["id"])
    aoi_id = str(row["aoi_id"])

    n = run(engine, project_id, aoi_id, cb=print)
    print(f"Total inséré : {n}")


if __name__ == "__main__":
    main()
