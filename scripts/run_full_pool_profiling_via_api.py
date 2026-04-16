#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Recalcule toutes les métriques pool (profilers inclus : COSIA, PM, score agrégé, etc.)
via POST .../recompute-metrics, puis affiche un résumé et les payloads PM.

Sans --project-id / --run-id : choisit automatiquement le **dernier run** (created_at max)
parmi tous les projets avec total_count > 0 (GET /api/projects + .../pool/runs).

Exemples:
  # Dernier pool connu (API), puis recompute-metrics
  python scripts/run_full_pool_profiling_via_api.py

  # Forcer un projet + run
  python scripts/run_full_pool_profiling_via_api.py \\
    --project-id <uuid> --run-id <uuid>

  # Backend distant + timeout long (profiling peut dépasser 5 min)
  python scripts/run_full_pool_profiling_via_api.py \\
    --base-url https://api.example.com --timeout-s 1200
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from typing import Any

import requests

METRIC_PM = "parcelles_personnes_morales"


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _parse_created_at(raw: Any) -> datetime:
    if raw is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if isinstance(raw, datetime):
        dt = raw
    else:
        s = str(raw).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def resolve_latest_pool_run(base_url: str, timeout_s: int) -> tuple[str, str, dict[str, Any]]:
    """
    Parcourt GET /api/projects puis GET .../pool/runs pour chaque projet,
    et retourne le couple (project_id, run_id) du run avec total_count > 0
    et created_at le plus récent.
    """
    base = base_url.rstrip("/")
    discover_timeout = min(120, max(15, int(timeout_s)))
    url_list = f"{base}/api/projects"
    log(f"GET {url_list} (résolution dernier pool)")
    r = requests.get(url_list, timeout=discover_timeout)
    r.raise_for_status()
    projects = r.json()
    if not isinstance(projects, list):
        raise RuntimeError("Réponse /api/projects : attendu une liste")

    best: tuple[datetime, str, str, dict[str, Any]] | None = None

    for p in projects:
        pid = p.get("id")
        if not pid:
            continue
        url_runs = f"{base}/api/projects/{pid}/pool/runs"
        try:
            rr = requests.get(url_runs, timeout=discover_timeout)
            rr.raise_for_status()
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                continue
            raise
        data = rr.json()
        runs = data.get("runs")
        if not isinstance(runs, list):
            continue
        for run in runs:
            if not isinstance(run, dict):
                continue
            try:
                tc = int(run.get("total_count") or 0)
            except (TypeError, ValueError):
                continue
            if tc <= 0:
                continue
            rid = run.get("id")
            if not rid:
                continue
            ts = _parse_created_at(run.get("created_at"))
            cand = (ts, str(pid), str(rid), run)
            if best is None or cand[0] > best[0]:
                best = cand

    if best is None:
        raise RuntimeError(
            "Aucun run parcelles_pool_runs avec total_count > 0 trouvé "
            "(vérifie qu’un filtre parcelles a bien produit un pool)."
        )
    _, project_id, run_id, run = best
    log(
        f"Dernier pool retenu: project_id={project_id} run_id={run_id} "
        f"total_count={run.get('total_count')} created_at={run.get('created_at')}"
    )
    return project_id, run_id, run


def post_recompute_all_metrics(
    base_url: str, project_id: str, run_id: str, timeout_s: int
) -> dict[str, Any]:
    """POST recompute-metrics sans score_only → tous les profilers + score final."""
    url = f"{base_url.rstrip('/')}/api/projects/{project_id}/pool/runs/{run_id}/recompute-metrics"
    log(f"POST {url}")
    t0 = time.perf_counter()
    r = requests.post(url, timeout=timeout_s)
    dt = time.perf_counter() - t0
    log(f"-> HTTP {r.status_code} en {dt:.1f}s")
    r.raise_for_status()
    return r.json() if r.content else {}


