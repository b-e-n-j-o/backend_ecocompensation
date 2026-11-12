"""Dureté foncière des îlots RPG : un calcul par SIREN, un score pondéré par îlot.

Le cache `durete_fonciere_cache` est celui des parcelles (clé = SIREN).
Le bonus arrachage vigne (+15, plafond 100) s'applique à la personne morale
sur cet îlot seulement, il n'est pas écrit dans le cache.
`score_final` de l'îlot est la moyenne des scores pondérée par la surface
détenue ; les personnes morales sans score n'entrent pas dans la moyenne.
"""
from __future__ import annotations

import logging
import threading
import uuid
from collections import defaultdict

from sqlalchemy import text

from api.recherche_de_foncier.enrichir.durete_fonciere.V3_POOL.durete_cache import (
    get_cached_siren,
    set_cached_siren,
)
from api.recherche_de_foncier.enrichir.durete_fonciere.durete_v4.scoring import niveau
from api.recherche_de_foncier.enrichir.durete_fonciere.durete_v4.service import run_durete_for_siren
from api.recherche_de_foncier.enrichir.pool.profilers.durete_fonciere import (
    _is_public_pm,
    _normalize_score_final,
    _normalize_score_with_vigne_bonus,
    _normalize_siren,
    _payload_public_sans_siren,
)

from .api import get_results
from .params import RES_TABLE, SRC_SCHEMA

logger = logging.getLogger(__name__)

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_JOBS_MAX = 40


def weighted_score(parts: list[tuple[float, float]]) -> float | None:
    """parts = (score, poids surface). Ignore les poids nuls."""
    num = den = 0.0
    for score, weight in parts:
        if weight <= 0:
            continue
        num += score * weight
        den += weight
    if den <= 0:
        return None
    return round(num / den, 1)


def _patch_job(job_id: str, **fields) -> None:
    with _jobs_lock:
        current = _jobs.get(job_id)
        if current:
            current.update(fields)


def get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def _load_owner_rows(conn, project_id: str, num_ilots: list[str], millesime: str) -> list[dict]:
    membre = f"{SRC_SCHEMA}.rpg{millesime}_ilot_membre"
    sql = f"""
        SELECT il.num_ilot,
               il.surface_ha AS ilot_ha,
               pp.owner_key,
               pp.siren,
               pp.denomination,
               pp.categorie_juridique,
               pp.est_public,
               u.idu,
               COALESCE(pp.contenance_m2, 0) AS contenance_m2,
               ST_Area(c.geom) AS parc_m2,
               m.part_parcelle
        FROM {RES_TABLE} il
        CROSS JOIN LATERAL unnest(il.idus) AS u(idu)
        JOIN {SRC_SCHEMA}.cadastre_parcelles c ON c.idu = u.idu
        JOIN {SRC_SCHEMA}.pm_ilot_membre mem ON mem.idu = u.idu
        JOIN {SRC_SCHEMA}.pm_parcelle pp
          ON pp.owner_key = mem.owner_key AND pp.idu = u.idu
        LEFT JOIN {membre} m ON m.num_ilot = il.num_ilot AND m.idu = u.idu
        WHERE il.project_id = CAST(:pid AS uuid)
          AND il.num_ilot = ANY(CAST(:ids AS text[]))
    """
    rows = conn.execute(text(sql), {"pid": project_id, "ids": num_ilots}).mappings().all()
    return [dict(r) for r in rows]


def _vigne_idus(conn, idus: list[str]) -> set[str]:
    if not idus:
        return set()
    rows = conn.execute(
        text(
            """
            SELECT c.idu
            FROM ecocompensation.cadastre_parcelles c
            WHERE c.idu = ANY(CAST(:idus AS text[]))
              AND c.geom IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM ecocompensation.arrachage_vignes av
                  WHERE ST_Intersects(c.geom, av.geom_2154)
              )
            """
        ),
        {"idus": idus},
    ).fetchall()
    return {str(r[0]) for r in rows}


def _shares_by_ilot(rows: list[dict]) -> dict[str, list[dict]]:
    """Une entrée par personne morale et par îlot, surface en ha dans l'îlot."""
    by_parcel: dict[tuple[str, str], list[dict]] = defaultdict(list)
    ilot_ha: dict[str, float] = {}
    for r in rows:
        ilot = str(r["num_ilot"])
        ilot_ha[ilot] = float(r["ilot_ha"] or 0)
        by_parcel[(ilot, str(r["idu"]))].append(r)

    owners: dict[str, dict[str, dict]] = defaultdict(dict)
    for (ilot, idu), group in by_parcel.items():
        part = group[0].get("part_parcelle")
        part_f = float(part) if part is not None else 1.0
        contenances = [float(g["contenance_m2"] or 0) for g in group]
        use_split = sum(contenances) <= 0
        parc_m2 = float(group[0]["parc_m2"] or 0)
        for g, cont in zip(group, contenances):
            m2 = (parc_m2 / len(group) if use_split else cont) * part_f
            siren = _normalize_siren(g.get("siren"))
            key = siren or f"owner:{g['owner_key']}"
            slot = owners[ilot].get(key)
            if slot is None:
                slot = {
                    "key": key,
                    "siren": siren,
                    "siren_brut": str(g.get("siren") or "").strip() or None,
                    "denomination": str(g.get("denomination") or "").strip(),
                    "forme_juridique": str(g.get("categorie_juridique") or "").strip(),
                    "est_public": bool(g.get("est_public")),
                    "ha": 0.0,
                    "idus": [],
                }
                owners[ilot][key] = slot
            slot["ha"] += m2 / 10000.0
            if idu not in slot["idus"]:
                slot["idus"].append(idu)
    out: dict[str, list[dict]] = {}
    for ilot, slots in owners.items():
        total = ilot_ha.get(ilot) or 0
        items = list(slots.values())
        for slot in items:
            slot["surface_pct"] = round(100.0 * slot["ha"] / total, 1) if total > 0 else None
        items.sort(key=lambda s: -s["ha"])
        out[ilot] = items
    return out


