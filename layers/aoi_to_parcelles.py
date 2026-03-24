#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aoi_to_parcelles.py
===================

Construit la couche ecocompensation_results.parcelles comme intersection
de l'AOI (ecocompensation.aoi) avec les parcelles déjà en base
(ecocompensation.parcelles). Tout se fait en SQL côté Supabase (pas de
transfert de données vers Python). Optimisations : CTE pour lire l'AOI une
seule fois, filtre bbox (&&) puis ST_Intersects pour utiliser l'index GIST.
"""

import logging
import time
from pathlib import Path

from sqlalchemy import create_engine, text
from dotenv import load_dotenv
from urllib.parse import quote_plus
import os

# =============================
# CONFIG
# =============================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


def create_results_table_if_not_exists(conn):
    """Crée ecocompensation_results.parcelles si besoin (même structure que ecocompensation.parcelles + project_id)."""
    conn.execute(text("CREATE SCHEMA IF NOT EXISTS ecocompensation_results;"))

    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS ecocompensation_results.parcelles (
                id text NULL,
                gid integer NULL,
                numero text NULL,
                feuille integer NULL,
                section text NULL,
                code_dep text NULL,
                nom_com text NULL,
                code_com text NULL,
                com_abs text NULL,
                code_arr text NULL,
                idu text NULL,
                contenance double precision NULL,
                code_insee text NULL,
                geom_2154 geometry(Geometry, 2154) NULL,
                project_id uuid NULL
            );
            """
        )
    )

    conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS idx_parcelles_results_geom_2154
                ON ecocompensation_results.parcelles
                USING GIST (geom_2154);
            """
        )
    )

    conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS idx_parcelles_results_project_id
                ON ecocompensation_results.parcelles (project_id);
            """
        )
    )


def run(engine, project_id: str, aoi_id: str, cb=None) -> int:
    """
    Construit ecocompensation_results.parcelles pour le projet donné (géométrie = AOI).

    :param engine: Engine SQLAlchemy déjà connecté.
    :param project_id: Identifiant du projet (écrit dans les lignes de résultat).
    :param aoi_id: Identifiant de l'AOI pour l'intersection géométrique.
    :param cb: Callback de log optionnel (cb(str)).
    :return: Nombre de parcelles insérées.
    """
    log = cb or (lambda msg: None)

    # 1) Vérifier que l'AOI existe et récupérer sa surface pour les logs
    with engine.begin() as conn:
        row_aoi = conn.execute(
            text(
                """
                SELECT id,
                       ST_Area(geom_2154) AS area_m2
                FROM ecocompensation.aoi
                WHERE id = :aid;
                """
            ),
            {"aid": aoi_id},
        ).mappings().one_or_none()

    if row_aoi is None:
        log(f"⚠️ AOI id={aoi_id} introuvable dans ecocompensation.aoi.")
        return 0

    area_ha = row_aoi["area_m2"] / 10_000 if row_aoi["area_m2"] else 0
    log(f"🔗 AOI utilisée : id={aoi_id}, surface ~ {area_ha:.2f} ha")

    # 2) Créer la table de résultats si besoin
    with engine.begin() as conn:
        create_results_table_if_not_exists(conn)

    # 3) Supprimer les anciennes parcelles pour ce projet (éviter doublons)
    with engine.begin() as conn:
        deleted = conn.execute(
            text("DELETE FROM ecocompensation_results.parcelles WHERE project_id = :pid"),
            {"pid": project_id},
        ).rowcount
    if deleted and deleted > 0:
        log(f"🧹 {deleted:,} anciennes parcelles supprimées pour project_id={project_id}")

    # 4) Intersection en SQL : parcelles en base qui intersectent l'AOI → results
    # Optimisations :
    # - CTE pour lire l'AOI une seule fois
    # - Filtre bbox (&&) en premier pour utiliser l'index GIST avant ST_Intersects
    log("✂️ Intersection AOI × ecocompensation.parcelles (ST_Intersects)...")
    t0 = time.perf_counter()

    with engine.begin() as conn:
        result = conn.execute(
            text(
                """
                WITH aoi AS (
                    SELECT geom_2154
                    FROM ecocompensation.aoi
                    WHERE id = :aid
                )
                INSERT INTO ecocompensation_results.parcelles (
                    id, gid, numero, feuille, section, code_dep, nom_com,
                    code_com, com_abs, code_arr, idu, contenance, code_insee,
                    geom_2154, project_id
                )
                SELECT
                    p.id, p.gid, p.numero, p.feuille, p.section, p.code_dep,
                    p.nom_com, p.code_com, p.com_abs, p.code_arr, p.idu,
                    p.contenance, p.code_insee,
                    ST_Multi(p.geom_2154) AS geom_2154,
                    :pid AS project_id
                FROM ecocompensation.parcelles p
                CROSS JOIN aoi
                WHERE p.geom_2154 && aoi.geom_2154
                  AND ST_Intersects(p.geom_2154, aoi.geom_2154);
                """
            ),
            {"aid": aoi_id, "pid": project_id},
        )
        inserted = result.rowcount if result.rowcount is not None else 0

    t1 = time.perf_counter()

    log(
        f"✅ {inserted:,} parcelles insérées dans ecocompensation_results.parcelles "
        f"pour project_id={project_id} (en {t1 - t0:.2f} s)."
    )

    # 5) Taille de la table results (best-effort)
    try:
        with engine.begin() as conn:
            size_bytes = conn.execute(
                text("SELECT pg_total_relation_size('ecocompensation_results.parcelles');")
            ).scalar_one()
        log(
            f"🗄️ Taille totale de la table ecocompensation_results.parcelles "
            f"~ {size_bytes / 1_000_000:.2f} Mo."
        )
    except Exception as e:
        log(f"⚠️ Impossible de récupérer la taille de la table en base: {e}")

    return inserted


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
        raise RuntimeError("Variables de connexion à la base manquantes dans le .env (AOI).")

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
    print(f"Total inséré : {n}")


if __name__ == "__main__":
    main()

