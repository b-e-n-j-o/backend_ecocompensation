"""
pool.py — réponse « une ligne par terrain » (last_results_uf v2), score éco, prospect PM, GeoJSON.

Format :
{
  "version": 2,
  "total_uf": N,
  "unites_foncieres": [{
      "rang", "uf_id", "source" ("pm"|"manuel"), "siren", "denomination", "forme_juridique",
      "nb_parcelles", "surface_ha", "miller", "distance_centre_km", "zone_humide_ha",
      "verdict", "motif_rejet", "criteres": {clé: {label, type, ok, idus|valeur…}},
      "fauna_distances": {espèce: m}, "veg_libelles", "veg_profile",
      "score_eco", "pm_prospect",
      "parcelles": [{"idu", "surface_ha", "in_pool", "fauna_distances", "criteres_portes"}]
  }],
  "diagnostic": {...entonnoir de build.run...}
}
"""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict

from sqlalchemy import bindparam, text

from api.recherche_de_foncier.enrichir.pool.profilers.personnes_morales import (
    DEFAULT_PROSPECTS_TABLE,
    _prospects_compensation_payload,
)
from api.recherche_de_foncier.enrichir.pool.profilers.score_eco import ScoreEcoProfiler

from . import tables
from .evaluation import criteres_portes
from .params import UfParams

logger = logging.getLogger(__name__)
P, T = tables.P, tables.T
ECO_MAX = 6


# --------------------------------------------------------------------------- score éco
def _distance_points(km: float) -> tuple[int, str]:
    if km < 3:
        return 3, "<3km"
    if km <= 7:
        return 2, "3-7km"
    if km <= 10:
        return 1, "7-10km"
    return 0, ">10km"


def _zone_humide_points(ha: float) -> tuple[int, str]:
    if ha >= 2.0:
        return 3, ">=2ha"
    if ha >= 1.0:
        return 2, "1-2ha"
    if ha > 0:
        return 1, "<1ha"
    return 0, "none"


def compute_score_eco(distance_km: float, fauna_distances: dict, fauna_criteria: list[dict], *,
                      zone_humide_ha: float = 0.0, study_type: str = "faune_buffer") -> dict:
    """Score éco 0..6 (inchangé : espèces + distance, ou ZH + espèces plafonnées à 3)."""
    if fauna_criteria:
        sp_pts, sp_reason, sp_extra = ScoreEcoProfiler._species_points_from_filter_enrich(
            fauna_distances or {}, fauna_criteria)
    else:
        sp_pts, sp_reason, sp_extra = 0, "no_faune_criteria", {}
    especes = {"points": None, "reason": sp_reason, **sp_extra}

    if study_type == "zones_humides_intra":
        zh_pts, zh_reason = _zone_humide_points(zone_humide_ha)
        sp_pts = min(3, sp_pts)
        especes["points"] = sp_pts
        return {"total_score": int(zh_pts + sp_pts), "max_score": ECO_MAX, "breakdown": {
            "zone_humide": {"points": zh_pts, "zone_humide_ha": round(zone_humide_ha, 4), "reason": zh_reason},
            "especes": especes}}

    d_pts, bucket = _distance_points(distance_km)
    especes["points"] = sp_pts
    return {"total_score": int(sp_pts + d_pts), "max_score": ECO_MAX, "breakdown": {
        "especes": especes,
        "distance": {"points": d_pts, "distance_km": round(distance_km, 3), "bucket": bucket}}}


# --------------------------------------------------------------------------- prospect PM
def lookup_prospects_by_siren(conn, sirens: list[str]) -> dict[str, dict]:
    """Prospect compensation agrégé par SIREN (une lecture par PM)."""
    clean = sorted({s.strip() for s in sirens if s and s.strip() and s != "manuel"})
    if not clean:
        return {}
    table = os.getenv("PARCELLES_PROSPECTS_TABLE", DEFAULT_PROSPECTS_TABLE)
    sql = text(f"""
        SELECT siren, bool_or(parcelle_deja_en_mc) AS parcelle_deja_en_mc,
               MAX(nb_mc_distinctes) AS nb_mc_distinctes,
               MAX(nb_parcelles_deja_en_mc) AS nb_parcelles_deja_en_mc,
               MAX(surface_deja_en_mc_m2) AS surface_deja_en_mc_m2
        FROM {table} WHERE siren IN :sirens GROUP BY siren""").bindparams(bindparam("sirens", expanding=True))
    out: dict[str, dict] = {}
    for i in range(0, len(clean), 500):
        for r in conn.execute(sql, {"sirens": clean[i:i + 500]}).mappings():
            payload = _prospects_compensation_payload(
                r["parcelle_deja_en_mc"], r["nb_mc_distinctes"],
                r["nb_parcelles_deja_en_mc"], r["surface_deja_en_mc_m2"])
            out[str(r["siren"])] = {**payload, "intersects_pm_database": True, "siren": str(r["siren"])}
    return out


