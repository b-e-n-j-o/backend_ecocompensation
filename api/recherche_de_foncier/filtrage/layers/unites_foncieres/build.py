"""
build.py — PM de l'AOI → parcelles retirées → terrains (SIREN + contiguïté) → verdict.

Étapes (chacune loguée avec ses comptes) :
  1. PM dans l'AOI (base PPM), SIREN ayant >= 2 parcelles, copiées dans uf_parcelles
  2. Paires de parcelles qui se touchent (même SIREN), calculées une seule fois
  3. Blocs trop petits (1 parcelle ou surface < min) retirés : « isolee » / « bloc_trop_petit »
  4. Critères éliminatoires parcelle par parcelle (mêmes clauses que le filtre parcelles),
     testés seulement sur les parcelles encore actives ; la 1re clause en échec est notée
  5. Nouveau découpage : une parcelle retirée coupe le bloc en deux
  6. Critères d'existence / faune / flore / ZH sur les parcelles actives
  7. Terrains = composantes restantes -> union (shapely) -> verdict -> uf_terrains
  8. Réévaluation des UF manuelles avec la même config
"""
from __future__ import annotations

import logging
import os
import time
from collections import Counter, defaultdict
from typing import Callable

from sqlalchemy import text

from . import tables
from .criteres import build_criteres
from .evaluation import (
    ELIMINATOIRE,
    ZH_SURFACE,
    Critere,
    ParcelleUF,
    components,
    evaluer_terrain,
)
from .geom import union_metrics
from .params import UfParams

logger = logging.getLogger(__name__)

PARCELLES_PM_TABLE = os.getenv("PARCELLES_PM_TABLE", "public.parcelles_personnes_morales")
P, T = tables.P, tables.T

SCOPE_PM_ACTIF = "p.source = 'pm' AND p.eliminee_par IS NULL"
SCOPE_MANUEL = "p.source = 'manuel' AND (CAST(:_uf_ids AS text[]) IS NULL OR p.uf_id = ANY(CAST(:_uf_ids AS text[])))"

Log = Callable[[str], None]


def _logger(cb: Log | None) -> Log:
    def log(msg: str) -> None:
        logger.info("[uf] %s", msg)
        if cb:
            cb(msg)
    return log


# --------------------------------------------------------------------------- AOI / PPM
def _get_aoi(conn, aoi_id: str) -> tuple[str, float, float]:
    row = conn.execute(text("""
        SELECT ST_AsText(geom_2154) AS wkt,
               ST_X(ST_Centroid(geom_2154)) AS cx, ST_Y(ST_Centroid(geom_2154)) AS cy
        FROM ecocompensation.aoi WHERE id = CAST(:aid AS uuid)"""), {"aid": aoi_id}).mappings().one_or_none()
    if not row or not row["wkt"]:
        raise RuntimeError(f"AOI introuvable : {aoi_id}")
    return str(row["wkt"]), float(row["cx"]), float(row["cy"])


def aoi_centre_for_project(conn, project_id: str) -> tuple[float, float]:
    row = conn.execute(text("""
        SELECT ST_X(ST_Centroid(a.geom_2154)) AS cx, ST_Y(ST_Centroid(a.geom_2154)) AS cy
        FROM ecocompensation.projects pr JOIN ecocompensation.aoi a ON a.id = pr.aoi_id
        WHERE pr.id = CAST(:pid AS uuid)"""), {"pid": project_id}).mappings().one()
    return float(row["cx"]), float(row["cy"])


def _dept_codes(conn, project_id: str, aoi_wkt: str) -> list[str]:
    """Départements de l'AOI (préfiltre attributaire PPM) : pool du projet, sinon cadastre."""
    for sql, params in (
        ("""SELECT DISTINCT left(trim(code_insee), 2) FROM ecocompensation_results.parcelles
            WHERE project_id = CAST(:pid AS uuid) AND length(trim(code_insee)) >= 2""",
         {"pid": project_id}),
        ("""SELECT DISTINCT left(trim(code_insee), 2) FROM ecocompensation.parcelles
            WHERE geom_2154 && ST_GeomFromText(:w, 2154) AND length(trim(code_insee)) >= 2""",
         {"w": aoi_wkt}),
    ):
        depts = sorted({d.strip().upper() for d in conn.execute(text(sql), params).scalars()
                        if d and len(d.strip()) == 2 and d.strip().isalnum()})
        if depts:
            return depts
    return []


