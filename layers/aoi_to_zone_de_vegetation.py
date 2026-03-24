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
    Lance le traitement AOI → ecocompensation_results.zone_de_vegetation pour le projet donné.

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

    # --- 1) Comptage des entités intersectées dans geo.zone_de_vegetation ---
    with engine.begin() as conn:
        log("[ZDV] Comptage des entités intersectant l'AOI...")
        t0 = time.perf_counter()
        count = conn.execute(
            text(
                """
                SELECT count(*)
                FROM geo.zone_de_vegetation
                WHERE ST_Intersects(
                        geom_2154,
                        ST_GeomFromText(:wkt_aoi, 2154)
                );
                """
            ),
            {"wkt_aoi": aoi_wkt},
        ).scalar_one()
        t1 = time.perf_counter()

    log(f"[ZDV] {count} entités intersectent l'AOI (en {t1 - t0:.2f} s).")

    if count == 0:
        log("[ZDV] Rien à insérer, fin.")
        return 0

    # --- 2) Sauvegarde directe dans ecocompensation_results.zone_de_vegetation ---
    log("[RESULTS] Insertion directe dans ecocompensation_results.zone_de_vegetation...")

    t2 = time.perf_counter()
    with engine.begin() as conn:
        # Schéma / table / index résultats
        conn.execute(
            text(
                """
                CREATE SCHEMA IF NOT EXISTS ecocompensation_results;

                CREATE TABLE IF NOT EXISTS ecocompensation_results.zone_de_vegetation (
                    id uuid NOT NULL DEFAULT gen_random_uuid(),
                    project_id uuid NULL,
                    id_src text NULL,
                    nature text NULL,
                    date_creat text NULL,
                    date_maj text NULL,
                    date_app date NULL,
                    date_conf date NULL,
                    acqu_plani text NULL,
                    prec_plani double precision NULL,
                    source text NULL,
                    id_source text NULL,
                    geom_2154 geometry(Geometry,2154) NOT NULL,
                    created_at timestamptz NULL DEFAULT now(),
                    PRIMARY KEY (id)
                );

                CREATE INDEX IF NOT EXISTS idx_ecocomp_results_zdv_geom
                    ON ecocompensation_results.zone_de_vegetation
                    USING GIST (geom_2154);

                CREATE INDEX IF NOT EXISTS idx_ecocomp_results_zdv_project
                    ON ecocompensation_results.zone_de_vegetation (project_id);
                """
            )
        )

        # Insertion des entités intersectées
        res = conn.execute(
            text(
                """
                INSERT INTO ecocompensation_results.zone_de_vegetation (
                    project_id,
                    id_src,
                    nature,
                    date_creat,
                    date_maj,
                    date_app,
                    date_conf,
                    acqu_plani,
                    prec_plani,
                    source,
                    id_source,
                    geom_2154
                )
                SELECT
                    :pid AS project_id,
                    z.id      AS id_src,
                    z.nature,
                    z.date_creat,
                    z.date_maj,
                    z.date_app,
                    z.date_conf,
                    z.acqu_plani,
                    z.prec_plani,
                    z.source,
                    z.id_source,
                    z.geom_2154
                FROM geo.zone_de_vegetation z
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
        f"ecocompensation_results.zone_de_vegetation pour project_id={project_id} "
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

