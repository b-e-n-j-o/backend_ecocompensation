"""
preanalyze/zdv.py
=================
Analyse ZDV (zone_de_vegetation) pour la parcelle cible.

Pour chaque nature présente dans la parcelle :
  - surface intersectante en ha
  - pourcentage de la superficie parcelle couverte

Source : table geo.zone_de_vegetation (colonnes : nature, geom_2154)
"""
from __future__ import annotations
from sqlalchemy import text
from sqlalchemy.engine import Engine


def analyze_zdv(engine: Engine, parcel_wkt: str, parcel_area_m2: float) -> dict:
    """
    Retourne :
    {
      "intersects": bool,
      "natures": [
        {"nature": "Bois", "surface_ha": 3.21, "pct_parcelle": 45.8},
        ...
      ],
      "total_surface_ha": float,  # somme des surfaces ZDV intersectantes
      "pct_total":        float,  # % de la parcelle couvert par ZDV
    }
    """
    sql = """
        SELECT
            z.nature,
            SUM(ST_Area(ST_Intersection(z.geom_2154, ST_GeomFromText(:wkt, 2154)))) AS area_m2
        FROM geo.zone_de_vegetation z
        WHERE ST_Intersects(z.geom_2154, ST_GeomFromText(:wkt, 2154))
          AND z.nature IS NOT NULL
        GROUP BY z.nature
        ORDER BY area_m2 DESC
    """
    try:
        with engine.begin() as conn:
            rows = conn.execute(text(sql), {"wkt": parcel_wkt}).mappings().all()
    except Exception as e:
        return {"intersects": False, "error": str(e)[:200]}

    if not rows:
        return {"intersects": False, "natures": [], "total_surface_ha": 0.0, "pct_total": 0.0}

    natures = []
    total_m2 = 0.0
    for r in rows:
        area = float(r["area_m2"] or 0)
        total_m2 += area
        pct = round(area / parcel_area_m2 * 100, 1) if parcel_area_m2 > 0 else 0.0
        natures.append({
            "nature":      r["nature"],
            "surface_ha":  round(area / 10_000, 4),
            "pct_parcelle": pct,
        })

    return {
        "intersects":       True,
        "natures":          natures,
        "total_surface_ha": round(total_m2 / 10_000, 4),
        "pct_total":        round(total_m2 / parcel_area_m2 * 100, 1) if parcel_area_m2 > 0 else 0.0,
    }