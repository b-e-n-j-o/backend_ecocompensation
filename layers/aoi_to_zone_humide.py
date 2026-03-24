#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
from pathlib import Path
from urllib.parse import quote_plus

from sqlalchemy import create_engine, text
from dotenv import load_dotenv


def run(engine, project_id: str, aoi_id: str, cb=None) -> int:
    """
    Lance le traitement AOI → ecocompensation_results.zone_humide pour le projet donné.

    :param engine: Engine SQLAlchemy déjà connecté.
    :param project_id: Identifiant du projet (écrit dans les lignes de résultat).
    :param aoi_id: Identifiant de l'AOI pour l'intersection géométrique.
    :param cb: Callback de log optionnel (cb(str)).
    :return: Nombre d'entités insérées.
    """
    log = cb or (lambda msg: None)

    # --- Lecture AOI depuis la base ---
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
        log(f"[AOI] AOI id={aoi_id} introuvable dans ecocompensation.aoi, exécution annulée.")
        return 0

    aoi_wkt = row_aoi["wkt_aoi"]
    aoi_area = row_aoi["area_m2"]
    log(f"[AOI] AOI id={aoi_id}, surface ~ {aoi_area/10_000:.2f} ha")

    # --- 1) Comptage des zones humides intersectées ---
    with engine.begin() as conn:
        log("[ZH] Comptage des entités intersectant l'AOI...")
        t0 = time.perf_counter()
        count = conn.execute(
            text(
                """
                SELECT count(*)
                FROM geo.zone_humide
                WHERE ST_Intersects(
                        geom_2154,
                        ST_GeomFromText(:wkt_aoi, 2154)
                );
                """
            ),
            {"wkt_aoi": aoi_wkt},
        ).scalar_one()
        t1 = time.perf_counter()

    log(f"[ZH] {count} entités intersectent l'AOI (en {t1 - t0:.2f} s).")

    if count == 0:
        log("[ZH] Rien à insérer, fin.")
        return 0

    # --- 2) Insertion directe dans ecocompensation_results.zone_humide ---
    log("[RESULTS] Insertion directe dans ecocompensation_results.zone_humide...")

    t2 = time.perf_counter()
    with engine.begin() as conn:
        # Schéma / table / index résultats
        conn.execute(
            text(
                """
                CREATE SCHEMA IF NOT EXISTS ecocompensation_results;

                CREATE TABLE IF NOT EXISTS ecocompensation_results.zone_humide (
                    id uuid NOT NULL DEFAULT gen_random_uuid(),
                    project_id uuid NULL,
                    source text NOT NULL,
                    inventaire_id text NULL,
                    libelle text NULL,
                    inv_nom text NULL,
                    geom_2154 geometry(Geometry,2154) NOT NULL,
                    created_at timestamptz NULL DEFAULT now(),
                    PRIMARY KEY (id)
                );

                CREATE INDEX IF NOT EXISTS idx_ecocomp_results_zh_geom
                    ON ecocompensation_results.zone_humide
                    USING GIST (geom_2154);

                CREATE INDEX IF NOT EXISTS idx_ecocomp_results_zh_project
                    ON ecocompensation_results.zone_humide (project_id);
                """
            )
        )

        # Insertion des zones humides intersectées
        res = conn.execute(
            text(
                """
                INSERT INTO ecocompensation_results.zone_humide (
                    project_id,
                    source,
                    inventaire_id,
                    libelle,
                    inv_nom,
                    geom_2154
                )
                SELECT
                    :pid AS project_id,
                    z.source,
                    z.inventaire_id,
                    z.libelle,
                    z.inv_nom,
                    ST_SetSRID(z.geom_2154, 2154)
                FROM geo.zone_humide z
                WHERE ST_Intersects(
                    z.geom_2154,
                    ST_GeomFromText(:wkt_aoi, 2154)
                );
                """
            ),
            {"pid": project_id, "wkt_aoi": aoi_wkt},
        )

    t3 = time.perf_counter()

    rows_inserted = res.rowcount if res.rowcount is not None else count
    log(
        f"[RESULTS] {rows_inserted} entités insérées dans "
        f"ecocompensation_results.zone_humide pour project_id={project_id} "
        f"(en {t3 - t2:.2f} s)."
    )
    return rows_inserted


def main() -> None:
    """
    Entrée CLI : construit son propre engine et utilise la dernière AOI.
    """
    from sqlalchemy import text as _text

    BASE_DIR = Path(__file__).resolve().parent
    load_dotenv(BASE_DIR / ".env")

    SUPABASE_HOST = os.getenv("SUPABASE_HOST")
    SUPABASE_PORT = os.getenv("SUPABASE_PORT", "6543")
    SUPABASE_DB = os.getenv("SUPABASE_DB", "postgres")
    SUPABASE_USER = os.getenv("SUPABASE_USER")
    SUPABASE_PASSWORD = os.getenv("SUPABASE_PASSWORD")

    if not all([SUPABASE_HOST, SUPABASE_DB, SUPABASE_USER, SUPABASE_PASSWORD]):
        raise RuntimeError("Variables de connexion à la base manquantes dans le .env.")

    password_quoted = quote_plus(SUPABASE_PASSWORD)
    db_url = (
        f"postgresql+psycopg://{SUPABASE_USER}:{password_quoted}"
        f"@{SUPABASE_HOST}:{SUPABASE_PORT}/{SUPABASE_DB}"
    )
    engine = create_engine(db_url)

    # Dernier projet et son AOI
    with engine.begin() as conn:
        row = conn.execute(
            _text(
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

