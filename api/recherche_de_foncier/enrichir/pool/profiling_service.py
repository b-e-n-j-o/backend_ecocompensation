from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable

from sqlalchemy import text

from api.recherche_de_foncier.enrichir.pool import pool_service
from api.recherche_de_foncier.enrichir.pool.profiler_registry import (
    get_profilers_for_study_type,
    profiling_summary_label,
    resolve_study_type,
)
from api.recherche_de_foncier.enrichir.pool.profilers.autorisations_urba import AutorisationsUrbaProfiler
from api.recherche_de_foncier.enrichir.pool.profilers.composite_score_v1 import CompositeScoreV1Profiler
from api.recherche_de_foncier.enrichir.pool.profilers.durete_fonciere import DureteFonciereProfiler
from api.recherche_de_foncier.enrichir.pool.profilers.personnes_morales import PersonnesMoralesProfiler
from api.recherche_de_foncier.enrichir.pool.profilers.score_eco import ScoreEcoProfiler

logger = logging.getLogger(__name__)


def compute_metrics_for_run(conn, project_id: str, run_id: str) -> None:
    t0 = time.perf_counter()
    pool_service.ensure_tables(conn)
    study_type = resolve_study_type(conn, project_id, run_id)
    profilers = get_profilers_for_study_type(study_type)
    try:
        idu_total = conn.execute(
            text(
                """
                SELECT COUNT(*)
                FROM ecocompensation_results.parcelles_pool
                WHERE project_id = CAST(:project_id AS uuid)
                  AND run_id = CAST(:run_id AS uuid)
                """
            ),
            {"project_id": project_id, "run_id": run_id},
        ).scalar_one()
    except Exception:
        idu_total = None

    logger.info(
        "POOL RUN START | project_id=%s run_id=%s study_type=%s profilers=%s parcelles_pool=%s",
        project_id,
        run_id,
        study_type,
        [p.metric_key for p in profilers],
        idu_total if idu_total is not None else "unknown",
    )
    try:
        conn.execute(text("SET LOCAL statement_timeout = '15min'"))
    except Exception:
        pass
    n_ok = 0
    n_err = 0
    total_upserts = 0
    for idx, profiler in enumerate(profilers, 1):
        p0 = time.perf_counter()
        logger.info(
            "POOL PHASE [%d/%d] START | metric=%s project_id=%s run_id=%s",
            idx,
            len(profilers),
            profiler.metric_key,
            project_id,
            run_id,
        )
        try:
            with conn.begin_nested():
                payload_by_idu = profiler.compute_for_run(conn, project_id, run_id)
                upserts = 0
                for idu, payload in payload_by_idu.items():
                    pool_service.upsert_metric(
                        conn,
                        project_id=project_id,
                        run_id=run_id,
                        idu=idu,
                        metric_key=profiler.metric_key,
                        metric_value=payload,
                    )
                    upserts += 1
                total_upserts += upserts
                n_ok += 1
                elapsed = time.perf_counter() - p0
                logger.info(
                    "POOL PHASE [%d/%d] DONE | metric=%s upserts=%d duration_s=%.2f",
                    idx,
                    len(profilers),
                    profiler.metric_key,
                    upserts,
                    elapsed,
                )
        except Exception:
            n_err += 1
            elapsed = time.perf_counter() - p0
            logger.exception(
                "POOL PHASE [%d/%d] ERROR | metric=%s duration_s=%.2f project_id=%s run_id=%s",
                idx,
                len(profilers),
                profiler.metric_key,
                elapsed,
                project_id,
                run_id,
            )
            continue
    total_s = time.perf_counter() - t0
    logger.info(
        "POOL RUN COMPLETE | project_id=%s run_id=%s phases_ok=%d phases_err=%d total_upserts=%d total_s=%.2f",
        project_id,
        run_id,
        n_ok,
        n_err,
        total_upserts,
        total_s,
    )