def _fetch_ppm(conn, aoi_wkt: str, depts: list[str], siren: str | None) -> list[dict]:
    params: dict = {"w": aoi_wkt}
    extra = ""
    if depts:
        params["likes"] = [f"{d}%" for d in depts]
        extra += " AND code_insee LIKE ANY(CAST(:likes AS text[]))"
    if siren:
        params["siren"] = siren
        extra += " AND siren = :siren"
    rows = conn.execute(text(f"""
        SELECT DISTINCT ON (siren, idu)
               idu, siren, denomination, forme_juridique, ST_AsText(geom_2154) AS wkt
        FROM {PARCELLES_PM_TABLE}
        WHERE geom_2154 && ST_GeomFromText(:w, 2154)
          AND ST_Intersects(geom_2154, ST_GeomFromText(:w, 2154))
          AND siren IS NOT NULL AND siren <> '' {extra}
        ORDER BY siren, idu"""), params).mappings().all()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- staging
_STAGE_SQL = f"""
INSERT INTO {P} (project_id, source, idu, siren, denomination, forme_juridique, surface_m2, geom_2154)
SELECT CAST(:pid AS uuid), 'pm', r.idu, r.siren, r.denomination, r.forme_juridique, ST_Area(g.geom), g.geom
FROM jsonb_to_recordset(CAST(:rows AS jsonb))
     AS r(idu text, siren text, denomination text, forme_juridique text, wkt text)
CROSS JOIN LATERAL (SELECT ST_GeomFromText(r.wkt, 2154) AS geom) g
WHERE g.geom IS NOT NULL
"""


def _reset_pm(conn, project_id: str, siren: str | None) -> None:
    cond = " AND siren = :siren" if siren else ""
    params = {"pid": project_id, "siren": siren}
    conn.execute(text(f"DELETE FROM {T} WHERE project_id = CAST(:pid AS uuid) AND source = 'pm'{cond}"), params)
    conn.execute(text(f"DELETE FROM {P} WHERE project_id = CAST(:pid AS uuid) AND source = 'pm'{cond}"), params)


def _pairs(conn, project_id: str, source: str = "pm") -> list[tuple[int, int]]:
    rows = conn.execute(text(f"""
        SELECT a.id, b.id
        FROM {P} a
        JOIN {P} b
          ON b.project_id = a.project_id AND b.source = a.source
         AND b.siren = a.siren AND a.id < b.id
         AND a.geom_2154 && b.geom_2154 AND {tables.CONTACT_SQL}
        WHERE a.project_id = CAST(:pid AS uuid) AND a.source = :source"""),
        {"pid": project_id, "source": source}).all()
    return [(int(a), int(b)) for a, b in rows]


def _surfaces_actives(conn, project_id: str) -> dict[int, float]:
    rows = conn.execute(text(f"""
        SELECT p.id, p.surface_m2 FROM {P} p
        WHERE p.project_id = CAST(:pid AS uuid) AND {SCOPE_PM_ACTIF}"""), {"pid": project_id}).all()
    return {int(i): float(s or 0) for i, s in rows}


def _retirer_petits_blocs(conn, project_id: str, surfaces: dict[int, float],
                          pairs: list[tuple[int, int]], params: UfParams, suffixe: str) -> Counter:
    marks: list[dict] = []
    for comp in components(list(surfaces), pairs):
        if len(comp) < params.min_parcelles:
            motif = "isolee"
        elif sum(surfaces[i] for i in comp) < params.min_area_m2:
            motif = "bloc_trop_petit"
        else:
            continue
        marks.extend({"id": i, "motif": f"{motif}{suffixe}"} for i in comp)
    tables.exec_recordset(conn, f"""
        UPDATE {P} p SET eliminee_par = r.motif
        FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS r(id bigint, motif text)
        WHERE p.id = r.id""", marks)
    return Counter(m["motif"] for m in marks)


