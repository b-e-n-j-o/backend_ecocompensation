#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aoi_to_frayeres.py
===================
Intersection AOI × ecocompensation.frayeres → ecocompensation_results.frayeres.

- Lit l’AOI depuis ecocompensation.aoi (pour un aoi_id donné).
- Sélectionne, dans ecocompensation.frayeres, les frayères qui intersectent l’AOI.
- Écrit le résultat dans ecocompensation_results.frayeres.

Ce script suppose que l’ETL `etl_frayeres.py` a déjà rempli ecocompensation.frayeres
avec une géométrie `geom_2154` en EPSG:2154.
"""

import os
import time
from pathlib import Path
from urllib.parse import quote_plus

from sqlalchemy import create_engine, text
from dotenv import load_dotenv


def run(engine, project_id: str, aoi_id: str, cb=None) -> int:
    """
    Intersection AOI × ecocompensation.frayeres → ecocompensation_results.frayeres.

    :param engine: Engine SQLAlchemy déjà connecté.
    :param project_id: Identifiant du projet (écrit dans les lignes de résultat).
    :param aoi_id: Identifiant de l'AOI pour la géométrie.
    :param cb: Callback de log optionnel (cb(str)).
    :return: Nombre d'entités insérées.
    """
    log = cb or (lambda msg: None)

    # 1) AOI depuis la base (pour CE aoi_id)
    with engine.begin() as conn:
        row_aoi = conn.execute(
            text(
                """
                SELECT id,
                       ST_AsText(geom_2154) AS wkt_aoi,
                       ST_Area(geom_2154)  AS area_m2
                FROM ecocompensation.aoi
                WHERE id = :aid;
                """
            ),
            {"aid": aoi_id},
        ).mappings().one_or_none()

    if row_aoi is None:
        log(
            f"[FRAYERES] Aucune AOI trouvée dans ecocompensation.aoi pour id={aoi_id}, exécution annulée."
        )
        return 0

    aoi_wkt = row_aoi["wkt_aoi"]
    aoi_area = row_aoi["area_m2"]
    log(f"[FRAYERES] AOI id={aoi_id}, surface ~ {aoi_area / 10_000:.2f} ha")

    # 2) Colonnes de ecocompensation.frayeres (pour créer la table results et l’INSERT)
    with engine.begin() as conn:
        cols = conn.execute(
            text(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'ecocompensation'
                  AND table_name = 'frayeres'
                ORDER BY ordinal_position;
                """
            )
        ).scalars().all()

    if not cols:
        log(
            "[FRAYERES] Table ecocompensation.frayeres introuvable ou vide. "
            "Exécuter d’abord etl_frayeres.py."
        )
        return 0

    col_list = [c for c in cols]
    cols_str = ", ".join(col_list)
    select_cols_str = ", ".join(f"f.{c}" for c in col_list)

    # 3) Schéma + table results (id, project_id + LIKE ecocompensation.frayeres)
    with engine.begin() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS ecocompensation_results;"))
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS ecocompensation_results.frayeres (
                    id uuid NOT NULL DEFAULT gen_random_uuid(),
                    project_id uuid NULL,
                    LIKE ecocompensation.frayeres INCLUDING DEFAULTS,
                    PRIMARY KEY (id)
                );
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_ecocomp_results_frayeres_geom
                    ON ecocompensation_results.frayeres USING GIST (geom_2154);
                CREATE INDEX IF NOT EXISTS idx_ecocomp_results_frayeres_project
                    ON ecocompensation_results.frayeres (project_id);
                """
            )
        )
        conn.execute(
            text("DELETE FROM ecocompensation_results.frayeres WHERE project_id = :pid"),
            {"pid": project_id},
        )

    # 4) Comptage puis insertion
    with engine.begin() as conn:
        count = conn.execute(
            text(
                """
                SELECT count(*)
                FROM ecocompensation.frayeres f
                WHERE ST_Intersects(
                    f.geom_2154,
                    ST_GeomFromText(:wkt_aoi, 2154)
                );
                """
            ),
            {"wkt_aoi": aoi_wkt},
        ).scalar_one()

    log(f"[FRAYERES] {count} entités de ecocompensation.frayeres intersectent l'AOI.")

    if count == 0:
        log("[FRAYERES] Rien à insérer.")
        return 0

    t0 = time.perf_counter()
    with engine.begin() as conn:
        res = conn.execute(
            text(
                f"""
                INSERT INTO ecocompensation_results.frayeres (project_id, {cols_str})
                SELECT :project_id AS project_id, {select_cols_str}
                FROM ecocompensation.frayeres f
                WHERE ST_Intersects(
                    f.geom_2154,
                    ST_GeomFromText(:wkt_aoi, 2154)
                );
                """
            ),
            {"project_id": project_id, "wkt_aoi": aoi_wkt},
        )
    t1 = time.perf_counter()

    rows = res.rowcount if res.rowcount is not None else count
    log(
        f"[RESULTS FRAYERES] {rows} entités insérées dans ecocompensation_results.frayeres "
        f"(en {t1 - t0:.2f} s)."
    )
    return rows


def main():
    """
    Entrée CLI : construit son propre engine et utilise la dernière AOI.
    """
    BASE_DIR = Path(__file__).resolve().parent
    load_dotenv(BASE_DIR / ".env")

    SUPABASE_HOST = os.getenv("SUPABASE_HOST")
    SUPABASE_PORT = os.getenv("SUPABASE_PORT", "6543")
    SUPABASE_DB = os.getenv("SUPABASE_DB", "postgres")
    SUPABASE_USER = os.getenv("SUPABASE_USER")
    SUPABASE_PASSWORD = os.getenv("SUPABASE_PASSWORD")

    if not all([SUPABASE_HOST, SUPABASE_DB, SUPABASE_USER, SUPABASE_PASSWORD]):
        raise RuntimeError(
            "Variables de connexion à la base manquantes dans le .env (FRAYERES)."
        )

    password_quoted = quote_plus(SUPABASE_PASSWORD)
    db_url = (
        f"postgresql+psycopg://{SUPABASE_USER}:{password_quoted}"
        f"@{SUPABASE_HOST}:{SUPABASE_PORT}/{SUPABASE_DB}"
    )
    engine = create_engine(db_url)

    # Dernier projet et son AOI
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
    print(f"Total frayères insérées : {n}")


if __name__ == "__main__":
    main()

