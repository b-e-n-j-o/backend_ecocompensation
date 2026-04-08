#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AOI → ecocompensation_results.carhab

Intersecte ``ecocompensation.carhab`` avec l’AOI et recopie les polygones
d’habitats (codes EUNIS, biotope, physio, etc.) dans les résultats projet.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

# Même logique dans le WHERE et le SELECT : géométrie ramenée en 2154 pour l’AOI.
_GEOM_2154 = """
CASE
    WHEN ST_SRID(c.geometry) = 0 THEN ST_SetSRID(c.geometry, 2154)
    WHEN ST_SRID(c.geometry) = 2154 THEN c.geometry
    ELSE ST_Transform(c.geometry, 2154)
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
            text(
                "SELECT to_regclass('ecocompensation.carhab') IS NOT NULL"
            ).execution_options(no_prepare=True)
        ).scalar_one()
        if not reg:
            log("[CARHAB] Table ecocompensation.carhab absente — rien à insérer.")
            return 0

    with engine.begin() as conn:
        log("[CARHAB] Comptage des entités intersectant l'AOI...")
        t0 = time.perf_counter()
        count = conn.execute(
            text(
                f"""
                SELECT count(*)
                FROM ecocompensation.carhab c
                WHERE c.geometry IS NOT NULL
                  AND ST_Intersects(
                        ({_GEOM_2154}),
                        ST_GeomFromText(:wkt_aoi, 2154)
                  );
                """
            ),
            {"wkt_aoi": aoi_wkt},
        ).scalar_one()
        t1 = time.perf_counter()

    log(f"[CARHAB] {count} entités intersectent l'AOI (en {t1 - t0:.2f} s).")

    if count == 0:
        log("[CARHAB] Rien à insérer, fin.")
        return 0

    log("[RESULTS] Insertion dans ecocompensation_results.carhab...")
    t2 = time.perf_counter()

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE SCHEMA IF NOT EXISTS ecocompensation_results;

                CREATE TABLE IF NOT EXISTS ecocompensation_results.carhab (
                    id uuid NOT NULL DEFAULT gen_random_uuid(),
                    project_id uuid NULL,
                    aoi_id uuid NULL,
                    id_polygone_carhab double precision NULL,
                    code_hab_carhab text NULL,
                    code_biotope text NULL,
                    code_physio text NULL,
                    code_eunis text NULL,
                    nom_eunis text NULL,
                    rang double precision NULL,
                    commentaire text NULL,
                    surface double precision NULL,
                    code_niv2 text NULL,
                    cd_hab_eunis double precision NULL,
                    id_sinp_evenement text NULL,
                    id_sinp_habitat text NULL,
                    conformite_biotope_departementale text NULL,
                    commentaire_conformite_biotope_departementale text NULL,
                    conformite_physio_departementale text NULL,
                    commentaire_conformite_physio_departementale text NULL,
                    geom_2154 geometry(Geometry, 2154) NOT NULL,
                    created_at timestamptz NULL DEFAULT now(),
                    PRIMARY KEY (id)
                );

                CREATE INDEX IF NOT EXISTS idx_ecocomp_results_carhab_geom
                    ON ecocompensation_results.carhab USING GIST (geom_2154);

                CREATE INDEX IF NOT EXISTS idx_ecocomp_results_carhab_project
                    ON ecocompensation_results.carhab (project_id);
                """
            )
        )

        res = conn.execute(
            text(
                f"""
                INSERT INTO ecocompensation_results.carhab (
                    project_id,
                    aoi_id,
                    id_polygone_carhab,
                    code_hab_carhab,
                    code_biotope,
                    code_physio,
                    code_eunis,
                    nom_eunis,
                    rang,
                    commentaire,
                    surface,
                    code_niv2,
                    cd_hab_eunis,
                    id_sinp_evenement,
                    id_sinp_habitat,
                    conformite_biotope_departementale,
                    commentaire_conformite_biotope_departementale,
                    conformite_physio_departementale,
                    commentaire_conformite_physio_departementale,
                    geom_2154
                )
                SELECT
                    CAST(:pid AS uuid) AS project_id,
                    CAST(:aid AS uuid) AS aoi_id,
                    c.id_polygone_carhab,
                    c.code_hab_carhab,
                    c.code_biotope,
                    c.code_physio,
                    c.code_eunis,
                    c.nom_eunis,
                    c.rang,
                    c.commentaire,
                    c.surface,
                    c.code_niv2,
                    c.cd_hab_eunis,
                    c.id_sinp_evenement,
                    c.id_sinp_habitat,
                    c.conformite_biotope_departementale,
                    c.commentaire_conformite_biotope_departementale,
                    c.conformite_physio_departementale,
                    c.commentaire_conformite_physio_departementale,
                    ({_GEOM_2154})::geometry(Geometry, 2154) AS geom_2154
                FROM ecocompensation.carhab c
                WHERE c.geometry IS NOT NULL
                  AND ST_Intersects(
                        ({_GEOM_2154}),
                        ST_GeomFromText(:wkt_aoi, 2154)
                  );
                """
            ),
            {"pid": project_id, "aid": aoi_id, "wkt_aoi": aoi_wkt},
        )

    t3 = time.perf_counter()
    rows_inserted = res.rowcount if res.rowcount is not None else count
    log(
        f"[RESULTS] {rows_inserted} entités insérées dans ecocompensation_results.carhab "
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