def _upsert_profiler_payload(
    conn,
    project_id: str,
    run_id: str,
    metric_key: str,
    payload_by_idu: dict,
) -> int:
    count = 0
    for idu, payload in payload_by_idu.items():
        pool_service.upsert_metric(
            conn,
            project_id=project_id,
            run_id=run_id,
            idu=idu,
            metric_key=metric_key,
            metric_value=payload,
        )
        count += 1
    return count


def compute_durete_for_run(
    conn,
    project_id: str,
    run_id: str,
    *,
    exclude_indesirables: bool = True,
    only_idus: set[str] | None = None,
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    """
    Calcule la dureté foncière (attractivité) pour les parcelles du run pool.

    - Pré-requis PM : rafraîchit `parcelles_personnes_morales` sur tout le run.
    - Dureté : uniquement sur les IDU actifs (hors pool indésirables projet si demandé),
      éventuellement restreints à `only_idus` (sélection UI).
    - Met à jour `composite_score_v1` après la dureté (tout le run, pour rester cohérent).
    """
    t0 = time.perf_counter()
    pool_service.ensure_tables(conn)
    try:
        conn.execute(text("SET LOCAL statement_timeout = '30min'"))
    except Exception:
        pass
    try:
        conn.commit()
    except Exception:
        pass

    pool_idus = {
        str(r["idu"])
        for r in pool_service.get_pool(conn, project_id=project_id, run_id=run_id)
        if r.get("idu")
    }
    skipped_indesirables = 0
    if exclude_indesirables:
        excluded = set(pool_service.list_project_indesirable_idus(conn, project_id))
        active_idus = pool_idus - excluded
        skipped_indesirables = len(pool_idus & excluded)
    else:
        active_idus = pool_idus

    requested_count = 0
    if only_idus is not None:
        requested = {str(x).strip() for x in only_idus if str(x).strip()}
        requested_count = len(requested)
        active_idus = active_idus & requested

    def progress(**kwargs) -> None:
        if on_progress:
            on_progress(kwargs)

    if not active_idus:
        empty = {
            "updated_count": 0,
            "active_idus": 0,
            "requested_idus": requested_count,
            "skipped_indesirables": skipped_indesirables,
            "eligible_pm": 0,
            "composite_updated": 0,
            "duration_s": round(time.perf_counter() - t0, 2),
            "done_idus": 0,
            "total_idus": 0,
            "remaining_idus": 0,
            "phase": "done",
        }
        progress(**empty)
        return empty

    total_idus = len(active_idus)
    progress(
        phase="pm",
        done_idus=0,
        total_idus=total_idus,
        remaining_idus=total_idus,
        active_idus=total_idus,
        requested_idus=requested_count,
        skipped_indesirables=skipped_indesirables,
    )

    pm_profiler = PersonnesMoralesProfiler()
    pm_payload = pm_profiler.compute_for_run(conn, project_id, run_id)
    pm_upserts = _upsert_profiler_payload(
        conn, project_id, run_id, pm_profiler.metric_key, pm_payload
    )
    try:
        conn.commit()
    except Exception:
        pass

    durete_upserts = 0

    def flush_durete(part: dict) -> None:
        nonlocal durete_upserts
        n = _upsert_profiler_payload(
            conn, project_id, run_id, "durete_fonciere", part
        )
        durete_upserts += n
        try:
            conn.commit()
        except Exception:
            pass
        done = min(durete_upserts, total_idus)
        progress(
            phase="scoring",
            done_idus=done,
            total_idus=total_idus,
            remaining_idus=max(0, total_idus - done),
            updated_count=durete_upserts,
            active_idus=total_idus,
            requested_idus=requested_count,
            skipped_indesirables=skipped_indesirables,
            pm_upserts=pm_upserts,
        )

    durete_profiler = DureteFonciereProfiler()
    durete_payload = durete_profiler.compute_for_run(
        conn, project_id, run_id, only_idus=active_idus, on_payloads=flush_durete
    )

    eligible_pm = sum(
        1
        for idu in active_idus
        if (durete_payload.get(idu) or {}).get("eligible") is True
    )

    progress(
        phase="composite",
        done_idus=total_idus,
        total_idus=total_idus,
        remaining_idus=0,
        updated_count=durete_upserts,
        active_idus=total_idus,
    )

    composite_profiler = CompositeScoreV1Profiler()
    composite_payload = composite_profiler.compute_for_run(conn, project_id, run_id)
    composite_upserts = _upsert_profiler_payload(
        conn, project_id, run_id, composite_profiler.metric_key, composite_payload
    )
    try:
        conn.commit()
    except Exception:
        pass

    duration_s = round(time.perf_counter() - t0, 2)
    logger.info(
        "DURETE POOL DONE | project_id=%s run_id=%s active_idus=%d requested_idus=%d "
        "skipped_indesirables=%d durete_upserts=%d eligible_pm=%d composite_upserts=%d duration_s=%.2f",
        project_id,
        run_id,
        len(active_idus),
        requested_count,
        skipped_indesirables,
        durete_upserts,
        eligible_pm,
        composite_upserts,
        duration_s,
    )
    return {
        "updated_count": durete_upserts,
        "active_idus": len(active_idus),
        "requested_idus": requested_count,
        "skipped_indesirables": skipped_indesirables,
        "eligible_pm": eligible_pm,
        "pm_upserts": pm_upserts,
        "composite_updated": composite_upserts,
        "duration_s": duration_s,
        "done_idus": total_idus,
        "total_idus": total_idus,
        "remaining_idus": 0,
        "phase": "done",
    }


def compute_autorisations_urba_for_run(
    conn,
    project_id: str,
    run_id: str,
    *,
    exclude_indesirables: bool = True,
    only_idus: set[str] | None = None,
    avec_voisins: bool = True,
) -> dict:
    """
    Autorisations Sitadel pour les parcelles du run.

    N'appelle ni la dureté foncière, ni le score composite.
    Écrit uniquement la métrique ``autorisations_urba``.
    """
    t0 = time.perf_counter()
    pool_service.ensure_tables(conn)
    try:
        conn.execute(text("SET LOCAL statement_timeout = '10min'"))
    except Exception:
        pass

    pool_idus = {
        str(r["idu"])
        for r in pool_service.get_pool(conn, project_id=project_id, run_id=run_id)
        if r.get("idu")
    }
    skipped_indesirables = 0
    if exclude_indesirables:
        excluded = set(pool_service.list_project_indesirable_idus(conn, project_id))
        active_idus = pool_idus - excluded
        skipped_indesirables = len(pool_idus & excluded)
    else:
        active_idus = pool_idus

    requested_count = 0
    if only_idus is not None:
        requested = {str(x).strip() for x in only_idus if str(x).strip()}
        requested_count = len(requested)
        active_idus = active_idus & requested

    if not active_idus:
        return {
            "updated_count": 0,
            "active_idus": 0,
            "requested_idus": requested_count,
            "skipped_indesirables": skipped_indesirables,
            "avec_permis_parcelle": 0,
            "avec_permis_voisin": 0,
            "duration_s": round(time.perf_counter() - t0, 2),
        }

    profiler = AutorisationsUrbaProfiler()
    payload = profiler.compute_for_run(
        conn,
        project_id,
        run_id,
        only_idus=active_idus,
        avec_voisins=avec_voisins,
    )
    upserts = _upsert_profiler_payload(
        conn, project_id, run_id, profiler.metric_key, payload
    )
    avec_permis_parcelle = sum(1 for item in payload.values() if item.get("concerne_parcelle"))
    avec_permis_voisin = sum(1 for item in payload.values() if item.get("concerne_voisin"))
    duration_s = round(time.perf_counter() - t0, 2)
    logger.info(
        "AUTORISATIONS URBA DONE | project_id=%s run_id=%s active_idus=%d "
        "upserts=%d permis_parcelle=%d permis_voisin=%d duration_s=%.2f",
        project_id,
        run_id,
        len(active_idus),
        upserts,
        avec_permis_parcelle,
        avec_permis_voisin,
        duration_s,
    )
    return {
        "updated_count": upserts,
        "active_idus": len(active_idus),
        "requested_idus": requested_count,
        "skipped_indesirables": skipped_indesirables,
        "avec_permis_parcelle": avec_permis_parcelle,
        "avec_permis_voisin": avec_permis_voisin,
        "duration_s": duration_s,
    }


URBA_METRIC_KEY = "urbanisme_gpu"


def _active_pool_idus(
    conn,
    project_id: str,
    run_id: str,
    *,
    exclude_indesirables: bool,
    only_idus: set[str] | None,
) -> tuple[set[str], int, int]:
    pool_idus = {
        str(r["idu"])
        for r in pool_service.get_pool(conn, project_id=project_id, run_id=run_id)
        if r.get("idu")
    }
    skipped_indesirables = 0
    if exclude_indesirables:
        excluded = set(pool_service.list_project_indesirable_idus(conn, project_id))
        active = pool_idus - excluded
        skipped_indesirables = len(pool_idus & excluded)
    else:
        active = pool_idus
    requested_count = 0
    if only_idus is not None:
        requested = {str(x).strip() for x in only_idus if str(x).strip()}
        requested_count = len(requested)
        active = active & requested
    return active, requested_count, skipped_indesirables


def load_urba_targets(
    conn,
    project_id: str,
    run_id: str,
    *,
    exclude_indesirables: bool = True,
    only_idus: set[str] | None = None,
) -> dict:
    """Parcelles du run prêtes pour le profiler GPU (géométrie GeoJSON 2154)."""
    active, requested_count, skipped = _active_pool_idus(
        conn,
        project_id,
        run_id,
        exclude_indesirables=exclude_indesirables,
        only_idus=only_idus,
    )
    if not active:
        return {
            "parcelles": [],
            "active_idus": 0,
            "requested_idus": requested_count,
            "skipped_indesirables": skipped,
            "missing_geom": 0,
        }
    rows = conn.execute(
        text(
            """
            SELECT p.idu, ST_AsGeoJSON(p.geom_2154) AS geometry
            FROM ecocompensation_results.parcelles_pool pp
            JOIN ecocompensation_results.parcelles p
              ON p.project_id = pp.project_id
             AND p.idu = pp.idu
            WHERE pp.project_id = CAST(:project_id AS uuid)
              AND pp.run_id = CAST(:run_id AS uuid)
              AND p.idu = ANY(:idus)
              AND p.geom_2154 IS NOT NULL
            """
        ),
        {"project_id": project_id, "run_id": run_id, "idus": list(active)},
    ).mappings().all()
    parcelles = []
    found: set[str] = set()
    for r in rows:
        raw = r["geometry"]
        if isinstance(raw, str):
            geometry = json.loads(raw)
        else:
            geometry = raw
        if not geometry:
            continue
        idu = str(r["idu"])
        found.add(idu)
        parcelles.append({"id": idu, "geometry": geometry, "srid": 2154})
    return {
        "parcelles": parcelles,
        "active_idus": len(active),
        "requested_idus": requested_count,
        "skipped_indesirables": skipped,
        "missing_geom": len(active - found),
    }


def _urba_metric(cible: dict, run_errors: list[str]) -> dict:
    couches = []
    for couche in cible.get("couches") or []:
        elements = []
        geom_kind = str(couche.get("geom_kind") or "")
        for el in couche.get("elements") or []:
            mesure = el.get("mesure") or {}
            liens = el.get("liens") or []
            url = next((ln.get("url") for ln in liens if ln.get("url")), None)
            attrs = el.get("attributs") or {}
            item = {
                "element_id": el.get("element_id"),
                "code": el.get("code"),
                "libelle_cnig": el.get("libelle_cnig"),
                "libelle": attrs.get("libelle") or attrs.get("nomsuplitt") or attrs.get("txt"),
                "position": mesure.get("position"),
                "url": url,
                "flags": el.get("flags") or [],
            }
            if geom_kind == "lin":
                item["longueur_m"] = mesure.get("longueur_m")
            elif geom_kind == "pct":
                item["nb_points"] = mesure.get("nb_points")
            else:
                item["pct_cible"] = mesure.get("pct_cible")
                item["surface_m2"] = mesure.get("surface_m2")
            elements.append(item)
        couches.append(
            {
                "layer_key": couche.get("layer_key"),
                "display_name": couche.get("display_name"),
                "famille": couche.get("famille"),
                "geom_kind": geom_kind or None,
                "status": couche.get("status"),
                "error": couche.get("error"),
                "elements": elements,
            }
        )
    synthese = cible.get("synthese") or {}
    score = cible.get("score") if isinstance(cible.get("score"), dict) else None
    score_out = None
    if score:
        sous = score.get("sous_scores") if isinstance(score.get("sous_scores"), dict) else {}
        score_out = {
            "profil": score.get("profil"),
            "version_regles": score.get("version_regles"),
            "score_global": score.get("score_global"),
            "classe": score.get("classe"),
            "libelle_classe": score.get("libelle_classe"),
            "sous_scores": {
                key: {"valeur": val.get("valeur"), "base": val.get("base"), "delta": val.get("delta")}
                for key, val in sous.items()
                if isinstance(val, dict)
            },
            "confiance": score.get("confiance"),
            "vetos": score.get("vetos") or [],
            "resume": score.get("resume") or [],
            "points_a_verifier": [
                p.get("texte") for p in (score.get("points_a_verifier") or []) if isinstance(p, dict) and p.get("texte")
            ],
            "contributions": [
                {
                    "label": c.get("label"),
                    "points": c.get("points"),
                    "justification": c.get("justification"),
                    "fiabilite": c.get("fiabilite"),
                }
                for c in (score.get("contributions") or [])
                if isinstance(c, dict)
            ][:12],
        }
    return {
        "surface_m2": cible.get("surface_m2"),
        "flags": cible.get("flags") or [],
        "zonages": synthese.get("zonages") or [],
        "documents": cible.get("documents") or [],
        "couches": couches,
        "run_errors": run_errors,
        "score": score_out,
    }


def store_urba_profile(conn, project_id: str, run_id: str, response) -> int:
    errors = list(response.meta.errors or [])
    payload = {
        idu: _urba_metric(cible.model_dump(mode="json", exclude_none=True), errors)
        for idu, cible in response.parcelles.items()
    }
    updated = _upsert_profiler_payload(conn, project_id, run_id, URBA_METRIC_KEY, payload)
    composite = CompositeScoreV1Profiler()
    _upsert_profiler_payload(
        conn, project_id, run_id, composite.metric_key, composite.compute_for_run(conn, project_id, run_id)
    )
    return updated


def compute_parcel_score_for_run(conn, project_id: str, run_id: str) -> int:
    """Recalcule uniquement la métrique `score_eco` pour un run (faune_buffer)."""
    pool_service.ensure_tables(conn)
    study_type = resolve_study_type(conn, project_id, run_id)
    profiler_keys = {p.metric_key for p in get_profilers_for_study_type(study_type)}
    if "score_eco" not in profiler_keys:
        logger.info(
            "score_eco ignoré pour study_type=%s (project_id=%s run_id=%s)",
            study_type,
            project_id,
            run_id,
        )
        return 0
    profiler = ScoreEcoProfiler()
    payload_by_idu = profiler.compute_for_run(conn, project_id, run_id)
    count = 0
    for idu, payload in payload_by_idu.items():
        pool_service.upsert_metric(
            conn,
            project_id=project_id,
            run_id=run_id,
            idu=idu,
            metric_key=profiler.metric_key,
            metric_value=payload,
        )
        count += 1
    return count