def _empty_pm(siren: str | None, denomination: str | None) -> dict:
    return {"intersects_pm_database": bool(siren and siren != "manuel"), "siren": siren,
            "denomination": denomination, "forme_juridique": None, "compensation_deja_realisee": False,
            "parcelle_deja_en_mc": None, "nb_mc_distinctes": None,
            "nb_parcelles_deja_en_mc": None, "surface_deja_en_mc_m2": None}


# --------------------------------------------------------------------------- réponse
def _min_distances(parcelles: list[dict]) -> dict:
    out: dict[str, int] = {}
    for p in parcelles:
        for sp, d in (p.get("fauna_distances") or {}).items():
            if d is None or int(d) < 0:
                out.setdefault(sp, -1)
            elif out.get(sp, -1) < 0 or int(d) < out[sp]:
                out[sp] = int(d)
    return out


def build_response(engine, project_id: str, params: UfParams, *, diagnostic: dict | None = None) -> dict:
    with engine.connect() as conn:
        terrains = [dict(r) for r in conn.execute(text(f"""
            SELECT uf_id, source, siren, denomination, forme_juridique, nb_parcelles, idus,
                   surface_ha, miller, dist_centre_m, verdict, motif_rejet, criteres,
                   veg_libelles, veg_profile
            FROM {T} WHERE project_id = CAST(:pid AS uuid) AND (verdict OR source = 'manuel')"""),
            {"pid": project_id}).mappings()]
        ufs = [t["uf_id"] for t in terrains]
        parcelles = conn.execute(text(f"""
            SELECT uf_id, idu, surface_m2, zh_m2, fauna_distances FROM {P}
            WHERE project_id = CAST(:pid AS uuid) AND uf_id = ANY(CAST(:ufs AS text[]))
            ORDER BY uf_id, surface_m2 DESC"""), {"pid": project_id, "ufs": ufs}).mappings().all()
        idus = sorted({p["idu"] for p in parcelles})
        in_pool = set(conn.execute(text("""
            SELECT DISTINCT idu FROM ecocompensation_results.parcelles
            WHERE project_id = CAST(:pid AS uuid) AND idu = ANY(CAST(:idus AS text[]))"""),
            {"pid": project_id, "idus": idus}).scalars()) if idus else set()
        try:
            prospects = lookup_prospects_by_siren(conn, [t["siren"] for t in terrains if t["siren"]])
        except Exception:
            logger.exception("[uf] lecture prospects échouée project_id=%s", project_id)
            prospects = {}

    by_uf: dict[str, list[dict]] = defaultdict(list)
    for p in parcelles:
        by_uf[p["uf_id"]].append(dict(p))

    criteria = params.fauna_criteria_dicts
    unites = []
    for t in terrains:
        detail = t["criteres"] or {}
        ps = by_uf.get(t["uf_id"], [])
        fd = _min_distances(ps)
        zh_ha = sum(float(p["zh_m2"] or 0) for p in ps) / 1e4
        dist_km = float(t["dist_centre_m"] or 0) / 1000.0
        unites.append({
            "uf_id": t["uf_id"], "source": t["source"], "siren": t["siren"],
            "denomination": t["denomination"], "forme_juridique": t["forme_juridique"],
            "nb_parcelles": t["nb_parcelles"], "surface_ha": round(float(t["surface_ha"] or 0), 2),
            "miller": round(float(t["miller"]), 3) if t["miller"] is not None else None,
            "distance_centre_km": round(dist_km, 3), "zone_humide_ha": round(zh_ha, 4),
            "verdict": t["verdict"], "motif_rejet": t["motif_rejet"], "criteres": detail,
            "fauna_distances": fd, "veg_libelles": list(t["veg_libelles"] or []),
            "veg_profile": dict(t["veg_profile"] or {}),
            "score_eco": compute_score_eco(dist_km, fd, criteria, zone_humide_ha=zh_ha,
                                           study_type=params.study_type),
            "pm_prospect": prospects.get(t["siren"] or "", _empty_pm(t["siren"], t["denomination"])),
            "parcelles": [{
                "idu": p["idu"], "surface_ha": round(float(p["surface_m2"] or 0) / 1e4, 4),
                "in_pool": p["idu"] in in_pool, "fauna_distances": dict(p["fauna_distances"] or {}),
                "criteres_portes": criteres_portes(p["idu"], detail),
            } for p in ps],
        })

    unites.sort(key=lambda u: (u["source"] != "manuel", -u["score_eco"]["total_score"], -u["surface_ha"]))
    unites = unites[:params.limit_uf]
    for rang, u in enumerate(unites, 1):
        u["rang"] = rang
    return {"version": 2, "total_uf": len(terrains), "unites_foncieres": unites,
            "diagnostic": diagnostic or {}}


