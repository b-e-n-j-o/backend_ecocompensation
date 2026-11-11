"""
uf_export_metrics.py
====================

Métriques PAR PARCELLE pour les parcelles des unités foncières, calculées avec
EXACTEMENT les mêmes profilers que le pool parcelles (même valeurs que la couche
`parcelles` de l'export).

Mécanisme : un run pool dédié, scope = 'uf_export', qui contient les parcelles des UF.
`profiling_service.compute_metrics_for_run` le traite comme un run normal. Le run est
réutilisé tant que la liste des parcelles ne change pas (clé = hash des IDU + filtre UF).

En complément, les champs « filtrage » (zone humide, hydro, faune…) sont lus sur
ecocompensation_results.parcelles quand ils existent, et les distances faune
manquantes sont calculées (même SQL que l'enrichissement des parcelles, mais par parcelle).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from typing import Any, Callable

from sqlalchemy import text

from api.recherche_de_foncier.enrichir.pool import pool_service
from api.recherche_de_foncier.filtrage.layers.unites_foncieres.tables import P as UF_TABLE

logger = logging.getLogger(__name__)

SCOPE = "uf_export"
RES_PARCELLES = "ecocompensation_results.parcelles"

# Champs de ecocompensation_results.parcelles relus s'ils existent (fallback filter_enrich
# de mmap_for_parcelle). Lus dynamiquement : aucune hypothèse sur le schéma exact.
_PARCEL_ENRICH_COLS = (
    "veg_libelles", "fauna_distances", "zone_humide_ha", "dist_hydro_m",
    "troncons_hydro_info", "dist_surface_hydro_m", "surface_hydro_ha", "surfaces_hydro_info",
)

Log = Callable[[str], None]


def _idus_key(idus: list[str], options_raw: dict | None) -> str:
    h = hashlib.sha1()
    h.update("|".join(sorted(idus)).encode())
    h.update(json.dumps(options_raw or {}, sort_keys=True, default=str).encode())
    return h.hexdigest()[:20]


def load_uf_parcels(
    engine,
    project_id: str,
    retained: dict[str, set[str]],
    centre_xy: tuple[float, float] | None,
) -> list[dict[str, Any]]:
    """
    Parcelles retenues des UF avec géométrie (2154), surface, Miller, distance au centre.
    retained : {uf_id: {idu, …}}
    """
    uf_ids = list(retained)
    if not uf_ids:
        return []
    cx, cy = centre_xy if centre_xy else (None, None)
    sql = text(f"""
        SELECT DISTINCT ON (uf_id, idu)
               uf_id, idu, geom_2154,
               ST_Area(geom_2154)       AS area_m2,
               ST_Perimeter(geom_2154)  AS perim_m,
               CASE WHEN CAST(:cx AS float8) IS NULL THEN NULL
                    ELSE ST_Distance(geom_2154, ST_SetSRID(ST_MakePoint(:cx, :cy), 2154)) END AS dist_m
        FROM {UF_TABLE}
        WHERE project_id = CAST(:pid AS uuid)
          AND uf_id = ANY(CAST(:ufs AS text[]))
          AND geom_2154 IS NOT NULL
        ORDER BY uf_id, idu
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {"pid": project_id, "ufs": uf_ids, "cx": cx, "cy": cy}).mappings().all()
    out = []
    for r in rows:
        uf_id, idu = str(r["uf_id"]), str(r["idu"])
        if idu not in retained.get(uf_id, set()):
            continue
        area, perim = float(r["area_m2"] or 0), float(r["perim_m"] or 0)
        out.append({
            "uf_id": uf_id,
            "idu": idu,
            "geom_2154": r["geom_2154"],
            "code_insee": idu[:5],
            "section": idu[8:10],
            "numero": idu[-4:],
            "surface_ha": round(area / 10_000.0, 4),
            "miller": round(4 * math.pi * area / perim ** 2, 4) if perim > 0 else 0.0,
            "distance_km": round(float(r["dist_m"]) / 1000.0, 2) if r["dist_m"] is not None else None,
        })
    return out