def _pm_from_analysis(slot: dict, analysis: dict, vigne: bool) -> dict:
    base = _normalize_score_final(analysis.get("score_final"))
    score = _normalize_score_with_vigne_bonus(base, vigne)
    return {
        "siren": slot["siren"],
        "denomination": analysis.get("denomination") or slot["denomination"] or None,
        "forme_juridique": analysis.get("forme_juridique") or slot["forme_juridique"] or None,
        "surface_ha": round(slot["ha"], 4),
        "surface_pct": slot["surface_pct"],
        "score_llm_base": base,
        "score_final": score,
        "niveau_durete": analysis.get("niveau_durete"),
        "explication": analysis.get("explication"),
        "detail_axes": analysis.get("detail_axes"),
        "statut": analysis.get("statut") or "ok",
        "eligible": score is not None,
        "reason": analysis.get("reason"),
        "intersects_arrachage_vigne": vigne,
        "from_cache": bool(analysis.get("_from_cache") or analysis.get("from_cache")),
    }


def _ilot_payload(slots: list[dict], analyses: dict[str, dict], vigne_idus: set[str]) -> dict:
    if not slots:
        return {
            "eligible": False,
            "reason": "no_pm",
            "score_final": None,
            "niveau_durete": None,
            "surface_pm_pct": 0,
            "pms": [],
        }
    pms = []
    parts: list[tuple[float, float]] = []
    pm_ha = 0.0
    for slot in slots:
        vigne = any(i in vigne_idus for i in slot["idus"])
        pm = _pm_from_analysis(slot, analyses.get(slot["key"], {}), vigne)
        if pm["score_final"] is not None and vigne:
            pm["niveau_durete"] = niveau(int(round(pm["score_final"])))
        pms.append(pm)
        pm_ha += slot["ha"]
        if pm["score_final"] is not None:
            parts.append((float(pm["score_final"]), slot["ha"] if slot["ha"] > 0 else 1.0))
    score = weighted_score(parts)
    scored_ha = sum(slot["ha"] for pm, slot in zip(pms, slots) if pm["score_final"] is not None)
    ilot_ha_pm = pm_ha or 1.0
    return {
        "eligible": score is not None,
        "reason": None if score is not None else "no_score",
        "score_final": score,
        "niveau_durete": niveau(int(round(score))) if score is not None else None,
        "surface_pm_pct": round(min(100.0, sum(s["surface_pct"] or 0 for s in slots)), 1),
        "surface_scored_pct": round(100.0 * scored_ha / ilot_ha_pm, 1) if scored_ha else 0,
        "pms": pms,
    }


def _analyse_siren(conn, slot: dict) -> dict:
    siren = slot["siren"]
    if not siren:
        if slot["est_public"] or _is_public_pm(slot["forme_juridique"], slot["denomination"]):
            payload = _payload_public_sans_siren(
                siren_cadastre=slot["siren_brut"],
                denomination=slot["denomination"] or None,
                forme_juridique=slot["forme_juridique"] or None,
                intersects_vigne=False,
            )
            payload["from_cache"] = False
            return payload
        return {"score_final": None, "statut": "skipped", "reason": "missing_or_invalid_siren"}

    cached = get_cached_siren(conn, siren)
    try:
        conn.commit()
    except Exception:
        pass
    if cached is not None:
        return cached
    res = run_durete_for_siren(
        siren=siren,
        idus=slot["idus"],
        denomination=slot["denomination"],
        forme_juridique=slot["forme_juridique"],
        avec_recherche_web=True,
    )
    if not isinstance(res, dict) or res.get("statut") == "erreur":
        return {
            "score_final": None,
            "statut": "erreur",
            "reason": "pipeline_exception",
            "denomination": slot["denomination"],
            "forme_juridique": slot["forme_juridique"],
        }
    try:
        set_cached_siren(
            conn,
            siren,
            res,
            denomination=slot["denomination"],
            forme_juridique=slot["forme_juridique"],
        )
        conn.commit()
    except Exception:
        logger.warning("DURETE ILOTS CACHE WRITE FAIL | siren=%s", siren, exc_info=True)
        try:
            conn.rollback()
        except Exception:
            pass
    res["from_cache"] = False
    return res


