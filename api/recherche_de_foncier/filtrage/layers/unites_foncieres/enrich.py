"""
enrich.py — enrichissement des terrains retenus (et UF manuelles), pour l'affichage et le score.

  - distance à chaque espèce du filtre, par parcelle (distance du terrain = min des parcelles)
  - m² de zone humide par parcelle, si la ZH compte et n'a pas déjà été calculée
  - profil d'occupation du sol CESBIO par terrain (surfaces et % par classe)
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from sqlalchemy import text

from api.recherche_de_foncier.filtrage.layers.cesbio_tables import VEG_TABLE_CALC
from api.recherche_de_foncier.filtrage.layers.faune import criterion_names, nearest_fauna_distance_sql

from . import tables
from .criteres import ZH_AREA_SQL
from .params import UfParams

logger = logging.getLogger(__name__)
P, T = tables.P, tables.T
VEG_BATCH_UF = 20

_SQL_VEG = f"""
WITH parc AS (
    SELECT p.uf_id, p.geom_2154 FROM {P} p
    WHERE p.project_id = CAST(:pid AS uuid) AND p.uf_id = ANY(CAST(:ufs AS text[]))
),
par_classe AS (
    SELECT parc.uf_id, v.libelle_prio,
           SUM(ST_Area(ST_Intersection(parc.geom_2154, ST_ClipByBox2D(v.geom, parc.geom_2154)))) AS m2,
           (array_agg(DISTINCT v.source) FILTER (WHERE v.source IS NOT NULL))[1] AS source
    FROM parc
    JOIN {VEG_TABLE_CALC} v ON parc.geom_2154 && v.geom AND ST_Intersects(parc.geom_2154, v.geom)
    WHERE v.libelle_prio IS NOT NULL
    GROUP BY parc.uf_id, v.libelle_prio
    HAVING SUM(ST_Area(ST_Intersection(parc.geom_2154, ST_ClipByBox2D(v.geom, parc.geom_2154)))) > 0
),
totaux AS (SELECT uf_id, SUM(ST_Area(geom_2154)) AS m2 FROM parc GROUP BY uf_id),
agg AS (
    SELECT c.uf_id,
           array_agg(c.libelle_prio ORDER BY c.libelle_prio) AS libelles,
           jsonb_object_agg(c.libelle_prio, jsonb_build_object(
               'area_m2', ROUND(c.m2::numeric, 1),
               'pct', ROUND((c.m2 / NULLIF(t.m2, 0) * 100)::numeric, 2),
               'source', c.source)) AS profile
    FROM par_classe c JOIN totaux t ON t.uf_id = c.uf_id
    GROUP BY c.uf_id
)
UPDATE {T} t SET veg_libelles = a.libelles, veg_profile = a.profile
FROM agg a WHERE t.project_id = CAST(:pid AS uuid) AND t.uf_id = a.uf_id
"""


def terrains_retenus(conn, project_id: str) -> list[str]:
    return list(conn.execute(text(f"""
        SELECT uf_id FROM {T}
        WHERE project_id = CAST(:pid AS uuid) AND (verdict OR source = 'manuel')
        ORDER BY uf_id"""), {"pid": project_id}).scalars())


def run(engine, project_id: str, params: UfParams, cb: Callable[[str], None] | None = None,
        *, uf_ids: list[str] | None = None) -> int:
    log = cb or logger.info
    with engine.connect() as conn:
        ufs = uf_ids or terrains_retenus(conn, project_id)
    if not ufs:
        log("ENRICH_UF : aucun terrain retenu")
        return 0
    log(f"ENRICH_UF : {len(ufs)} terrains")
    base = {"pid": project_id, "ufs": ufs}

    for fc in params.config.fauna_criteria:
        t = time.perf_counter()
        with engine.begin() as conn:
            tables.set_statement_timeout(conn)
            conn.execute(text(f"""
                UPDATE {P} p SET fauna_distances = p.fauna_distances
                    || jsonb_build_object(CAST(:sp AS text), COALESCE(({nearest_fauna_distance_sql("p.geom_2154")}), -1))
                WHERE p.project_id = CAST(:pid AS uuid) AND p.uf_id = ANY(CAST(:ufs AS text[]))"""),
                {**base, "sp": fc.species, "names": criterion_names(fc.species, fc.aliases)})
        log(f"   🦎 distances {fc.species} ({time.perf_counter() - t:.1f}s)")

    zh_deja = params.config.zone_humide_mode == "intersect" and params.config.min_zone_humide_ha > 0
    if params.need_zone_humide and not zh_deja:
        t = time.perf_counter()
        with engine.begin() as conn:
            tables.set_statement_timeout(conn)
            conn.execute(text(f"""
                UPDATE {P} p SET zh_m2 = ({ZH_AREA_SQL})
                WHERE p.project_id = CAST(:project_id AS uuid) AND p.uf_id = ANY(CAST(:ufs AS text[]))"""),
                {"project_id": project_id, "ufs": ufs})
        log(f"   💧 zones humides ({time.perf_counter() - t:.1f}s)")

    for i in range(0, len(ufs), VEG_BATCH_UF):
        t = time.perf_counter()
        with engine.begin() as conn:
            tables.set_statement_timeout(conn)
            conn.execute(text(_SQL_VEG), {"pid": project_id, "ufs": ufs[i:i + VEG_BATCH_UF]})
        log(f"   🌿 occupation du sol {min(i + VEG_BATCH_UF, len(ufs))}/{len(ufs)} "
            f"({time.perf_counter() - t:.1f}s)")
    return len(ufs)
