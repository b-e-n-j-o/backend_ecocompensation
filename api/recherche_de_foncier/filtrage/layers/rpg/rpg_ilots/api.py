"""
Lectures pour le front : JSON du tableau, puis GeoJSON de la carte.

Le GeoJSON contient trois sortes d'entités (propriété ``kind``) :
    ``uf``        union des parcelles cadastrales de l'îlot
    ``parcelle``  chaque parcelle cadastrale membre, avec section, numéro, surface,
                  part dans l'îlot, propriétaire PM + owner_index, présence dans le pool
    ``ilot_rpg``  contour RPG d'origine de l'îlot
Toutes portent ``num_ilot`` et ``rang``.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from .params import RES_TABLE, SRC_SCHEMA

GEOJSON_PRECISION = 6


def get_results(engine, project_id: str) -> dict | None:
    with engine.connect() as conn:
        has_col = conn.execute(text("""
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'ecocompensation' AND table_name = 'projects'
              AND column_name = 'last_results_ilots_rpg'""")).first()
        if not has_col:
            return None
        res = conn.execute(
            text("SELECT last_results_ilots_rpg FROM ecocompensation.projects WHERE id = CAST(:pid AS uuid)"),
            {"pid": project_id}).scalar()
    if isinstance(res, str):
        res = json.loads(res)
    return res if isinstance(res, dict) else None


def rank_owners(parc_rows) -> dict[str, dict[str, int]]:
    """Par îlot : rang des propriétaires PM par surface détenue décroissante (0 = principal)."""
    surf: dict[str, dict[str, float]] = {}
    for r in parc_rows:
        for o in r["owners"] or []:
            d = surf.setdefault(r["num_ilot"], {})
            d[o] = d.get(o, 0.0) + float(r["surface_ha"] or 0)
    return {
        ilot: {o: i for i, (o, _) in enumerate(sorted(d.items(), key=lambda kv: (-kv[1], kv[0])))}
        for ilot, d in surf.items()
    }


def _feature(kind: str, geom: str, props: dict, fid: str) -> dict:
    return {"type": "Feature", "id": fid, "geometry": json.loads(geom), "properties": {"kind": kind, **props}}


def get_geojson(engine, project_id: str) -> dict:
    empty = {"type": "FeatureCollection", "features": []}
    results = get_results(engine, project_id)
    ilots = (results or {}).get("ilots") or []
    if not ilots:
        return empty
    by_id = {il["num_ilot"]: il for il in ilots}
    ids = list(by_id)
    millesime = str((results or {}).get("millesime") or "2025")
    rpg_ilots_table = f"{SRC_SCHEMA}.rpg{millesime}_ilots"
    rpg_membre_table = f"{SRC_SCHEMA}.rpg{millesime}_ilot_membre"
    p = GEOJSON_PRECISION
    params = {"pid": project_id, "ids": ids}

    with engine.connect() as conn:
        uf_rows = conn.execute(text(f"""
            SELECT num_ilot, ST_AsGeoJSON(ST_Transform(geom_2154, 4326), {p}) AS geom
            FROM {RES_TABLE}
            WHERE project_id = CAST(:pid AS uuid) AND num_ilot = ANY(CAST(:ids AS text[]))
              AND geom_2154 IS NOT NULL"""), params).mappings().all()
        parc_rows = conn.execute(text(f"""
            SELECT il.num_ilot, c.idu, c.code_insee, c.section, c.numero,
                   ST_Area(c.geom) / 10000.0 AS surface_ha,
                   m.part_parcelle,
                   COALESCE(pm.owners, '{{}}') AS owners,
                   (pp.idu IS NOT NULL) AS in_pool,
                   ST_AsGeoJSON(ST_Transform(c.geom, 4326), {p}) AS geom
            FROM {RES_TABLE} il
            CROSS JOIN LATERAL unnest(il.idus) AS u(idu)
            JOIN {SRC_SCHEMA}.cadastre_parcelles c ON c.idu = u.idu
            LEFT JOIN {rpg_membre_table} m ON m.num_ilot = il.num_ilot AND m.idu = u.idu
            LEFT JOIN LATERAL (
                SELECT array_agg(DISTINCT x.owner_key ORDER BY x.owner_key) AS owners
                FROM {SRC_SCHEMA}.pm_ilot_membre x WHERE x.idu = u.idu
            ) pm ON TRUE
            LEFT JOIN ecocompensation_results.parcelles pp
                   ON pp.project_id = il.project_id AND pp.idu = u.idu
            WHERE il.project_id = CAST(:pid AS uuid) AND il.num_ilot = ANY(CAST(:ids AS text[]))"""),
            params).mappings().all()
        try:
            rpg_rows = conn.execute(text(f"""
                SELECT num_ilot, ST_AsGeoJSON(ST_Transform(geom, 4326), {p}) AS geom
                FROM {rpg_ilots_table} WHERE num_ilot = ANY(CAST(:ids AS text[]))"""),
                {"ids": ids}).mappings().all()
        except Exception:
            rpg_rows = []

    features = []
    for r in uf_rows:
        il = by_id[r["num_ilot"]]
        score = il.get("score_eco") or {}
        total, max_s = score.get("total_score"), score.get("max_score") or 6
        features.append(_feature("uf", r["geom"], {
            "num_ilot": il["num_ilot"],
            "rang": il.get("rang"),
            "surface_ha": il.get("surface_ha"),
            "nb_parcelles": il.get("nb_parcelles"),
            "cat_dominante": il.get("cat_dominante") or "NC",
            "code_cultu_dominant": il.get("code_cultu_dominant"),
            "pm_unique": bool(il.get("pm_unique")),
            "pm_dominant": il.get("pm_dominant"),
            "score_eco": total,
            "score_eco_max": max_s,
            "score_ratio": round(total / max_s, 4) if isinstance(total, (int, float)) and max_s else 0,
        }, f"uf:{il['num_ilot']}"))
    owner_rank = rank_owners(parc_rows)
    for r in parc_rows:
        il = by_id[r["num_ilot"]]
        ranks = owner_rank.get(r["num_ilot"], {})
        owners = sorted(r["owners"] or [], key=lambda o: ranks.get(o, 10**9))
        main_owner = owners[0] if owners else None
        features.append(_feature("parcelle", r["geom"], {
            "num_ilot": r["num_ilot"],
            "rang": il.get("rang"),
            "idu": r["idu"],
            "code_insee": r["code_insee"],
            "section": r["section"],
            "numero": r["numero"],
            "surface_ha": round(float(r["surface_ha"] or 0), 4),
            "part_parcelle": float(r["part_parcelle"]) if r["part_parcelle"] is not None else None,
            "proprietaire": main_owner,
            "nb_proprietaires_pm": len(owners),
            "owner_index": ranks.get(main_owner, -1) if main_owner else -1,
            "in_pool": bool(r["in_pool"]),
        }, f"parcelle:{r['idu']}"))
    for r in rpg_rows:
        il = by_id[r["num_ilot"]]
        features.append(_feature("ilot_rpg", r["geom"], {
            "num_ilot": r["num_ilot"], "rang": il.get("rang"),
        }, f"ilot_rpg:{r['num_ilot']}"))

    order = {"ilot_rpg": 0, "uf": 1, "parcelle": 2}
    features.sort(key=lambda f: (order[f["properties"]["kind"]], f["properties"].get("rang") or 10**9))
    return {"type": "FeatureCollection", "features": features}
