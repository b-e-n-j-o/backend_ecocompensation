"""
cadastre_autour.py — Parcelles cadastrales dans un carré autour d'un point, pour la carte KerFoncier
(fonction « Afficher les parcelles de cette zone »).

Source : ecocompensation.parcelles (cadastre Etalab, lecture seule, même source que le tiling AOI
et que add_parcelles.py → les idu affichés sont directement ajoutables au pool).

Route : GET /cadastre/autour?lon=-0.58&lat=44.84&r=500
CLI   : DATABASE_URL=postgresql://... python cadastre_autour.py --lon -0.58 --lat 44.84 [--r 500] [--out zone.geojson] [-v]
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from typing import Any, Mapping, Sequence

from fastapi import APIRouter, Query
from sqlalchemy import text

log = logging.getLogger("kerfoncier.cadastre_autour")

MAX_FEATURES = 5000
MAX_R_M = 1000
DEFAULT_R_M = 500

# - Le point est projeté en 2154 ; geom_2154 n'est jamais transformée dans le WHERE
#   → idx_parcelles_geom_2154 (GiST) est utilisé.
# - ST_Expand(point, r) = carré de 2r × 2r centré sur le clic.
# - 6 décimales ≈ 10 cm : suffisant pour l'affichage, divise le poids du JSON.
SQL = text("""
SELECT DISTINCT ON (p.idu)
       p.idu, p.code_insee, p.nom_com, p.section, p.numero, p.contenance,
       ST_AsGeoJSON(ST_Transform(p.geom_2154, 4326), 6) AS geojson
FROM ecocompensation.parcelles p
WHERE p.geom_2154 && ST_Expand(
        ST_Transform(ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), 2154), :r)
  AND p.idu IS NOT NULL
  AND p.geom_2154 IS NOT NULL
ORDER BY p.idu
LIMIT :limit
""")

PROPS = ("idu", "code_insee", "nom_com", "section", "numero", "contenance")


def to_feature_collection(rows: Sequence[Mapping[str, Any]], max_features: int = MAX_FEATURES) -> dict:
    """Lignes SQL → FeatureCollection GeoJSON (+ drapeau `truncated`)."""
    truncated = len(rows) > max_features
    features = []
    for r in rows[:max_features]:
        if not r.get("geojson"):
            continue
        features.append({
            "type": "Feature",
            "properties": {k: r.get(k) for k in PROPS},
            "geometry": json.loads(r["geojson"]),
        })
    return {"type": "FeatureCollection", "truncated": truncated, "features": features}


def fetch_cadastre_autour(conn, lon: float, lat: float, r: int = DEFAULT_R_M) -> dict:
    """`conn` : connexion SQLAlchemy (engine.connect() ou session.connection())."""
    r = max(50, min(int(r), MAX_R_M))
    t0 = time.perf_counter()
    rows = conn.execute(SQL, {"lon": lon, "lat": lat, "r": r, "limit": MAX_FEATURES + 1}).mappings().all()
    fc = to_feature_collection(rows)
    log.info("cadastre_autour lon=%.5f lat=%.5f r=%dm → %d parcelles%s en %.0f ms",
             lon, lat, r, len(fc["features"]), " (TRONQUÉ)" if fc["truncated"] else "",
             (time.perf_counter() - t0) * 1000)
    if not fc["features"]:
        log.info("  aucune parcelle : point hors couverture du cadastre chargé (Gironde uniquement) ?")
    return fc


# ── Route FastAPI ────────────────────────────────────────────────────────────
router = APIRouter(tags=["cadastre"])


@router.get("/cadastre/autour")
def cadastre_autour(
    lon: float = Query(..., ge=-5.5, le=10.0, description="Longitude WGS84"),
    lat: float = Query(..., ge=41.0, le=51.5, description="Latitude WGS84"),
    r: int = Query(DEFAULT_R_M, ge=50, le=MAX_R_M, description="Demi-côté du carré, en mètres"),
):
    from db import get_engine

    with get_engine().connect() as conn:
        return fetch_cadastre_autour(conn, lon, lat, r)


# ── CLI ──────────────────────────────────────────────────────────────────────
def main() -> None:
    import os

    from sqlalchemy import create_engine

    ap = argparse.ArgumentParser(description="Parcelles autour d'un point (test sans le front).")
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--r", type=int, default=DEFAULT_R_M, help="demi-côté en m (max 1000)")
    ap.add_argument("--out", help="écrit le GeoJSON (ouvrable dans QGIS)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s — %(message)s", datefmt="%H:%M:%S")

    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        if a.verbose:
            plan = conn.execute(text("EXPLAIN ANALYZE " + SQL.text),
                                {"lon": a.lon, "lat": a.lat, "r": a.r, "limit": MAX_FEATURES + 1}).scalars().all()
            log.debug("Plan :\n  %s", "\n  ".join(plan))
            if not any("idx_parcelles_geom_2154" in line for line in plan):
                log.warning("L'index idx_parcelles_geom_2154 n'apparaît pas dans le plan !")
        fc = fetch_cadastre_autour(conn, a.lon, a.lat, a.r)

    surf = sum((f["properties"]["contenance"] or 0) for f in fc["features"]) / 1e4
    log.info("Surface cumulée : %.1f ha", surf)
    for f in fc["features"][:5]:
        p = f["properties"]
        log.debug("  %s  %s %s-%s  %.2f ha", p["idu"], p["nom_com"], p["section"], p["numero"],
                  (p["contenance"] or 0) / 1e4)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(fc, fh)
        log.info("Écrit : %s", a.out)


if __name__ == "__main__":
    main()