# --------------------------------------------------------------------------- critères SQL
def appliquer_critere(conn, project_id: str, c: Critere, sql_params: dict, scope: str,
                      uf_ids: list[str] | None = None) -> None:
    """Écrit le booléen du critère dans criteres[c.key] (et zh_m2 pour la surface ZH)."""
    params = {**sql_params, "_crit_key": c.key, "_uf_ids": uf_ids}
    tables.set_statement_timeout(conn)
    if c.kind == ZH_SURFACE:
        conn.execute(text(f"""
            UPDATE {P} p SET zh_m2 = z.m2,
                   criteres = p.criteres || jsonb_build_object(CAST(:_crit_key AS text), z.m2 > 0)
            FROM (SELECT p.id, ({c.sql}) AS m2 FROM {P} p
                  WHERE p.project_id = CAST(:project_id AS uuid) AND {scope}) z
            WHERE p.id = z.id"""), params)
        return
    conn.execute(text(f"""
        UPDATE {P} p
        SET criteres = p.criteres || jsonb_build_object(CAST(:_crit_key AS text), COALESCE(({c.sql}), FALSE))
        WHERE p.project_id = CAST(:project_id AS uuid) AND {scope}"""), params)


def _eliminer(conn, project_id: str, c: Critere, sql_params: dict) -> int:
    appliquer_critere(conn, project_id, c, sql_params, SCOPE_PM_ACTIF)
    return conn.execute(text(f"""
        UPDATE {P} p SET eliminee_par = CAST(:_crit_key AS text)
        WHERE p.project_id = CAST(:project_id AS uuid) AND {SCOPE_PM_ACTIF}
          AND p.criteres ->> CAST(:_crit_key AS text) = 'false'"""),
        {"project_id": project_id, "_crit_key": c.key}).rowcount or 0


# --------------------------------------------------------------------------- terrains
def _load_parcelles(conn, project_id: str, scope: str, uf_ids: list[str] | None = None) -> list[tuple[str | None, ParcelleUF]]:
    rows = conn.execute(text(f"""
        SELECT p.id, p.uf_id, p.idu, p.siren, p.denomination, p.forme_juridique, p.surface_m2,
               p.criteres, p.zh_m2, ST_AsText(p.geom_2154) AS wkt
        FROM {P} p WHERE p.project_id = CAST(:project_id AS uuid) AND {scope}
        ORDER BY p.id"""), {"project_id": project_id, "_uf_ids": uf_ids}).mappings().all()
    return [(r["uf_id"], ParcelleUF(
        id=int(r["id"]), idu=r["idu"], siren=r["siren"], surface_m2=float(r["surface_m2"] or 0),
        criteres=dict(r["criteres"] or {}), zh_m2=float(r["zh_m2"] or 0), wkt=r["wkt"],
        denomination=r["denomination"], forme_juridique=r["forme_juridique"],
    )) for r in rows]


def _terrain_row(uf_id: str, source: str, parcelles: list[ParcelleUF], criteres: list[Critere],
                 params: UfParams, cx: float, cy: float) -> dict:
    m = union_metrics([p.wkt for p in parcelles], cx, cy) or {}
    verdict = evaluer_terrain(parcelles, criteres, params.regles(), miller=m.get("miller"))
    head = parcelles[0]
    return {
        "uf_id": uf_id, "source": source,
        "siren": head.siren if source == "pm" else "manuel",
        "denomination": head.denomination if source == "pm" else "Ajout manuel",
        "forme_juridique": head.forme_juridique if source == "pm" else None,
        "nb_parcelles": len(parcelles), "idus": [p.idu for p in parcelles],
        "surface_ha": round(m.get("surface_ha", sum(p.surface_m2 for p in parcelles) / 1e4), 4),
        "miller": round(m["miller"], 6) if "miller" in m else None,
        "dist_centre_m": round(m["dist_centre_m"], 1) if "dist_centre_m" in m else None,
        "wkt": m.get("wkt"), "verdict": verdict.ok, "motif_rejet": verdict.motif,
        "criteres": verdict.detail,
    }


