#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aoi_to_espaces_naturels_sensibles_ens.py
========================================

Intersection AOI × ecocompensation.espaces_naturels_sensibles_ens
→ ecocompensation_results.espaces_naturels_sensibles_ens.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

SOURCE_TABLE = "ecocompensation.espaces_naturels_sensibles_ens"
RESULTS_TABLE = "ecocompensation_results.espaces_naturels_sensibles_ens"

DATA_COLUMNS = [
    "id_com",
    "code_com",
    "parcelle",
    "section",
    "pre",
    "idu",
    "id_par",
    "supf",
    "texte",
    "nom_site",
    "commune",
    "date_acq",
    "refcad",
    "sup_calcul",
    "shape_leng",
    "shape_area",
    "geom_2154",
]


def _ensure_results_table(conn) -> None:
    conn.execute(text("CREATE SCHEMA IF NOT EXISTS ecocompensation_results;"))
    conn.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS {RESULTS_TABLE} (
                id uuid NOT NULL DEFAULT gen_random_uuid(),
                project_id uuid NULL,
                id_com text NULL,
                code_com text NULL,
                parcelle text NULL,
                section text NULL,
                pre text NULL,
                idu text NULL,
                id_par text NULL,
                supf double precision NULL,
                texte text NULL,
                nom_site text NULL,
                commune text NULL,
                date_acq text NULL,
                refcad text NULL,
                sup_calcul double precision NULL,
                shape_leng double precision NULL,
                shape_area double precision NULL,
                geom_2154 geometry(Geometry, 2154) NOT NULL,
                created_at timestamptz NULL DEFAULT now(),
                PRIMARY KEY (id)
            );
            """
        )
    )
    conn.execute(
        text(
            f"""
            CREATE INDEX IF NOT EXISTS idx_ecocomp_results_ens_geom
                ON {RESULTS_TABLE} USING GIST (geom_2154);
            CREATE INDEX IF NOT EXISTS idx_ecocomp_results_ens_project
                ON {RESULTS_TABLE} (project_id);
            """
        )
    )


def run(engine, project_id: str, aoi_id: str, cb=None) -> int:
    log = cb or (lambda msg: None)

    with engine.begin() as conn:
        row_aoi = conn.execute(
            text(
                """
                SELECT id,
                       ST_AsText(geom_2154) AS wkt_aoi,
                       ST_Area(geom_2154) AS area_m2
                FROM ecocompensation.aoi
                WHERE id = :aid;
                """
            ),
            {"aid": aoi_id},
        ).mappings().one_or_none()

    if row_aoi is None:
        log(f"[ENS] AOI id={aoi_id} introuvable, exécution annulée.")
        return 0

    aoi_wkt = row_aoi["wkt_aoi"]
    log(f"[ENS] AOI id={aoi_id}, surface ~ {row_aoi['area_m2'] / 10_000:.2f} ha")

    with engine.begin() as conn:
        source_exists = conn.execute(
            text("SELECT to_regclass(:r) IS NOT NULL"),
            {"r": SOURCE_TABLE},
        ).scalar_one()
    if not source_exists:
        log(f"[ENS] Table source {SOURCE_TABLE} absente.")
        with engine.begin() as conn:
            _ensure_results_table(conn)
        return 0

    with engine.begin() as conn:
        count = conn.execute(
            text(
                f"""
                SELECT count(*)
                FROM {SOURCE_TABLE} s
                WHERE s.geom_2154 IS NOT NULL
                  AND ST_Intersects(
                      s.geom_2154,
                      ST_GeomFromText(:wkt_aoi, 2154)
                  );
                """
            ),
            {"wkt_aoi": aoi_wkt},
        ).scalar_one()

    log(f"[ENS] {count} entité(s) intersectent l'AOI.")

    with engine.begin() as conn:
        _ensure_results_table(conn)
        conn.execute(
            text(f"DELETE FROM {RESULTS_TABLE} WHERE project_id = :pid"),
            {"pid": project_id},
        )

    if count == 0:
        log("[ENS] Aucune entité — table résultats prête (vide pour ce projet).")
        return 0

    cols_str = ", ".join(DATA_COLUMNS)
    select_cols = ", ".join(f"s.{c}" for c in DATA_COLUMNS)

    t0 = time.perf_counter()
    with engine.begin() as conn:
        res = conn.execute(
            text(
                f"""
                INSERT INTO {RESULTS_TABLE} (project_id, {cols_str})
                SELECT :project_id AS project_id, {select_cols}
                FROM {SOURCE_TABLE} s
                WHERE s.geom_2154 IS NOT NULL
                  AND ST_Intersects(
                      s.geom_2154,
                      ST_GeomFromText(:wkt_aoi, 2154)
                  );
                """
            ),
            {"project_id": project_id, "wkt_aoi": aoi_wkt},
        )

    rows = res.rowcount if res.rowcount is not None else count
    log(f"[ENS] {rows} entité(s) insérées dans {RESULTS_TABLE} ({time.perf_counter() - t0:.2f} s).")
    return rows


def main() -> None:
    base_dir = Path(__file__).resolve().parent
    load_dotenv(base_dir / ".env")

    host = os.getenv("SUPABASE_HOST")
    port = os.getenv("SUPABASE_PORT", "6543")
    db = os.getenv("SUPABASE_DB", "postgres")
    user = os.getenv("SUPABASE_USER")
    pwd = os.getenv("SUPABASE_PASSWORD")
    if not all([host, db, user, pwd]):
        raise RuntimeError("Variables DB manquantes (.env) pour ENS.")

    engine = create_engine(
        f"postgresql+psycopg://{user}:{quote_plus(pwd)}@{host}:{port}/{db}"
    )

    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, aoi_id
                FROM ecocompensation.projects
                WHERE aoi_id IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 1;
                """
            )
        ).mappings().one_or_none()
    if not row:
        print("Aucun projet avec AOI trouvé.")
        return

    n = run(engine, str(row["id"]), str(row["aoi_id"]), cb=print)
    print(f"Total ENS insérées : {n}")


if __name__ == "__main__":
    main()