def _existing_cols(conn, table: str, wanted: tuple[str, ...]) -> list[str]:
    schema, name = table.split(".")
    rows = conn.execute(
        text("SELECT column_name FROM information_schema.columns WHERE table_schema = :s AND table_name = :t"),
        {"s": schema, "t": name},
    ).scalars().all()
    return [c for c in wanted if c in set(rows)]


def attach_filter_fields(
    engine, project_id: str, parcels: list[dict], species: list[str], log: Log,
    aliases_by_species: dict[str, list[str]] | None = None,
) -> None:
    """Complète chaque parcelle avec les champs du filtrage (ZH, hydro, faune) — en place."""
    idus = sorted({p["idu"] for p in parcels})
    if not idus:
        return
    with engine.connect() as conn:
        cols = _existing_cols(conn, RES_PARCELLES, _PARCEL_ENRICH_COLS)
        by_idu: dict[str, dict] = {}
        if cols:
            rows = conn.execute(
                text(f"""
                    SELECT DISTINCT ON (idu) idu, {", ".join(cols)}
                    FROM {RES_PARCELLES}
                    WHERE project_id = CAST(:pid AS uuid) AND idu = ANY(CAST(:idus AS text[]))
                    ORDER BY idu
                """),
                {"pid": project_id, "idus": idus},
            ).mappings().all()
            by_idu = {str(r["idu"]): dict(r) for r in rows}
        log(f"[UF_EXPORT] champs filtrage relus : {len(by_idu)}/{len(idus)} parcelles ({', '.join(cols) or 'aucune colonne'})")

        # Faune : distance par espèce, calculée là où elle manque (une requête par espèce)
        fauna: dict[str, dict[str, int]] = {}
        for sp in species:
            missing = [i for i in idus if sp not in ((by_idu.get(i) or {}).get("fauna_distances") or {})]
            if not missing:
                continue
            names = [sp, *((aliases_by_species or {}).get(sp) or [])]
            t0 = time.perf_counter()
            rows = conn.execute(
                text(f"""
                    SELECT p.idu,
                           COALESCE((
                               SELECT ROUND(ST_Distance(p.geom_2154, f.geometry))::int
                               FROM (
                                   SELECT geometry FROM ecocompensation.fauna
                                   WHERE nom_vernaculaire = ANY(CAST(:names AS text[])) AND geometry IS NOT NULL
                                   UNION ALL
                                   SELECT geom FROM ecocompensation.observation_faune
                                   WHERE nom_espece = ANY(CAST(:names AS text[])) AND geom IS NOT NULL
                               ) f
                               ORDER BY p.geom_2154 <-> f.geometry
                               LIMIT 1
                           ), -1) AS dist_m
                    FROM (SELECT DISTINCT ON (idu) idu, geom_2154 FROM {UF_TABLE}
                          WHERE project_id = CAST(:pid AS uuid) AND idu = ANY(CAST(:idus AS text[]))
                          ORDER BY idu) p
                """),
                {"pid": project_id, "idus": missing, "names": names},
            ).mappings().all()
            for r in rows:
                fauna.setdefault(str(r["idu"]), {})[sp] = int(r["dist_m"])
            log(f"[UF_EXPORT] faune '{sp}' : {len(rows)} parcelles en {time.perf_counter() - t0:.1f}s")

    for p in parcels:
        src = by_idu.get(p["idu"]) or {}
        for c in cols:
            if src.get(c) is not None and c != "fauna_distances":
                p[c] = src[c]
        fd = dict(src.get("fauna_distances") or {})
        fd.update(fauna.get(p["idu"], {}))
        fd = {k: v for k, v in fd.items() if v is not None and v >= 0}
        if fd:
            p["fauna_distances"] = fd