def _construire_terrains_pm(conn, project_id: str, pairs, criteres, params: UfParams,
                            cx: float, cy: float, log: Log) -> Counter:
    parcelles = {p.id: p for _uf, p in _load_parcelles(conn, project_id, SCOPE_PM_ACTIF)}
    rows, assign = [], []
    n_par_siren: Counter = Counter()
    for comp in components(list(parcelles), pairs):
        if len(comp) < params.min_parcelles:
            continue
        ps = [parcelles[i] for i in comp]
        siren = ps[0].siren or "inconnu"
        n_par_siren[siren] += 1
        uf_id = f"{siren}_{n_par_siren[siren]:03d}"
        rows.append(_terrain_row(uf_id, "pm", ps, criteres, params, cx, cy))
        assign.extend({"id": p.id, "uf_id": uf_id} for p in ps)

    tables.exec_recordset(conn, tables.UPSERT_TERRAINS_SQL, rows, {"pid": project_id}, batch=200)
    tables.exec_recordset(conn, f"""
        UPDATE {P} p SET uf_id = r.uf_id
        FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS r(id bigint, uf_id text)
        WHERE p.id = r.id""", assign)

    diag = Counter(terrains_evalues=len(rows), terrains_retenus=sum(r["verdict"] for r in rows))
    for r in rows:
        if not r["verdict"]:
            diag[f"rejet:{r['motif_rejet']}"] += 1
        log(f"   {'✓' if r['verdict'] else '✗'} {r['uf_id']:<16} {r['nb_parcelles']:>4} parcelles "
            f"{r['surface_ha']:>8.2f} ha  miller={r['miller']}"
            + ("" if r["verdict"] else f"  rejet={r['motif_rejet']}"))
    return diag


def evaluer_manuels(engine, project_id: str, params: UfParams, log: Log | None = None, *,
                    uf_ids: list[str] | None = None,
                    criteres_params: tuple[list[Critere], dict] | None = None) -> int:
    """(Ré)évalue les UF manuelles : tous les critères en booléens, aucune parcelle retirée."""
    log = log or _logger(None)
    criteres, sql_params = criteres_params or build_criteres(
        engine, project_id, params.config, cesbio_par_parcelle=params.cesbio_par_parcelle, log=log)
    with engine.begin() as conn:
        conn.execute(text(f"""
            UPDATE {P} p SET criteres = '{{}}', zh_m2 = 0
            WHERE p.project_id = CAST(:project_id AS uuid) AND {SCOPE_MANUEL}"""),
            {"project_id": project_id, "_uf_ids": uf_ids})
        for c in criteres:
            appliquer_critere(conn, project_id, c, sql_params, SCOPE_MANUEL, uf_ids)
        by_uf: dict[str, list[ParcelleUF]] = defaultdict(list)
        for uf_id, p in _load_parcelles(conn, project_id, SCOPE_MANUEL, uf_ids):
            by_uf[uf_id].append(p)
        if not by_uf:
            return 0
        cx, cy = aoi_centre_for_project(conn, project_id)
        rows = [_terrain_row(uf, "manuel", ps, criteres, params, cx, cy) for uf, ps in by_uf.items()]
        tables.exec_recordset(conn, tables.UPSERT_TERRAINS_SQL, rows, {"pid": project_id}, batch=200)
    for r in rows:
        log(f"   UF manuelle {r['uf_id']} : {'passe' if r['verdict'] else 'échoue (' + str(r['motif_rejet']) + ')'}")
    return len(rows)


