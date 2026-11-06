"""
build.py — AOI -> îlots RPG -> unités foncières (une ligne par îlot dans ecocompensation_results.ilots_rpg).

UF d'un îlot = ses parcelles cadastrales membres (rpg{millesime}_ilot_membre) qui intersectent l'AOI.
Géométrie = union de ces parcelles (c'est elle qui sert aux filtres et à l'affichage).
On garde : nb parcelles >= min_parcelles et surface >= min_area_ha.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from sqlalchemy import text

from .params import RES_TABLE, IlotsRpgParams

log_ = logging.getLogger(__name__)

DDL = f"""
CREATE TABLE IF NOT EXISTS {RES_TABLE} (
    project_id          uuid NOT NULL,
    num_ilot            text NOT NULL,
    millesime           text,
    idus                text[] NOT NULL,
    nb_parcelles        integer,
    surface_ha          double precision,
    miller              double precision,
    dist_centre_m       double precision,
    cat_dominante       text,
    code_group_dominant integer,
    code_cultu_dominant text,
    part_pp             double precision,
    part_jachere        double precision,
    part_vigne          double precision,
    nb_pm               integer,
    pm_dominant         text,
    pm_unique           boolean,
    veg_libelles        text[] NOT NULL DEFAULT '{{}}',
    veg_profile         jsonb  NOT NULL DEFAULT '{{}}',
    fauna_distances     jsonb  NOT NULL DEFAULT '{{}}',
    geom_2154           geometry(MultiPolygon, 2154),
    created_at          timestamptz DEFAULT now(),
    PRIMARY KEY (project_id, num_ilot)
);
CREATE INDEX IF NOT EXISTS idx_ilots_rpg_geom  ON {RES_TABLE} USING gist (geom_2154);
CREATE INDEX IF NOT EXISTS idx_ilots_rpg_veg   ON {RES_TABLE} USING gin (veg_libelles);
CREATE INDEX IF NOT EXISTS idx_ilots_rpg_fauna ON {RES_TABLE} USING gin (fauna_distances)
"""


def sql_insert(p: IlotsRpgParams) -> str:
    return f"""
    WITH aoi AS (
        SELECT geom_2154 AS g, ST_Centroid(geom_2154) AS centre
        FROM ecocompensation.aoi WHERE id = CAST(:aid AS uuid)
    ),
    il AS (
        SELECT i.* FROM {p.ilots_table} i, aoi
        WHERE i.geom && aoi.g AND ST_Intersects(i.geom, aoi.g)
    ),
    mem AS (
        SELECT m.num_ilot, c.idu, c.geom
        FROM il
        JOIN {p.membre_table} m ON m.num_ilot = il.num_ilot
        JOIN ecocompensation.cadastre_parcelles c ON c.idu = m.idu AND c.code_dep = m.code_dep
        CROSS JOIN aoi
        WHERE c.geom && aoi.g AND ST_Intersects(c.geom, aoi.g)
    ),
    uf AS (
        SELECT num_ilot,
               array_agg(idu ORDER BY idu) AS idus,
               count(*)::int AS nb,
               ST_Multi(ST_CollectionExtract(ST_Union(geom), 3)) AS geom
        FROM mem GROUP BY num_ilot
    )
    INSERT INTO {RES_TABLE} (
        project_id, num_ilot, millesime, idus, nb_parcelles, surface_ha, miller, dist_centre_m,
        cat_dominante, code_group_dominant, code_cultu_dominant, part_pp, part_jachere, part_vigne,
        nb_pm, pm_dominant, pm_unique, geom_2154)
    SELECT CAST(:pid AS uuid), uf.num_ilot, :mil, uf.idus, uf.nb,
           ST_Area(uf.geom) / 10000.0,
           (4.0 * PI() * ST_Area(uf.geom)) / NULLIF(ST_Perimeter(uf.geom) ^ 2, 0),
           ST_Distance(ST_Centroid(uf.geom), aoi.centre),
           il.cat_dominante, il.code_group_dominant::int, il.code_cultu_dominant,
           il.part_pp, il.part_jachere, il.part_vigne,
           il.nb_pm, il.pm_dominant, il.pm_unique,
           uf.geom
    FROM uf JOIN il USING (num_ilot) CROSS JOIN aoi
    WHERE uf.nb >= :min_parc AND ST_Area(uf.geom) / 10000.0 >= :min_area
    """


def run(engine, project_id: str, aoi_id: str, params: IlotsRpgParams,
        cb: Callable[[str], None] | None = None) -> int:
    """(Re)construit les îlots candidats du projet. Retourne le nombre d'îlots retenus."""
    log = cb or log_.info
    t0 = time.time()
    with engine.begin() as conn:
        conn.exec_driver_sql(DDL)
        conn.execute(text(
            f"ALTER TABLE {RES_TABLE} ADD COLUMN IF NOT EXISTS veg_profile jsonb NOT NULL DEFAULT '{{}}'"))
        n_del = conn.execute(text(f"DELETE FROM {RES_TABLE} WHERE project_id = CAST(:pid AS uuid)"),
                             {"pid": project_id}).rowcount
        conn.execute(text("SET LOCAL statement_timeout = 0"))
        n = conn.execute(text(sql_insert(params)), {
            "pid": project_id, "aid": aoi_id, "mil": params.millesime,
            "min_parc": params.min_parcelles, "min_area": params.min_area_ha,
        }).rowcount
    log(f"[ILOTS_RPG] build : {n} îlots retenus (>= {params.min_parcelles} parcelles, "
        f">= {params.min_area_ha} ha), {n_del} anciens supprimés, {time.time() - t0:.1f}s")
    return n
