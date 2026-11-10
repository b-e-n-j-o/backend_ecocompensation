"""
enrich.py — végétation CESBIO + distances faune par îlot (même logique que enrich_uf.py, table ilots_rpg).
"""
from __future__ import annotations

import logging
import math
import time
from typing import Callable

from sqlalchemy import text

try:
    from api.recherche_de_foncier.filtrage.layers.faune import nearest_fauna_distance_sql
except ImportError:
    import importlib.util
    from pathlib import Path

    _faune_path = Path(__file__).resolve().parents[2] / "faune.py"
    _faune_spec = importlib.util.spec_from_file_location("faune_points", _faune_path)
    _faune_mod = importlib.util.module_from_spec(_faune_spec)
    assert _faune_spec and _faune_spec.loader
    _faune_spec.loader.exec_module(_faune_mod)
    nearest_fauna_distance_sql = _faune_mod.nearest_fauna_distance_sql

try:
    from api.recherche_de_foncier.filtrage.layers.cesbio_tables import VEG_TABLE_CALC
except ImportError:  # tests rpg qui court-circuitent le paquet api
    import importlib.util
    from pathlib import Path

    _path = Path(__file__).resolve().parents[2] / "cesbio_tables.py"
    _spec = importlib.util.spec_from_file_location("cesbio_tables_fallback", _path)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    VEG_TABLE_CALC = _mod.VEG_TABLE_CALC

from .params import RES_TABLE

log_ = logging.getLogger(__name__)

VEG_BATCH_SIZE = 100
FAUNA_BATCH_SIZE = 300
STMT_TIMEOUT = "90s"

SQL_VEG = f"""
WITH il_parc AS (
    SELECT il.num_ilot, u.idu
    FROM {RES_TABLE} il
    CROSS JOIN LATERAL unnest(il.idus) AS u(idu)
    WHERE il.project_id = CAST(:pid AS uuid)
      AND il.num_ilot = ANY(CAST(:batch AS text[]))
),
parc AS (
    SELECT DISTINCT ON (c.idu) c.idu, c.geom AS geom_2154
    FROM ecocompensation.cadastre_parcelles c
    WHERE c.idu IN (SELECT idu FROM il_parc)
    ORDER BY c.idu
),
parc_veg AS (
    SELECT parc.idu, v.libelle_prio,
           SUM(CASE
               WHEN ST_CoveredBy(v.geom, parc.geom_2154) THEN ST_Area(v.geom)
               ELSE ST_Area(ST_Intersection(parc.geom_2154, v.geom))
           END) AS area_m2,
           (array_agg(DISTINCT v.source) FILTER (WHERE v.source IS NOT NULL))[1] AS source
    FROM parc
    JOIN {VEG_TABLE_CALC} v
      ON parc.geom_2154 && v.geom AND ST_Intersects(parc.geom_2154, v.geom)
    WHERE v.libelle_prio IS NOT NULL
    GROUP BY parc.idu, v.libelle_prio
),
class_areas AS (
    SELECT ip.num_ilot, pv.libelle_prio, SUM(pv.area_m2) AS area_m2,
           (array_agg(DISTINCT pv.source) FILTER (WHERE pv.source IS NOT NULL))[1] AS source
    FROM il_parc ip
    JOIN parc_veg pv ON pv.idu = ip.idu
    GROUP BY ip.num_ilot, pv.libelle_prio
    HAVING SUM(pv.area_m2) > 0
),
geom_areas AS (
    SELECT num_ilot, ST_Area(geom_2154) AS area_m2
    FROM {RES_TABLE}
    WHERE project_id = CAST(:pid AS uuid) AND num_ilot = ANY(CAST(:batch AS text[]))
),
veg AS (
    SELECT ca.num_ilot,
           array_agg(DISTINCT ca.libelle_prio ORDER BY ca.libelle_prio) AS libs,
           jsonb_object_agg(
               ca.libelle_prio,
               jsonb_build_object(
                   'area_m2', ROUND(ca.area_m2::numeric, 1),
                   'pct', ROUND((ca.area_m2 / NULLIF(ga.area_m2, 0) * 100)::numeric, 2),
                   'source', ca.source
               )
           ) AS profile
    FROM class_areas ca
    JOIN geom_areas ga ON ga.num_ilot = ca.num_ilot
    GROUP BY ca.num_ilot
)
UPDATE {RES_TABLE} il
SET veg_libelles = COALESCE(veg.libs, '{{}}'),
    veg_profile  = COALESCE(veg.profile, '{{}}')
FROM veg
WHERE il.project_id = CAST(:pid AS uuid) AND il.num_ilot = veg.num_ilot
"""

SQL_FAUNA = f"""
WITH fa AS (
    SELECT il.num_ilot,
           COALESCE({nearest_fauna_distance_sql("il.geom_2154")}, -1) AS dist_m
    FROM {RES_TABLE} il
    WHERE il.project_id = CAST(:pid AS uuid) AND il.num_ilot = ANY(CAST(:batch AS text[]))
)
UPDATE {RES_TABLE} il SET fauna_distances = il.fauna_distances || jsonb_build_object(CAST(:species AS text), fa.dist_m)
FROM fa
WHERE il.project_id = CAST(:pid AS uuid) AND il.num_ilot = fa.num_ilot
"""


def _batches(ids: list[str], size: int):
    for start in range(0, len(ids), size):
        yield ids[start:start + size]


def run(engine, project_id: str, species: list[str],
        cb: Callable[[str], None] | None = None) -> int:
    log = cb or log_.info
    t0 = time.time()
    with engine.begin() as conn:
        ids = [r[0] for r in conn.execute(
            text(f"SELECT num_ilot FROM {RES_TABLE} WHERE project_id = CAST(:pid AS uuid) ORDER BY num_ilot"),
            {"pid": project_id})]
        conn.execute(text(
            f"ALTER TABLE {RES_TABLE} ADD COLUMN IF NOT EXISTS veg_profile jsonb NOT NULL DEFAULT '{{}}'"))
        conn.execute(text(f"UPDATE {RES_TABLE} SET veg_libelles = '{{}}', veg_profile = '{{}}', fauna_distances = '{{}}' "
                          "WHERE project_id = CAST(:pid AS uuid)"), {"pid": project_id})
    if not ids:
        log("[ILOTS_RPG] enrich : aucun îlot à enrichir")
        return 0

    n_veg = math.ceil(len(ids) / VEG_BATCH_SIZE)
    for bi, batch in enumerate(_batches(ids, VEG_BATCH_SIZE), 1):
        with engine.begin() as conn:
            conn.execute(text(f"SET LOCAL statement_timeout = '{STMT_TIMEOUT}'"))
            conn.execute(text(SQL_VEG), {"pid": project_id, "batch": batch})
        log(f"[ILOTS_RPG] végétation {bi}/{n_veg}")

    for sp in species:
        n_f = math.ceil(len(ids) / FAUNA_BATCH_SIZE)
        for bi, batch in enumerate(_batches(ids, FAUNA_BATCH_SIZE), 1):
            with engine.begin() as conn:
                conn.execute(text(f"SET LOCAL statement_timeout = '{STMT_TIMEOUT}'"))
                conn.execute(text(SQL_FAUNA), {"pid": project_id, "batch": batch, "species": sp, "names": [sp]})
            log(f"[ILOTS_RPG] faune '{sp}' {bi}/{n_f}")
    if not species:
        log("[ILOTS_RPG] aucune espèce : fauna_distances ignorée")

    log(f"[ILOTS_RPG] enrich : {len(ids)} îlots enrichis ({time.time() - t0:.1f}s)")
    return len(ids)
