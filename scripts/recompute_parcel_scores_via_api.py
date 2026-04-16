#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Script de dev pour recalculer les scores parcelle d'un run via l'API backend,
puis afficher uniquement `parcel_score_v1` avec détail.

Exemples:
  python recompute_parcel_scores_via_api.py \
    --project-id 50f9cba1-6526-40f1-90ce-3f7719cbf3c6 \
    --run-id 017e3e72-cc7f-4bd5-ab7b-ad014f06db43

  python recompute_parcel_scores_via_api.py \
    --project-id 50f9cba1-6526-40f1-90ce-3f7719cbf3c6 \
    --run-id 017e3e72-cc7f-4bd5-ab7b-ad014f06db43 \
    --idu 33213000AP0032

  # Sans argument : utilise les UUID par défaut définis dans main().
  python recompute_parcel_scores_via_api.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from typing import Any

import requests


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def call_recompute(base_url: str, project_id: str, run_id: str, timeout_s: int, score_only: bool) -> dict[str, Any]:
    endpoint = "recompute-score" if score_only else "recompute-metrics"
    url = f"{base_url}/api/projects/{project_id}/pool/runs/{run_id}/{endpoint}"
    log(f"POST {url}")
    t0 = time.perf_counter()
    r = requests.post(url, timeout=timeout_s)
    dt = time.perf_counter() - t0
    log(f"-> HTTP {r.status_code} en {dt:.2f}s")
    r.raise_for_status()
    return r.json() if r.content else {}


def fetch_bulk_metrics(base_url: str, project_id: str, run_id: str, timeout_s: int) -> dict[str, Any]:
    url = f"{base_url}/api/projects/{project_id}/pool/metrics"
    params = {"run_id": run_id}
    log(f"GET {url}?run_id={run_id}")
    t0 = time.perf_counter()
    r = requests.get(url, params=params, timeout=timeout_s)
    dt = time.perf_counter() - t0
    log(f"-> HTTP {r.status_code} en {dt:.2f}s")
    r.raise_for_status()
    return r.json()


def extract_scores(by_idu: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for idu, rows in by_idu.items():
        for row in rows:
            if row.get("metric_key") == "parcel_score_v1":
                val = row.get("metric_value_jsonb")
                if isinstance(val, dict):
                    out[idu] = val
                break
    return out


def print_scores(scores: dict[str, dict[str, Any]], focus_idu: str | None) -> None:
    if focus_idu:
        log(f"Filtre IDU ciblé: {focus_idu}")
        score = scores.get(focus_idu)
        if score is None:
            log("Aucun metric_key=parcel_score_v1 trouvé pour cet IDU.")
            return
        print(json.dumps({focus_idu: score}, ensure_ascii=False, indent=2))
        return

    if not scores:
        log("Aucun score parcel_score_v1 trouvé dans les métriques du run.")
        return

    log(f"{len(scores)} parcelles avec parcel_score_v1")
    # Aperçu top 20
    ranking = sorted(
        scores.items(),
        key=lambda kv: float((kv[1].get("total_score") or 0)),
        reverse=True,
    )
    for i, (idu, score) in enumerate(ranking[:20], start=1):
        total = score.get("total_score")
        max_score = score.get("max_score")
        print(f"{i:>2}. {idu} -> {total}/{max_score}")

    print("\n--- Détail JSON (complet) ---")
    print(json.dumps(scores, ensure_ascii=False, indent=2))


def main() -> int:
    # Défauts dev (remplace ici si besoin)
    _project_id = "50f9cba1-6526-40f1-90ce-3f7719cbf3c6"
    _run_id = "017e3e72-cc7f-4bd5-ab7b-ad014f06db43"

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--project-id",
        default=_project_id,
        help=f"UUID projet (défaut dev: {_project_id})",
    )
    parser.add_argument(
        "--run-id",
        default=_run_id,
        help=f"UUID run parcelles_pool_runs (défaut dev: {_run_id})",
    )
    parser.add_argument("--idu", help="IDU spécifique à afficher")
    parser.add_argument("--base-url", default="http://localhost:8000", help="Base URL API backend")
    parser.add_argument("--timeout-s", type=int, default=300, help="Timeout HTTP en secondes")
    parser.add_argument(
        "--full-metrics",
        action="store_true",
        help="Appelle le recompute global des métriques (sinon score-only par défaut).",
    )
    parser.add_argument(
        "--skip-recompute",
        action="store_true",
        help="N'appelle pas le recompute, lit juste les métriques déjà en base.",
    )
    args = parser.parse_args()

    try:
        if not args.skip_recompute:
            recompute_resp = call_recompute(
                args.base_url,
                args.project_id,
                args.run_id,
                args.timeout_s,
                score_only=not args.full_metrics,
            )
            log(f"Recompute response: {json.dumps(recompute_resp, ensure_ascii=False)}")
        else:
            log("Recompute ignoré (--skip-recompute).")

        bulk = fetch_bulk_metrics(args.base_url, args.project_id, args.run_id, args.timeout_s)
        by_idu = bulk.get("by_idu", {})
        if not isinstance(by_idu, dict):
            log("Réponse inattendue: 'by_idu' absent ou invalide.")
            return 2

        scores = extract_scores(by_idu)
        print_scores(scores, args.idu)
        return 0

    except requests.HTTPError as e:
        body = ""
        if e.response is not None:
            try:
                body = e.response.text[:2000]
            except Exception:
                body = "<body indisponible>"
        log(f"HTTPError: {e}\n{body}")
        return 1
    except Exception as e:
        log(f"Erreur: {e!r}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

