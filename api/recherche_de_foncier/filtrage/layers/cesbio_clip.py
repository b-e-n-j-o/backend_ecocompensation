"""Découpe l'occupation du sol CESBIO sur une géométrie passée en sous-requête."""

from __future__ import annotations

import json

from sqlalchemy import text

from api.recherche_de_foncier.filtrage.layers.cesbio_tables import VEG_TABLE_CALC


def clip_cesbio_geojson(conn, geom_sql: str, params: dict) -> dict:
    """FeatureCollection 4326. `geom_sql` doit renvoyer une colonne `geom` (2154)."""
    rows = conn.execute(text(f"""
        WITH t AS ({geom_sql})
        SELECT v.libelle_prio, v.source,
               ST_Area(ST_Intersection(t.geom, v.geom)) AS m2,
               ST_AsGeoJSON(ST_Transform(ST_Intersection(t.geom, v.geom), 4326), 6) AS geom
        FROM t
        JOIN {VEG_TABLE_CALC} v
          ON t.geom && v.geom AND ST_Intersects(t.geom, v.geom)
        WHERE v.libelle_prio IS NOT NULL
    """), params).mappings().all()
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": json.loads(r["geom"]),
            "properties": {
                "libelle": r["libelle_prio"],
                "source": r["source"],
                "surface_ha": round(float(r["m2"] or 0) / 1e4, 4),
            },
        } for r in rows if r["geom"] and (r["m2"] or 0) > 0],
    }
