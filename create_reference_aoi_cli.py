#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Crée un projet/AOI de référence vierge pour tests de couches.

Centre (lon,lat): -0.905514, 44.617539
Diamètre AOI: 10 km (rayon 5 km)
"""

from __future__ import annotations

import uuid
from sqlalchemy import text
from db import get_engine


LON = -0.905514
LAT = 44.617539
RADIUS_M = 10_000  # 20 km de diamètre


def main() -> None:
    engine = get_engine()
    project_id = str(uuid.uuid4())
    name = "AOI_REFERENCE_10KM_DIAMETER"

    with engine.begin() as conn:
        aoi_id = str(
            conn.execute(
                text(
                    """
                    INSERT INTO ecocompensation.aoi (id, code_insee, buffer_m, geom_2154, project_id)
                    VALUES (
                        :pid,
                        :code,
                        :buffer_m,
                        ST_Transform(
                            ST_Buffer(
                                ST_Transform(ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), 2154),
                                :radius_m
                            ),
                            2154
                        ),
                        :pid
                    )
                    RETURNING id
                    """
                ),
                {
                    "pid": project_id,
                    "code": "REF_AOI",
                    "buffer_m": RADIUS_M,
                    "lon": LON,
                    "lat": LAT,
                    "radius_m": RADIUS_M,
                },
            ).scalar_one()
        )

        conn.execute(
            text(
                """
                INSERT INTO ecocompensation.projects (id, name, aoi_id, status)
                VALUES (:pid, :name, :aoi_id, 'created')
                """
            ),
            {"pid": project_id, "name": name, "aoi_id": aoi_id},
        )

    print(f"project_id={project_id}")
    print(f"aoi_id={aoi_id}")


if __name__ == "__main__":
    main()

