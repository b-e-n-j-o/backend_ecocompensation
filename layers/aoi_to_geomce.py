#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aoi_to_geomce.py
================

Intersection de l'AOI courante avec les couches de mesures compensatoires
déjà ingérées en base (schema `geo`) :

    - geo.mesures_compensatoire_surf   (MultiPolygon)
    - geo.mesures_compensatoire_lin    (MultiLineString)
    - geo.mesures_compensatoire_pct    (MultiPoint)
    - geo.mesures_compensatoire_commune (MultiPolygon)

Les entités intersectant l'AOI sont copiées dans le schéma
`ecocompensation_results`, dans des tables parallèles :

    - ecocompensation_results.mesures_compensatoire_surf
    - ecocompensation_results.mesures_compensatoire_lin
    - ecocompensation_results.mesures_compensatoire_pct
    - ecocompensation_results.mesures_compensatoire_commune

Chaque table de résultats contient :

    id uuid (PK), project_id, toutes les colonnes attributaires d'origine,
    geom_2154, created_at.
"""

import os
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus

from sqlalchemy import create_engine, text
from dotenv import load_dotenv


TABLES_CONFIG = [
    {
        "src": "geo.mesures_compensatoire_surf",
        "dst": "ecocompensation_results.mesures_compensatoire_surf",
        "label": "surf",
    },
    {
        "src": "geo.mesures_compensatoire_lin",
        "dst": "ecocompensation_results.mesures_compensatoire_lin",
        "label": "lin",
    },
    {
        "src": "geo.mesures_compensatoire_pct",
        "dst": "ecocompensation_results.mesures_compensatoire_pct",
        "label": "pct",
    },
    {
        "src": "geo.mesures_compensatoire_commune",
        "dst": "ecocompensation_results.mesures_compensatoire_commune",
        "label": "commune",
    },
]


def ensure_results_table(conn, dst_table: str):
    """
    Crée la table de résultats si besoin, avec schéma standard.
    On ne recopie pas automatiquement la structure, on re-déclare
    explicitement les colonnes de l'ingestion.
    """
    schema, table = dst_table.split(".")
    conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema};"))

    conn.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS {dst_table} (
                id uuid NOT NULL DEFAULT gen_random_uuid(),
                project_id uuid NULL,
                categorie text NULL,
                classe text NULL,
                type text NULL,
                sous_categorie text NULL,
                type_procedure text NULL,
                theme text NULL,
                projet text NULL,
                maitre_ouvrage text NULL,
                origine_si text NULL,
                dossier_no text NULL,
                duree text NULL,
                date_decision date NULL,
                identifiant integer NULL,
                l_dep text NULL,
                liste_communes text NULL,
                geom_2154 geometry(Geometry,2154) NOT NULL,
                created_at timestamptz NULL DEFAULT now(),
                PRIMARY KEY (id)
            );

            CREATE INDEX IF NOT EXISTS idx_{table}_geom
                ON {dst_table}
                USING GIST (geom_2154);

            CREATE INDEX IF NOT EXISTS idx_{table}_project
                ON {dst_table} (project_id);
            """
        )
    )


def run(engine, project_id: str, aoi_id: str, cb=None) -> int:
    """
    Intersecte l'AOI donnée avec les tables GEOMCE et insère
    dans les tables ecocompensation_results.mesures_compensatoire_*.

    :param engine: Engine SQLAlchemy déjà connecté.
    :param project_id: Identifiant du projet (écrit dans les lignes de résultat).
    :param aoi_id: Identifiant de l'AOI pour l'intersection géométrique.
    :param cb: Callback de log optionnel (cb(str)).
    :return: Nombre total d'entités insérées (toutes tables confondues).
    """
    log = cb or (lambda msg: None)

    # 1) Récupérer l'AOI courante (pour ce aoi_id)
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

    with engine.begin() as conn:
        # S'assurer que le schéma de résultats existe
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS ecocompensation_results;"))

    total_inserted = 0

    # 2) Traiter chaque table source
    for cfg in TABLES_CONFIG:
        src = cfg["src"]
        dst = cfg["dst"]
        label = cfg["label"]

        log(f"\n[{label.upper()}] Traitement {src} → {dst}")

        with engine.begin() as conn:
            # Vérifier existence de la table source
            exists = conn.execute(
                text(
                    """
                    SELECT to_regclass(:reg) IS NOT NULL AS exists
                    """
                ),
                {"reg": src},
            ).scalar_one()

            if not exists:
                log(f"[{label.upper()}] ⚠️ Table source {src} introuvable, on saute.")
                continue

            # Compter les entités intersectées
            log(f"[{label.upper()}] Comptage des entités intersectant l'AOI...")
            t0 = time.perf_counter()
            count = conn.execute(
                text(
                    f"""
                    SELECT count(*)
                    FROM {src} s
                    WHERE ST_Intersects(
                        s.geom_2154,
                        ST_GeomFromText(:wkt_aoi, 2154)
                    );
                    """
                ),
                {"wkt_aoi": aoi_wkt},
            ).scalar_one()
            t1 = time.perf_counter()

        log(f"[{label.upper()}] {count} entités intersectent l'AOI (en {t1 - t0:.2f} s).")

        if count == 0:
            log(f"[{label.upper()}] Rien à insérer pour {src}.")
            continue

        # Insertion dans la table de résultats
        log(f"[RESULTS-{label.upper()}] Insertion dans {dst} ...")
        t2 = time.perf_counter()
        with engine.begin() as conn:
            ensure_results_table(conn, dst)
            conn.execute(text(f"DELETE FROM {dst} WHERE project_id = :pid"), {"pid": project_id})
            res = conn.execute(
                text(
                    f"""
                    INSERT INTO {dst} (
                        project_id,
                        categorie,
                        classe,
                        type,
                        sous_categorie,
                        type_procedure,
                        theme,
                        projet,
                        maitre_ouvrage,
                        origine_si,
                        dossier_no,
                        duree,
                        date_decision,
                        identifiant,
                        l_dep,
                        liste_communes,
                        geom_2154
                    )
                    SELECT
                        :pid AS project_id,
                        s.categorie,
                        s.classe,
                        s.type,
                        s.sous_categorie,
                        s.type_procedure,
                        s.theme,
                        s.projet,
                        s.maitre_ouvrage,
                        s.origine_si,
                        s.dossier_no,
                        s.duree,
                        s.date_decision,
                        s.identifiant,
                        s.l_dep,
                        s.liste_communes,
                        ST_SetSRID(s.geom_2154, 2154)
                    FROM {src} s
                    WHERE ST_Intersects(
                        s.geom_2154,
                        ST_GeomFromText(:wkt_aoi, 2154)
                    );
                    """
                ),
                {"project_id": project_id, "wkt_aoi": aoi_wkt},
            )
        t3 = time.perf_counter()

        rows_inserted = res.rowcount if res.rowcount is not None else count
        log(
            f"[RESULTS-{label.upper()}] {rows_inserted} entités insérées dans {dst} "
            f"pour project_id={project_id} (en {t3 - t2:.2f} s)."
        )
        total_inserted += rows_inserted

    return total_inserted


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
        raise RuntimeError("Variables de connexion à la base manquantes dans le .env (GEOMCE).")

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
    print(f"Total inséré (toutes tables GEOMCE) : {n}")


if __name__ == "__main__":
    main()


