#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AOI → ecocompensation_results.remontee_de_nappes
================================================
Intersecte la couche nationale ``ecocompensation.remontee_de_nappes`` avec l’AOI
et insère les entités dans les résultats projet.
"""

from sqlalchemy import text


def run(engine, project_id: str, aoi_id: str, cb=None) -> int:
    log = cb or (lambda _msg: None)

    with engine.begin() as conn:
        tbl_ok = conn.execute(
            text("SELECT to_regclass('ecocompensation.remontee_de_nappes') IS NOT NULL")
        ).scalar_one()
        if not tbl_ok:
            log("[REMONTÉE NAPPES] Table ecocompensation.remontee_de_nappes absente — rien à insérer.")
            return 0

        row_aoi = conn.execute(
            text(
                """
                SELECT ST_AsText(geom_2154) AS wkt_aoi
                FROM ecocompensation.aoi
                WHERE id = :aid;
                """
            ),
            {"aid": aoi_id},
        ).mappings().one_or_none()

    if row_aoi is None:
        log(f"[AOI] AOI id={aoi_id} introuvable, remontée de nappes annulée.")
        return 0

    aoi_wkt = row_aoi["wkt_aoi"]

    with engine.begin() as conn:
        count = conn.execute(
            text(
                """
                SELECT count(*)
                FROM ecocompensation.remontee_de_nappes r
                WHERE ST_Intersects(r.geom_2154, ST_GeomFromText(:wkt_aoi, 2154));
                """
            ),
            {"wkt_aoi": aoi_wkt},
        ).scalar_one()

    log(f"[REMONTÉE NAPPES] {count} entités intersectent l’AOI.")
    if count == 0:
        return 0

    log("[RESULTS] Insertion dans ecocompensation_results.remontee_de_nappes...")
    n_inserted = 0
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE SCHEMA IF NOT EXISTS ecocompensation_results;

                CREATE TABLE IF NOT EXISTS ecocompensation_results.remontee_de_nappes (
                    id uuid NOT NULL DEFAULT gen_random_uuid(),
                    project_id uuid NULL,
                    aoi_id uuid NULL,
                    source_index bigint NULL,
                    classe text NULL,
                    fiab_mnt text NULL,
                    fiab_eso text NULL,
                    fiab_tot text NULL,
                    classefiab text NULL,
                    gridcode bigint NULL,
                    geom_2154 geometry(Geometry, 2154) NOT NULL,
                    created_at timestamptz NULL DEFAULT now(),
                    PRIMARY KEY (id)
                );

                CREATE INDEX IF NOT EXISTS idx_ecocomp_results_remontee_de_nappes_geom
                    ON ecocompensation_results.remontee_de_nappes USING gist (geom_2154);

                CREATE INDEX IF NOT EXISTS idx_ecocomp_results_remontee_de_nappes_project
                    ON ecocompensation_results.remontee_de_nappes (project_id);

                CREATE INDEX IF NOT EXISTS idx_ecocomp_results_remontee_de_nappes_classefiab
                    ON ecocompensation_results.remontee_de_nappes (classefiab);
                """
            )
        )

        res = conn.execute(
            text(
                """
                INSERT INTO ecocompensation_results.remontee_de_nappes (
                    project_id,
                    aoi_id,
                    source_index,
                    classe,
                    fiab_mnt,
                    fiab_eso,
                    fiab_tot,
                    classefiab,
                    gridcode,
                    geom_2154
                )
                SELECT
                    CAST(:pid AS uuid),
                    CAST(:aid AS uuid),
                    r.source_index,
                    r.classe,
                    r.fiab_mnt,
                    r.fiab_eso,
                    r.fiab_tot,
                    r.classefiab,
                    r.gridcode,
                    r.geom_2154
                FROM ecocompensation.remontee_de_nappes r
                WHERE ST_Intersects(
                    r.geom_2154,
                    ST_GeomFromText(:wkt_aoi, 2154)
                );
                """
            ),
            {"pid": project_id, "aid": aoi_id, "wkt_aoi": aoi_wkt},
        )
        rc = res.rowcount
        n_inserted = int(rc) if rc is not None and rc >= 0 else int(count)

    log(f"[RESULTS] {n_inserted} entités insérées (remontée de nappes).")
    return n_inserted
