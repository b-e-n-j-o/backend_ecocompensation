"""GeoJSON carte Données internes : parcelles du pool + attributs RankingTable / export."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from filtre_options import FiltreOptions
from exports.classement_export_attrs import build_parcelle_export_row, mmap_for_parcelle
from pool import pool_service

# Propriétés plates pour MapLibre (popup) — alignées sur l’export GPKG, sans les txt très longs.
MAP_PROP_KEYS: tuple[str, ...] = (
    "idu",
    "rang",
    "surf_ha",
    "dist_km",
    "dist_hyd",
    "zh_ha",
    "score_eco",
    "eco_max",
    "score_comp",
    "score_dur",
    "attr_fonc",
    "dur_niv",
    "cesbio",
    "espece_esp",
    "rayon_esp",
    "p_morale",
    "siren",
    "pm_denom",
    "pm_prosp",
    "txt_dure",
)


def _json_safe(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, float):
        if v != v:  # NaN
            return None
    return v


def _feature_properties(
    parcelle: dict[str, Any],
    mmap: dict[str, Any],
    options: FiltreOptions,
    statut: str,
) -> dict[str, Any]:
    row = build_parcelle_export_row(parcelle, mmap, options, clip_for_shapefile=False)
    props: dict[str, Any] = {"statut_pool": statut, "libelle": statut}
    for key in MAP_PROP_KEYS:
        val = _json_safe(row.get(key))
        if val is None or val == "":
            continue
        if key in {"dist_hyd", "rayon_esp"} and val == -1:
            continue
        if key == "txt_dure" and isinstance(val, str) and len(val) > 1200:
            val = val[:1170] + "\n…"
        props[key] = val
    return props


def build_pool_map_overlay(conn, project_id: str, run_id: str) -> dict[str, Any]:
    """
    Une transaction : géométries pool + métriques + indésirables.
    Retourne trois FeatureCollections WGS84 (retenues / ajoutées / indésirables).
    """
    pool_service.ensure_tables(conn)
    if not pool_service.run_belongs_to_project(conn, project_id, run_id):
        return {"ok": False, "reason": "not_found"}
    aoi_id = ""
    prow = conn.execute(
        text("SELECT aoi_id FROM ecocompensation.projects WHERE id = CAST(:pid AS uuid)"),
        {"pid": project_id},
    ).mappings().first()
    if prow and prow.get("aoi_id") is not None:
        aoi_id = str(prow["aoi_id"])

    meta = pool_service.get_run_meta(conn, project_id, run_id) or {}
    options = FiltreOptions.from_dict(meta.get("options_json") or {})
    parcelles = pool_service.get_parcelles_for_run_results(conn, project_id, run_id, aoi_id)
    by_idu_metrics = pool_service.get_all_metrics_grouped_by_idu(conn, project_id, run_id)
    geom_rows = pool_service.get_parcelles_geometries_for_run(conn, project_id, run_id)
    geom_by_idu = {str(r["idu"]): r.get("geometry") for r in geom_rows}
    indus = set(pool_service.list_project_indesirable_idus(conn, project_id))

    empty = {"type": "FeatureCollection", "features": []}
    buckets: dict[str, list] = {"retenues": [], "ajoutees": [], "indesirables": []}

    for p in parcelles:
        idu = str(p.get("idu") or "")
        if not idu:
            continue
        geom = geom_by_idu.get(idu)
        if not geom:
            continue
        mmap = mmap_for_parcelle(p, by_idu_metrics)
        origin = (mmap.get("pool_origin") or {}).get("source")
        if idu in indus:
            statut, bucket = "Indésirable", "indesirables"
        elif origin == "manual_idu":
            statut, bucket = "Ajoutée", "ajoutees"
        else:
            statut, bucket = "Retenue", "retenues"
        props = _feature_properties(p, mmap, options, statut)
        props["id"] = idu
        buckets[bucket].append(
            {
                "type": "Feature",
                "id": idu,
                "geometry": dict(geom) if isinstance(geom, dict) else geom,
                "properties": props,
            }
        )

    return {
        "ok": True,
        "run_id": run_id,
        "retenues": {**empty, "features": buckets["retenues"]},
        "ajoutees": {**empty, "features": buckets["ajoutees"]},
        "indesirables": {**empty, "features": buckets["indesirables"]},
    }
