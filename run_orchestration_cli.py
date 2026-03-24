#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_orchestration_cli.py
========================

Lance l'orchestration des couches pour un projet donné,
en affichant un log lisible couche par couche dans le terminal.

Usage :
    python run_orchestration_cli.py <project_id>
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from sqlalchemy import text

from db import get_engine
from orchestrator import run_orchestration
from layers.layer_runner import LAYER_REGISTRY, LayerResult


logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


def _get_project(engine, project_id: str) -> dict:
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT * FROM ecocompensation.projects WHERE id = :pid"),
            {"pid": project_id},
        ).mappings().one_or_none()
    if not row:
        raise SystemExit(f"Projet {project_id} introuvable dans ecocompensation.projects")
    return dict(row)


def _layer_label_from_key(key: str) -> str:
    for cfg in LAYER_REGISTRY:
        if cfg["key"] == key:
            return cfg["label"]
    return key


async def _push_cli(data: dict) -> None:
    event = data.get("event")
    layer_key = data.get("layer_key")
    message = data.get("message", "")

    if event == "start":
        logger.info(message)
        return

    if event == "complete":
        logger.info(message)
        return

    if layer_key:
        label = _layer_label_from_key(layer_key)
        prefix = f"[{layer_key} | {label}]"
    else:
        prefix = "[global]"

    if event in {"running", "progress"}:
        logger.info("%s %s", prefix, message)
    elif event == "done":
        logger.info("%s ✅ %s", prefix, message)
    elif event == "skipped":
        logger.info("%s ⏭ %s", prefix, message)
    elif event == "error":
        logger.error("%s ❌ %s", prefix, message)
    else:
        logger.info("%s %s", prefix, message)


async def _run(project_id: str) -> dict[str, LayerResult]:
    engine = get_engine()
    proj = _get_project(engine, project_id)
    aoi_id = proj.get("aoi_id")
    if not aoi_id:
        raise SystemExit(f"Le projet {project_id} n'a pas d'aoi_id associé.")

    logger.info("Lancement de l'orchestration pour le projet %s (aoi_id=%s)", project_id, aoi_id)
    results = await run_orchestration(engine, project_id, str(aoi_id), _push_cli)

    logger.info("Résumé par couche :")
    for key, res in results.items():
        label = _layer_label_from_key(key)
        if res.error:
            status = "ERREUR"
        elif res.skipped:
            status = "IGNORÉE"
        else:
            status = "OK"
        logger.info(
            " - %-24s | %-30s | status=%-7s | n_inserted=%6d | duration=%.1fs | error=%s",
            key,
            label,
            status,
            res.n_inserted,
            res.duration_s,
            res.error or "",
        )

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Lancer l'orchestration des couches pour un projet.")
    parser.add_argument("project_id", help="UUID du projet (ecocompensation.projects.id)")
    args = parser.parse_args()

    asyncio.run(_run(args.project_id))


if __name__ == "__main__":
    main()