def save_response(engine, project_id: str, result: dict) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE ecocompensation.projects
            SET last_results_uf = CAST(:r AS jsonb), updated_at = now()
            WHERE id = CAST(:pid AS uuid)"""),
            {"r": json.dumps(result, ensure_ascii=False, default=str), "pid": project_id})


def stored_diagnostic(engine, project_id: str) -> dict:
    with engine.connect() as conn:
        raw = conn.execute(text("SELECT last_results_uf FROM ecocompensation.projects "
                                "WHERE id = CAST(:pid AS uuid)"), {"pid": project_id}).scalar()
    if isinstance(raw, str):
        raw = json.loads(raw)
    return (raw or {}).get("diagnostic") or {} if isinstance(raw, dict) else {}


def build_veg_geojson(engine, project_id: str, uf_id: str) -> dict:
    """Occupation du sol CESBIO découpée sur l'emprise d'UN terrain (clic sur l'UF dans la carte)."""
    from api.recherche_de_foncier.filtrage.layers.cesbio_clip import clip_cesbio_geojson

    geom_sql = f"""
        SELECT geom_2154 AS geom FROM {T}
        WHERE project_id = CAST(:pid AS uuid) AND uf_id = :uf AND geom_2154 IS NOT NULL
    """
    with engine.connect() as conn:
        fc = clip_cesbio_geojson(conn, geom_sql, {"pid": project_id, "uf": uf_id})
    for feature in fc["features"]:
        feature["properties"]["uf_id"] = uf_id
    return fc


def build_geojson(engine, project_id: str, results_uf: dict) -> dict:
    """Parcelles des terrains de last_results_uf, EPSG:4326, pour la carte."""
    unites = (results_uf or {}).get("unites_foncieres") or []
    if not unites:
        return {"type": "FeatureCollection", "features": []}
    meta = {u["uf_id"]: u for u in unites}
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT uf_id, idu, surface_m2, ST_AsGeoJSON(ST_Transform(geom_2154, 4326), 6) AS geom
            FROM {P} WHERE project_id = CAST(:pid AS uuid) AND uf_id = ANY(CAST(:ufs AS text[]))"""),
            {"pid": project_id, "ufs": list(meta)}).mappings().all()
    features = []
    for r in rows:
        u = meta[r["uf_id"]]
        sc = u.get("score_eco") or {}
        p = next((x for x in u.get("parcelles") or [] if x["idu"] == r["idu"]), {})
        features.append({
            "type": "Feature", "id": f"{r['uf_id']}:{r['idu']}",
            "geometry": json.loads(r["geom"]),
            "properties": {
                "uf_id": r["uf_id"], "uf_rang": u.get("rang"), "source": u.get("source"),
                "idu": r["idu"], "surface_ha": round(float(r["surface_m2"] or 0) / 1e4, 4),
                "verdict": u.get("verdict"), "in_pool": p.get("in_pool", False),
                "criteres_portes": p.get("criteres_portes", []),
                "score_ratio": round((sc.get("total_score") or 0) / (sc.get("max_score") or ECO_MAX), 4),
            },
        })
    with engine.connect() as conn:
        unions = conn.execute(text(f"""
            SELECT uf_id,
                   ST_AsGeoJSON(ST_Transform(ST_UnaryUnion(ST_Collect(geom_2154)), 4326), 6) AS geom
            FROM {P}
            WHERE project_id = CAST(:pid AS uuid) AND uf_id = ANY(CAST(:ufs AS text[]))
            GROUP BY uf_id"""),
            {"pid": project_id, "ufs": list(meta)}).mappings().all()
    emprises = []
    for r in unions:
        if not r["geom"]:
            continue
        u = meta[r["uf_id"]]
        sc = u.get("score_eco") or {}
        emprises.append({
            "type": "Feature", "id": r["uf_id"],
            "geometry": json.loads(r["geom"]),
            "properties": {
                "uf_id": r["uf_id"], "uf_rang": u.get("rang"),
                "denomination": u.get("denomination"),
                "surface_ha": u.get("surface_ha"),
                "score_ratio": round((sc.get("total_score") or 0) / (sc.get("max_score") or ECO_MAX), 4),
            },
        })
    return {
        "type": "FeatureCollection",
        "features": features,
        "emprises": {"type": "FeatureCollection", "features": emprises},
    }
