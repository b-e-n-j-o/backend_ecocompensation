"""Aperçu cours d'eau / plans d'eau dans la zone de recherche du filtre."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from shapely.geometry import shape
from sqlalchemy import text

from db import get_engine

router = APIRouter()
_engine = get_engine()


class HydroZoneBody(BaseModel):
    kind: str = Field(..., description="troncons ou surfaces")
    buffer_m: float = Field(3000, ge=0, le=50_000)
    geometry: dict


@router.post("/zone-features")
def zone_features(body: HydroZoneBody):
    kind = body.kind.strip().lower()
    table = {
        "troncons": "ecocompensation.troncons_hydros",
        "surfaces": "ecocompensation.surfaces_hydros",
    }.get(kind)
    if not table:
        raise HTTPException(400, "kind = troncons ou surfaces")
    try:
        geom = shape(body.geometry)
    except Exception as e:
        raise HTTPException(400, "Géométrie illisible") from e
    if geom.is_empty:
        return {"type": "FeatureCollection", "features": []}
    srid = 2154 if abs(geom.bounds[0]) > 1000 else 4326
    zone = """
        CASE WHEN :srid = 2154
             THEN ST_SetSRID(ST_GeomFromGeoJSON(:gj), 2154)
             ELSE ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(:gj), 4326), 2154)
        END
    """
    simplify = 8 if kind == "troncons" else 12
    sql = text(f"""
        SELECT COALESCE(nom, nature, '') AS nom,
               ST_AsGeoJSON(ST_Transform(ST_SimplifyPreserveTopology(h.geom_2154, :simp), 4326)) AS geojson
        FROM {table} h
        WHERE h.geom_2154 && ST_Expand({zone}, :buf)
          AND ST_DWithin(h.geom_2154, {zone}, :buf)
        LIMIT 1200
    """)
    with _engine.begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = '10s'"))
        rows = conn.execute(sql, {
            "gj": json.dumps(body.geometry),
            "srid": srid,
            "buf": float(body.buffer_m),
            "simp": simplify,
        }).mappings().all()
    features = []
    for r in rows:
        if not r["geojson"]:
            continue
        features.append({
            "type": "Feature",
            "properties": {"nom": r["nom"], "kind": kind},
            "geometry": json.loads(r["geojson"]),
        })
    return {"type": "FeatureCollection", "features": features}