def _drop_runs(conn, project_id: str, run_ids: list[str]) -> None:
    if not run_ids:
        return
    for table, col in (
        ("parcelles_pool_metrics", "run_id"), ("parcelles_pool", "run_id"),
        ("parcelles_pool_indesirables", "run_id"), ("parcelles_pool_runs", "id"),
    ):
        conn.execute(
            text(f"DELETE FROM ecocompensation_results.{table} "
                 f"WHERE project_id = CAST(:pid AS uuid) AND {col} = ANY(CAST(:ids AS uuid[]))"),
            {"pid": project_id, "ids": run_ids},
        )


def ensure_uf_parcel_metrics(
    engine,
    project_id: str,
    parcels: list[dict],
    options_raw: dict | None,
    *,
    force: bool = False,
    log: Log | None = None,
) -> tuple[str, dict[str, list[dict[str, Any]]]]:
    """
    Garantit les métriques pool (profilers) pour les parcelles des UF.
    Retourne (run_id, metrics_by_idu) au format de get_all_metrics_grouped_by_idu.
    """
    from api.recherche_de_foncier.enrichir.pool.profiling_service import compute_metrics_for_run

    log = log or (lambda m: logger.info("[uf_export] project_id=%s %s", project_id, m))
    by_idu: dict[str, dict] = {}
    for p in parcels:
        by_idu.setdefault(p["idu"], p)
    idus = sorted(by_idu)
    key = _idus_key(idus, options_raw)

    with engine.begin() as conn:
        pool_service.ensure_tables(conn)
        rows = conn.execute(
            text("""
                SELECT id::text AS id, options_json->>'uf_export_key' AS k
                FROM ecocompensation_results.parcelles_pool_runs
                WHERE project_id = CAST(:pid AS uuid) AND scope = :scope
                ORDER BY created_at DESC
            """),
            {"pid": project_id, "scope": SCOPE},
        ).mappings().all()
        reuse = next((r["id"] for r in rows if r["k"] == key), None) if not force else None
        if reuse:
            run_id = reuse
            _drop_runs(conn, project_id, [r["id"] for r in rows if r["id"] != run_id])
            log(f"[UF_EXPORT] métriques parcelles déjà calculées (run {run_id[:8]}) — réutilisées")
        else:
            _drop_runs(conn, project_id, [r["id"] for r in rows])
            run_id = pool_service.create_run(
                conn, project_id, SCOPE, {**(options_raw or {}), "uf_export_key": key}, len(idus)
            )
            pool_service.insert_pool_parcelles(conn, project_id, run_id, [
                {"idu": i, "rank": None, "surface_ha": by_idu[i]["surface_ha"], "miller": by_idu[i]["miller"],
                 "distance_km": by_idu[i]["distance_km"], "dist_hydro_m": by_idu[i].get("dist_hydro_m")}
                for i in idus
            ])
            missing = conn.execute(
                text(f"""
                    SELECT count(*) FROM unnest(CAST(:idus AS text[])) u(idu)
                    WHERE NOT EXISTS (SELECT 1 FROM {RES_PARCELLES} p
                                      WHERE p.project_id = CAST(:pid AS uuid) AND p.idu = u.idu)
                """),
                {"pid": project_id, "idus": idus},
            ).scalar_one()
            if missing:
                log(f"[UF_EXPORT] ⚠ {missing}/{len(idus)} parcelles d'UF absentes de {RES_PARCELLES} : "
                    "les profilers ne les verront pas (attributs vides pour celles-ci)")
            log(f"[UF_EXPORT] run {run_id[:8]} créé : {len(idus)} parcelles")

    if not reuse:
        t0 = time.perf_counter()
        with engine.begin() as conn:
            compute_metrics_for_run(conn, project_id, run_id)
        log(f"[UF_EXPORT] profilers terminés en {time.perf_counter() - t0:.1f}s")

    with engine.connect() as conn:
        metrics = pool_service.get_all_metrics_grouped_by_idu(conn, project_id, run_id)
    log(f"[UF_EXPORT] métriques : {len(metrics)}/{len(idus)} parcelles")
    return run_id, metrics