# --------------------------------------------------------------------------- run
def run(engine, project_id: str, aoi_id: str, params: UfParams, cb: Log | None = None,
        *, engine_ppm=None) -> dict:
    """Construit et évalue les terrains PM du projet. Retourne le diagnostic (entonnoir)."""
    log = _logger(cb)
    t0 = time.perf_counter()
    engine_ppm = engine_ppm or engine
    diag: Counter = Counter()
    tables.ensure_tables(engine)

    with engine.begin() as conn:
        _reset_pm(conn, project_id, params.siren)
        aoi_wkt, cx, cy = _get_aoi(conn, aoi_id)
        depts = _dept_codes(conn, project_id, aoi_wkt)
    log(f"PHASE:unites_foncieres:start départements={','.join(depts) or '—'}"
        + (f" siren={params.siren}" if params.siren else ""))

    t = time.perf_counter()
    with engine_ppm.connect() as conn:
        ppm = _fetch_ppm(conn, aoi_wkt, depts, params.siren)
    nb_par_siren = Counter(r["siren"] for r in ppm)
    ppm = [r for r in ppm if nb_par_siren[r["siren"]] >= params.min_parcelles]
    diag["ppm_aoi"] = sum(nb_par_siren.values())
    diag["ppm_siren_multi"] = len(ppm)
    log(f"1. PM dans l'AOI : {diag['ppm_aoi']:,} parcelles, {len(nb_par_siren):,} SIREN "
        f"-> {len(ppm):,} parcelles de SIREN à ≥ {params.min_parcelles} parcelles "
        f"({time.perf_counter() - t:.1f}s)")

    if ppm:
        criteres, sql_params = build_criteres(
            engine, project_id, params.config, cesbio_par_parcelle=params.cesbio_par_parcelle, log=log)
        log("   Critères : " + " | ".join(f"{c.key}[{c.kind}]" for c in criteres))

        with engine.begin() as conn:
            tables.exec_recordset(conn, _STAGE_SQL, ppm, {"pid": project_id})
            t = time.perf_counter()
            pairs = _pairs(conn, project_id)
            log(f"2. Contiguïté : {len(pairs):,} paires de parcelles qui se touchent ({time.perf_counter() - t:.1f}s)")

            retires = _retirer_petits_blocs(conn, project_id, _surfaces_actives(conn, project_id),
                                            pairs, params, "")
            diag.update(retires)
            log(f"3. Blocs écartés avant critères : {dict(retires) or 'aucun'}")

            log("4. Critères éliminatoires (parcelle par parcelle)")
            for c in (c for c in criteres if c.kind == ELIMINATOIRE):
                t = time.perf_counter()
                n = _eliminer(conn, project_id, c, sql_params)
                diag[f"elimine:{c.key}"] = n
                log(f"   ✗ {c.label} : {n:,} parcelles retirées ({time.perf_counter() - t:.1f}s)")

            retires = _retirer_petits_blocs(conn, project_id, _surfaces_actives(conn, project_id),
                                            pairs, params, "_apres_exclusions")
            diag.update(retires)
            n_actives = len(_surfaces_actives(conn, project_id))
            log(f"5. Nouveau découpage : {dict(retires) or 'rien de plus'} -> {n_actives:,} parcelles actives")

            log("6. Critères d'existence / faune / flore / ZH (parcelles actives)")
            for c in (c for c in criteres if c.kind != ELIMINATOIRE):
                t = time.perf_counter()
                appliquer_critere(conn, project_id, c, sql_params, SCOPE_PM_ACTIF)
                log(f"   · {c.label} ({time.perf_counter() - t:.1f}s)")

            log("7. Terrains")
            diag.update(_construire_terrains_pm(conn, project_id, pairs, criteres, params, cx, cy, log))

        log("8. UF manuelles")
        diag["manuels"] = evaluer_manuels(engine, project_id, params, log,
                                          criteres_params=(criteres, sql_params))
    else:
        diag["manuels"] = evaluer_manuels(engine, project_id, params, log)

    diag["secondes"] = round(time.perf_counter() - t0, 1)
    log(f"PHASE:unites_foncieres:done {diag.get('terrains_retenus', 0)} terrains retenus "
        f"/ {diag.get('terrains_evalues', 0)} évalués ({diag['secondes']}s)")
    return dict(diag)