def _representative_slots(shares: dict[str, list[dict]]) -> dict[str, dict]:
    """Un slot par clé (SIREN ou propriétaire public), en fusionnant les IDU."""
    out: dict[str, dict] = {}
    for slots in shares.values():
        for slot in slots:
            cur = out.get(slot["key"])
            if cur is None:
                out[slot["key"]] = {**slot, "idus": list(slot["idus"])}
                continue
            for idu in slot["idus"]:
                if idu not in cur["idus"]:
                    cur["idus"].append(idu)
            if not cur["denomination"] and slot["denomination"]:
                cur["denomination"] = slot["denomination"]
    return out


def _persist(conn, project_id: str, by_ilot: dict[str, dict]) -> None:
    import json

    row = conn.execute(
        text("SELECT last_results_ilots_rpg FROM ecocompensation.projects WHERE id = CAST(:pid AS uuid)"),
        {"pid": project_id},
    ).scalar()
    if isinstance(row, str):
        row = json.loads(row)
    if not isinstance(row, dict):
        return
    for il in row.get("ilots") or []:
        num = str(il.get("num_ilot"))
        if num in by_ilot:
            il["durete_fonciere"] = by_ilot[num]
    conn.execute(
        text(
            """
            UPDATE ecocompensation.projects
            SET last_results_ilots_rpg = CAST(:payload AS jsonb), updated_at = now()
            WHERE id = CAST(:pid AS uuid)
            """
        ),
        {"pid": project_id, "payload": json.dumps(row, default=str)},
    )
    conn.commit()


def compute_ilots_durete(conn, project_id: str, num_ilots: list[str] | None, on_progress=None) -> dict:
    results = get_results(conn.engine, project_id)
    ilots = (results or {}).get("ilots") or []
    millesime = str((results or {}).get("millesime") or "2025")
    wanted = [str(il.get("num_ilot")) for il in ilots if il.get("num_ilot")]
    if num_ilots is not None:
        keep = {str(n) for n in num_ilots}
        wanted = [n for n in wanted if n in keep]
    if not wanted:
        return {"done_ilots": 0, "total_ilots": 0, "remaining_ilots": 0, "phase": "done"}

    shares = _shares_by_ilot(_load_owner_rows(conn, project_id, wanted, millesime))
    reps = _representative_slots(shares)
    all_idus = sorted({idu for slot in reps.values() for idu in slot["idus"]})
    vigne = _vigne_idus(conn, all_idus)
    try:
        conn.commit()
    except Exception:
        pass

    total = len(reps)
    analyses: dict[str, dict] = {}
    if on_progress:
        on_progress({"phase": "siren", "done_ilots": 0, "total_ilots": len(wanted), "remaining_ilots": len(wanted), "done_sirens": 0, "total_sirens": total})
    for i, (key, slot) in enumerate(reps.items(), 1):
        try:
            analyses[key] = _analyse_siren(conn, slot)
        except Exception:
            logger.exception("DURETE ILOTS | siren=%s", slot.get("siren"))
            analyses[key] = {"score_final": None, "statut": "erreur", "reason": "pipeline_exception"}
        if on_progress:
            on_progress({
                "phase": "siren",
                "done_sirens": i,
                "total_sirens": total,
                "done_ilots": 0,
                "total_ilots": len(wanted),
                "remaining_ilots": len(wanted),
            })

    by_ilot = {}
    for num in wanted:
        by_ilot[num] = _ilot_payload(shares.get(num, []), analyses, vigne)
    _persist(conn, project_id, by_ilot)
    stats = {
        "phase": "done",
        "done_ilots": len(wanted),
        "total_ilots": len(wanted),
        "remaining_ilots": 0,
        "done_sirens": total,
        "total_sirens": total,
    }
    if on_progress:
        on_progress(stats)
    return stats


def start_job(engine, project_id: str, num_ilots: list[str] | None) -> str:
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "running",
            "project_id": project_id,
            "phase": "queued",
            "done_ilots": 0,
            "total_ilots": len(num_ilots) if num_ilots else None,
            "remaining_ilots": len(num_ilots) if num_ilots else None,
        }
        if len(_jobs) > _JOBS_MAX:
            for k, v in list(_jobs.items()):
                if v.get("status") in {"ok", "error"}:
                    _jobs.pop(k, None)

    def _run() -> None:
        try:
            with engine.connect() as conn:
                stats = compute_ilots_durete(
                    conn, project_id, num_ilots, on_progress=lambda info: _patch_job(job_id, **info),
                )
            _patch_job(job_id, status="ok", **stats)
        except Exception as exc:
            logger.exception("DURETE ILOTS JOB FAIL | project=%s", project_id)
            _patch_job(job_id, status="error", error=str(exc) or type(exc).__name__)

    threading.Timer(0.25, _run).start()
    return job_id
