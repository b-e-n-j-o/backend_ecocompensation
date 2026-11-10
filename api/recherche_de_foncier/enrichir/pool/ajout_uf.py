"""Regroupe des parcelles cadastrales contiguës en une unité foncière manuelle."""
from __future__ import annotations

import json
import uuid

from sqlalchemy import text


class AjoutUfError(ValueError):
    pass


def _components(idus: list[str], pairs: list[tuple[str, str]]) -> list[list[str]]:
    parent = {i: i for i in idus}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    groups: dict[str, list[str]] = {}
    for i in idus:
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def add_manual_uf(engine, project_id: str, idus: list[str]) -> dict:
    clean = list(dict.fromkeys(str(i).strip() for i in idus if str(i).strip()))
    if len(clean) < 2:
        raise AjoutUfError("Sélectionnez au moins deux parcelles.")

    with engine.begin() as conn:
        rows = conn.execute(
            text("""
                SELECT idu, geom_2154, ST_Area(geom_2154) AS area_m2
                FROM ecocompensation.parcelles
                WHERE idu = ANY(CAST(:idus AS text[]))
                  AND geom_2154 IS NOT NULL
            """),
            {"idus": clean},
        ).mappings().all()
        found = {str(r["idu"]) for r in rows}
        missing = [i for i in clean if i not in found]
        if missing:
            raise AjoutUfError(f"Parcelles absentes du cadastre : {', '.join(missing[:8])}")

        pairs = conn.execute(
            text("""
                SELECT a.idu AS a, b.idu AS b
                FROM ecocompensation.parcelles a
                JOIN ecocompensation.parcelles b
                  ON a.idu < b.idu
                 AND a.idu = ANY(CAST(:idus AS text[]))
                 AND b.idu = ANY(CAST(:idus AS text[]))
                 AND ST_Touches(a.geom_2154, b.geom_2154)
            """),
            {"idus": clean},
        ).mappings().all()
        groups = _components(clean, [(str(p["a"]), str(p["b"])) for p in pairs])
        if len(groups) != 1:
            raise AjoutUfError(
                f"Ces parcelles forment {len(groups)} blocs séparés. "
                "Une unité foncière doit être d'un seul tenant."
            )

        metrics = conn.execute(
            text("""
                WITH u AS (
                    SELECT ST_UnaryUnion(ST_Collect(geom_2154)) AS g
                    FROM ecocompensation.parcelles
                    WHERE idu = ANY(CAST(:idus AS text[]))
                )
                SELECT
                    ST_Area(u.g) AS area_m2,
                    ST_Perimeter(u.g) AS perim_m,
                    ST_Distance(
                        ST_Centroid(u.g),
                        ST_Centroid(a.geom_2154)
                    ) AS dist_m
                FROM u
                JOIN ecocompensation.projects pr ON pr.id = CAST(:pid AS uuid)
                JOIN ecocompensation.aoi a ON a.id = pr.aoi_id
            """),
            {"idus": clean, "pid": project_id},
        ).mappings().one()

        area = float(metrics["area_m2"] or 0)
        perim = float(metrics["perim_m"] or 0)
        miller = (4 * 3.141592653589793 * area / perim ** 2) if perim > 0 else 0.0
        surface_ha = round(area / 10_000, 4)
        dist_m = float(metrics["dist_m"] or 0)
        uf_id = f"manuel_{uuid.uuid4().hex[:8]}"
        subset_id = f"{uf_id}_0000"

        conn.execute(
            text("""
                INSERT INTO ecocompensation_results.unites_foncieres (
                    project_id, uf_id, siren, denomination, forme_juridique,
                    nb_parcelles, surface_ha_uf, idu, surface_ha, geom_2154
                )
                SELECT
                    CAST(:pid AS uuid), :uf_id, 'manuel', 'Ajout manuel', NULL,
                    :nb, :surface_ha_uf, idu,
                    ROUND((ST_Area(geom_2154) / 10000.0)::numeric, 4),
                    geom_2154
                FROM ecocompensation.parcelles
                WHERE idu = ANY(CAST(:idus AS text[]))
            """),
            {
                "pid": project_id,
                "uf_id": uf_id,
                "nb": len(clean),
                "surface_ha_uf": surface_ha,
                "idus": clean,
            },
        )

        conn.execute(
            text("""
                INSERT INTO ecocompensation_results.sous_ensembles (
                    project_id, uf_id, subset_id, k, idus,
                    surface_ha, geom_2154, miller, dist_centre_m,
                    denomination, siren
                )
                SELECT
                    CAST(:pid AS uuid), :uf_id, :subset_id, :k, CAST(:idus AS text[]),
                    :surface_ha,
                    ST_UnaryUnion(ST_Collect(geom_2154)),
                    :miller, :dist_m,
                    'Ajout manuel', 'manuel'
                FROM ecocompensation.parcelles
                WHERE idu = ANY(CAST(:idus AS text[]))
            """),
            {
                "pid": project_id,
                "uf_id": uf_id,
                "subset_id": subset_id,
                "k": len(clean),
                "idus": clean,
                "surface_ha": surface_ha,
                "miller": round(miller, 6),
                "dist_m": round(dist_m, 1),
            },
        )

        stored = conn.execute(
            text("""
                SELECT last_results_uf, last_filter, filter_config
                FROM ecocompensation.projects
                WHERE id = CAST(:pid AS uuid)
            """),
            {"pid": project_id},
        ).mappings().one()
        fauna_raw = stored.get("last_filter") or stored.get("filter_config")
        last_results = stored.get("last_results_uf")
        stored = last_results
        if isinstance(stored, str):
            stored = json.loads(stored)
        if not isinstance(stored, dict):
            stored = {"total_uf": 0, "total_sous_ensembles": 0, "unites_foncieres": []}
        unites = list(stored.get("unites_foncieres") or [])
        unites.insert(0, {
            "rang": 0,
            "uf_id": uf_id,
            "siren": "manuel",
            "denomination": "Ajout manuel",
            "nb_parcelles": len(clean),
            "idus": clean,
            "distance_centre_km": round(dist_m / 1000, 3),
            "sous_ensembles": [{
                "subset_id": subset_id,
                "k": len(clean),
                "idus": clean,
                "surface_ha": surface_ha,
                "miller": round(miller, 3),
                "distance_centre_km": round(dist_m / 1000, 3),
                "denomination": "Ajout manuel",
                "siren": "manuel",
            }],
        })
        for i, u in enumerate(unites, 1):
            u["rang"] = i
        stored["unites_foncieres"] = unites
        stored["total_uf"] = len(unites)
        stored["total_sous_ensembles"] = sum(len(u.get("sous_ensembles") or []) for u in unites)
        conn.execute(
            text("""
                UPDATE ecocompensation.projects
                SET last_results_uf = CAST(:payload AS jsonb)
                WHERE id = CAST(:pid AS uuid)
            """),
            {"pid": project_id, "payload": json.dumps(stored)},
        )

    from api.recherche_de_foncier.filtrage.layers.enrich_uf import (
        enrich_uf_entities,
        ensure_columns,
        fauna_criteria_from_project_filter,
    )
    from api.recherche_de_foncier.filtrage.layers.faune import criterion_names

    ensure_columns(engine)
    criteria = fauna_criteria_from_project_filter(fauna_raw)
    species = [c["species"] for c in criteria]
    name_by_species = {
        c["species"]: criterion_names(c["species"], c.get("aliases"))
        for c in criteria
    }
    enrich_uf_entities(
        engine,
        project_id,
        uf_ids=[uf_id],
        subset_ids=[subset_id],
        species=species,
        name_by_species=name_by_species,
    )
    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE ecocompensation_results.sous_ensembles ss
                SET veg_libelles = COALESCE(uf.veg_libelles, '{}'),
                    veg_profile  = COALESCE(uf.veg_profile, '{}'::jsonb)
                FROM (
                    SELECT DISTINCT ON (uf_id) uf_id, veg_libelles, veg_profile
                    FROM ecocompensation_results.unites_foncieres
                    WHERE project_id = CAST(:pid AS uuid) AND uf_id = :uf_id
                    ORDER BY uf_id
                ) uf
                WHERE ss.project_id = CAST(:pid AS uuid)
                  AND ss.subset_id = :subset_id
            """),
            {"pid": project_id, "uf_id": uf_id, "subset_id": subset_id},
        )

    return {
        "uf_id": uf_id,
        "subset_id": subset_id,
        "nb_parcelles": len(clean),
        "surface_ha": surface_ha,
        "idus": clean,
    }


def delete_uf(engine, project_id: str, uf_id: str) -> dict:
    uf_id = str(uf_id or "").strip()
    if not uf_id:
        raise AjoutUfError("Identifiant d'unité foncière manquant.")
    with engine.begin() as conn:
        n_ss = conn.execute(
            text("""
                DELETE FROM ecocompensation_results.sous_ensembles
                WHERE project_id = CAST(:pid AS uuid) AND uf_id = :uf_id
            """),
            {"pid": project_id, "uf_id": uf_id},
        ).rowcount
        n_uf = conn.execute(
            text("""
                DELETE FROM ecocompensation_results.unites_foncieres
                WHERE project_id = CAST(:pid AS uuid) AND uf_id = :uf_id
            """),
            {"pid": project_id, "uf_id": uf_id},
        ).rowcount
        stored = conn.execute(
            text("SELECT last_results_uf FROM ecocompensation.projects WHERE id = CAST(:pid AS uuid)"),
            {"pid": project_id},
        ).scalar()
        if isinstance(stored, str):
            stored = json.loads(stored)
        if isinstance(stored, dict):
            unites = [u for u in (stored.get("unites_foncieres") or []) if str(u.get("uf_id")) != uf_id]
            for i, u in enumerate(unites, 1):
                u["rang"] = i
            stored["unites_foncieres"] = unites
            stored["total_uf"] = len(unites)
            stored["total_sous_ensembles"] = sum(len(u.get("sous_ensembles") or []) for u in unites)
            conn.execute(
                text("""
                    UPDATE ecocompensation.projects
                    SET last_results_uf = CAST(:payload AS jsonb)
                    WHERE id = CAST(:pid AS uuid)
                """),
                {"pid": project_id, "payload": json.dumps(stored)},
            )
    if not n_uf and not n_ss:
        raise AjoutUfError("Unité foncière introuvable.")
    return {"uf_id": uf_id, "deleted": True}
