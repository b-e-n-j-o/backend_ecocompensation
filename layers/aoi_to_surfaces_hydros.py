#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clip national ecocompensation.surfaces_hydros → ecocompensation_results.surfaces_hydros
pour un projet / AOI (méthode zones humides).
"""

from __future__ import annotations

import time

from sqlalchemy import text


_DDL = """
CREATE SCHEMA IF NOT EXISTS ecocompensation_results;

CREATE TABLE IF NOT EXISTS ecocompensation_results.surfaces_hydros (
    project_id uuid NOT NULL,
    cleabs text NOT NULL,
    code_hydrographique text NULL,
    nature text NULL,
    position_par_rapport_au_sol text NULL,
    persistance text NULL,
    salinite boolean NULL,
    origine text NULL,
    statut text NULL,
    commentaire_sur_l_objet_hydro text NULL,
    nom text NULL,
    geom_2154 geometry(Geometry, 2154) NOT NULL,
    created_at timestamptz NULL DEFAULT now(),
    PRIMARY KEY (project_id, cleabs)
);

CREATE INDEX IF NOT EXISTS idx_ecocomp_results_surfaces_hydros_geom
    ON ecocompensation_results.surfaces_hydros USING GIST (geom_2154);

CREATE INDEX IF NOT EXISTS idx_ecocomp_results_surfaces_hydros_project
    ON ecocompensation_results.surfaces_hydros (project_id);
"""


def _ensure_results_table(conn) -> None:
    conn.execute(text(_DDL).execution_options(no_prepare=True))


def _purge_project(conn, project_id: str) -> None:
    conn.execute(
        text(
            """
            DELETE FROM ecocompensation_results.surfaces_hydros
            WHERE project_id = CAST(:pid AS uuid)
            """
        ),
        {"pid": project_id},
    )


def run(engine, project_id: str, aoi_id: str, cb=None) -> int:
    """
    Insère les surfaces hydrographiques intersectant l'AOI du projet.

    :return: Nombre de surfaces insérées.
    """
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
        log(f"[SURFACES_HYDROS] AOI id={aoi_id} introuvable, exécution annulée.")
        return 0

    aoi_wkt = row_aoi["wkt_aoi"]
    log(f"[SURFACES_HYDROS] AOI id={aoi_id}, surface ~ {row_aoi['area_m2'] / 10_000:.2f} ha")

    with engine.begin() as conn:
        count = conn.execute(
            text(
                """
                SELECT count(*)
                FROM ecocompensation.surfaces_hydros sh
                WHERE sh.geom_2154 IS NOT NULL
                  AND ST_Intersects(sh.geom_2154, ST_GeomFromText(:wkt_aoi, 2154));
                """
            ),
            {"wkt_aoi": aoi_wkt},
        ).scalar_one()

    log(f"[SURFACES_HYDROS] {count} surface(s) intersectent l'AOI.")

    if count == 0:
        with engine.begin() as conn:
            _ensure_results_table(conn)
            _purge_project(conn, project_id)
        return 0

    t0 = time.perf_counter()
    with engine.begin() as conn:
        _ensure_results_table(conn)
        _purge_project(conn, project_id)
        conn.execute(
            text(
                """
                INSERT INTO ecocompensation_results.surfaces_hydros (
                    project_id,
                    cleabs,
                    code_hydrographique,
                    nature,
                    position_par_rapport_au_sol,
                    persistance,
                    salinite,
                    origine,
                    statut,
                    commentaire_sur_l_objet_hydro,
                    nom,
                    geom_2154
                )
                SELECT
                    CAST(:pid AS uuid),
                    sh.cleabs,
                    sh.code_hydrographique,
                    sh.nature,
                    sh.position_par_rapport_au_sol,
                    sh.persistance,
                    sh.salinite,
                    sh.origine,
                    sh.statut,
                    sh.commentaire_sur_l_objet_hydro,
                    sh.nom,
                    sh.geom_2154
                FROM ecocompensation.surfaces_hydros sh
                WHERE sh.geom_2154 IS NOT NULL
                  AND ST_Intersects(
                      sh.geom_2154,
                      ST_GeomFromText(:wkt_aoi, 2154)
                  );
                """
            ),
            {"pid": project_id, "wkt_aoi": aoi_wkt},
        )
        inserted = conn.execute(
            text(
                """
                SELECT count(*) FROM ecocompensation_results.surfaces_hydros
                WHERE project_id = CAST(:pid AS uuid)
                """
            ),
            {"pid": project_id},
        ).scalar_one()

    log(
        f"[SURFACES_HYDROS] {inserted} surface(s) insérées pour project_id={project_id} "
        f"(en {time.perf_counter() - t0:.2f} s)."
    )
    return int(inserted or 0)