def fetch_bulk_metrics(
    base_url: str, project_id: str, run_id: str, timeout_s: int
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/projects/{project_id}/pool/metrics"
    params = {"run_id": run_id}
    log(f"GET {url}?run_id={run_id}")
    t0 = time.perf_counter()
    r = requests.get(url, params=params, timeout=timeout_s)
    dt = time.perf_counter() - t0
    log(f"-> HTTP {r.status_code} en {dt:.2f}s")
    r.raise_for_status()
    return r.json()


def index_metrics_keys(by_idu: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _idu, rows in by_idu.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            mk = row.get("metric_key")
            if isinstance(mk, str):
                counts[mk] = counts.get(mk, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: kv[0]))


def extract_metric_by_idu(
    by_idu: dict[str, Any], metric_key: str
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for idu, rows in by_idu.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if row.get("metric_key") == metric_key:
                val = row.get("metric_value_jsonb")
                if isinstance(val, dict):
                    out[idu] = val
                break
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Lance le profiling pool complet (toutes les métriques) et résume parcelles_personnes_morales."
    )
    parser.add_argument(
        "--project-id",
        default=None,
        help="UUID projet (avec --run-id ; sinon résolution auto du dernier pool)",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="UUID parcelles_pool_runs (avec --project-id ; sinon résolution auto)",
    )
    parser.add_argument("--base-url", default="http://localhost:8000", help="Base URL API backend")
    parser.add_argument(
        "--timeout-s",
        type=int,
        default=1200,
        help="Timeout HTTP (secondes). Le profiling complet peut être long.",
    )
    parser.add_argument(
        "--skip-recompute",
        action="store_true",
        help="Ne pas POST recompute-metrics : uniquement GET métriques actuelles.",
    )
    parser.add_argument(
        "--idu",
        help="Afficher le JSON complet des métriques (toutes clés) pour un IDU après le résumé.",
    )
    parser.add_argument(
        "--pm-samples",
        type=int,
        default=8,
        help="Nombre max d’exemples JSON pour la métrique parcelles_personnes_morales (défaut: 8).",
    )
    args = parser.parse_args()

    try:
        if args.project_id and args.run_id:
            project_id = str(args.project_id).strip()
            run_id = str(args.run_id).strip()
        elif not args.project_id and not args.run_id:
            project_id, run_id, _ = resolve_latest_pool_run(args.base_url, args.timeout_s)
        else:
            log("Indiquez les deux --project-id et --run-id, ou aucun des deux pour le dernier pool.")
            return 2

        if not args.skip_recompute:
            resp = post_recompute_all_metrics(
                args.base_url, project_id, run_id, args.timeout_s
            )
            log(f"Réponse recompute: {json.dumps(resp, ensure_ascii=False)}")
        else:
            log("Recompute ignoré (--skip-recompute).")

        bulk = fetch_bulk_metrics(
            args.base_url, project_id, run_id, min(120, args.timeout_s)
        )
        by_idu = bulk.get("by_idu", {})
        if not isinstance(by_idu, dict):
            log("Réponse invalide: 'by_idu' absent ou pas un objet.")
            return 2

        total_p = bulk.get("total_parcelles", len(by_idu))
        log(f"Parcelles avec au moins une métrique: {total_p}")

        counts = index_metrics_keys(by_idu)
        print("\n--- Métriques présentes (occurrences par clé) ---")
        for mk, n in counts.items():
            print(f"  {mk}: {n}")

        pm = extract_metric_by_idu(by_idu, METRIC_PM)
        hits = [i for i, v in pm.items() if v.get("intersects_pm_database") is True]
        print(f"\n--- {METRIC_PM} ---")
        print(f"  Parcelles avec entrée métrique: {len(pm)}")
        print(f"  Avec intersects_pm_database=True: {len(hits)}")

        n_sample = max(0, args.pm_samples)
        shown = 0
        for idu in sorted(pm.keys()):
            if shown >= n_sample:
                break
            print(f"\n  [{idu}]")
            print(json.dumps(pm[idu], ensure_ascii=False, indent=4))
            shown += 1
        if len(pm) > n_sample:
            print(f"\n  ... ({len(pm) - n_sample} autres non affichés, --pm-samples pour plus)")

        if args.idu:
            print(f"\n--- Détail toutes métriques pour IDU {args.idu!r} ---")
            rows = by_idu.get(args.idu)
            if not isinstance(rows, list):
                log("IDU inconnu ou pas de lignes.")
            else:
                by_key = {r.get("metric_key"): r.get("metric_value_jsonb") for r in rows}
                print(json.dumps(by_key, ensure_ascii=False, indent=2))

        return 0

    except requests.HTTPError as e:
        body = ""
        if e.response is not None:
            try:
                body = e.response.text[:4000]
            except Exception:
                body = "<body indisponible>"
        log(f"HTTPError: {e}\n{body}")
        return 1
    except Exception as e:
        log(f"Erreur: {e!r}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
