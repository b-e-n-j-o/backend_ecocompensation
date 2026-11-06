#!/usr/bin/env python3
"""Étape flore seule sur les parcelles déjà tuilées d'un projet.

python flora_filter.py --project-id UUID --taxon "nom:robinia pseudoacacia" --dist 200 -v
"""
from __future__ import annotations

import argparse
import logging
import time

from sqlalchemy import text

from api.recherche_de_foncier.filtrage.layers.filter_pipeline import (
    FilterConfig,
    FloraCriterion,
    flora_step_label,
)
from api.recherche_de_foncier.routers.flore_router import ensure_flore_schema

log = logging.getLogger("kerfoncier.flora_filter")

_COUNT = text("""
    SELECT COUNT(*) FROM ecocompensation_results.parcelles p
    WHERE p.project_id = CAST(:pid AS uuid)
""")


def run_flora_step(engine, project_id: str, criteria: list[FloraCriterion], combine: str = "or") -> dict:
    ensure_flore_schema(engine)
    config = FilterConfig(flora_criteria=criteria, flora_combine=combine, min_area_ha=0.1)
    label = flora_step_label(config)
    keys = [c.taxon_key for c in criteria]
    dists = [float(c.dist_m) for c in criteria]
    if str(combine).lower() == "and":
        where = """
            NOT EXISTS (
                SELECT 1
                FROM unnest(CAST(:flora_keys AS text[]), CAST(:flora_dists AS float8[])) AS c(k, d)
                WHERE NOT EXISTS (
                    SELECT 1 FROM ecocompensation.flore_observation o
                    WHERE o.taxon_key = c.k
                      AND ST_DWithin(p.geom_2154, o.geom, c.d)
                )
            )
        """
    else:
        where = """
            EXISTS (
                SELECT 1
                FROM unnest(CAST(:flora_keys AS text[]), CAST(:flora_dists AS float8[])) AS c(k, d)
                JOIN ecocompensation.flore_observation o ON o.taxon_key = c.k
                WHERE ST_DWithin(p.geom_2154, o.geom, c.d)
            )
        """
    params = {"pid": project_id, "flora_keys": keys, "flora_dists": dists}
    t0 = time.perf_counter()
    with engine.begin() as conn:
        before = int(conn.execute(_COUNT, {"pid": project_id}).scalar_one())
        after = int(conn.execute(
            text(f"SELECT COUNT(*) FROM ecocompensation_results.parcelles p WHERE p.project_id = CAST(:pid AS uuid) AND ({where})"),
            params,
        ).scalar_one())
    elapsed = time.perf_counter() - t0
    log.info("%s — avant=%d après=%d en %.2fs", label, before, after, elapsed)
    return {"label": label, "before": before, "after": after, "duration_s": round(elapsed, 3)}


def main() -> None:
    import os
    from sqlalchemy import create_engine

    ap = argparse.ArgumentParser(description="Filtre flore seul (compte, ne supprime pas).")
    ap.add_argument("--project-id", required=True)
    ap.add_argument("--taxon", action="append", required=True, help="taxon_key, répétable")
    ap.add_argument("--dist", type=float, default=200)
    ap.add_argument("--combine", choices=("or", "and"), default="or")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(levelname)s %(message)s")
    engine = create_engine(os.environ["DATABASE_URL"])
    criteria = [FloraCriterion(taxon_key=t, label=t, dist_m=a.dist) for t in a.taxon]
    print(run_flora_step(engine, a.project_id, criteria, a.combine))


if __name__ == "__main__":
    main()
