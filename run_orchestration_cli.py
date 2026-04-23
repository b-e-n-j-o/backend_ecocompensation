#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_orchestration_cli.py
========================

Lance l'orchestration des couches pour un projet donné,
en affichant un log lisible couche par couche dans le terminal.

Usages :
    python run_orchestration_cli.py <project_id>
    python run_orchestration_cli.py <project_id> --layers geomce
    python run_orchestration_cli.py <project_id> --layers geomce,voies_ferrees
    python run_orchestration_cli.py <project_id> --dry-run
    python run_orchestration_cli.py <project_id> --layers geomce --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time

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


def _get_aoi_diameter_km(engine, aoi_id: str) -> float | None:
    with engine.begin() as conn:
        diameter_m = conn.execute(
            text(
                """
                SELECT ST_MaxDistance(geom_2154, geom_2154) AS diameter_m
                FROM ecocompensation.aoi
                WHERE id = :aid
                """
            ),
            {"aid": aoi_id},
        ).scalar_one_or_none()
    if diameter_m is None:
        return None
    return float(diameter_m) / 1000.0


def _layer_label_from_key(key: str) -> str:
    for cfg in LAYER_REGISTRY:
        if cfg["key"] == key:
            return cfg["label"]
    return key


def _parse_layer_keys(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    return keys or None


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


async def _run(
    project_id: str,
    layer_keys: list[str] | None = None,
    dry_run: bool = False,
    uf_max_parcelles: int | None = None,
) -> dict[str, LayerResult]:
    engine = get_engine()
    proj = _get_project(engine, project_id)
    aoi_id = proj.get("aoi_id")
    if not aoi_id:
        raise SystemExit(f"Le projet {project_id} n'a pas d'aoi_id associé.")

    if layer_keys is not None:
        selected_keys = layer_keys
    else:
        selected_keys = [cfg["key"] for cfg in LAYER_REGISTRY]

    diameter_km = _get_aoi_diameter_km(engine, str(aoi_id))

    logger.info(
        "Lancement pour projet %s (aoi_id=%s) | dry_run=%s | layers=%s",
        project_id,
        aoi_id,
        dry_run,
        ",".join(selected_keys),
    )
    if diameter_km is not None:
        logger.info("AOI diamètre géométrique: %.2f km", diameter_km)
    else:
        logger.info("AOI diamètre géométrique: indisponible")

    results = await run_orchestration(
        engine,
        project_id,
        str(aoi_id),
        _push_cli,
        layer_keys=layer_keys,
        dry_run=dry_run,
        uf_max_parcelles=uf_max_parcelles,
    )

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
    parser.add_argument(
        "--layers",
        help="Liste de clés de couches séparées par des virgules (ex: geomce,voies_ferrees).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Exécute sans conserver les données (nettoyage après chaque couche).",
    )
    parser.add_argument(
        "--uf-max-parcelles",
        type=int,
        default=None,
        help="Cap optionnel du nombre de parcelles par UF pour les sous-ensembles (par défaut: aucun cap forcé par l'orchestrateur).",
    )
    args = parser.parse_args()

    asyncio.run(
        _run(
            args.project_id,
            layer_keys=_parse_layer_keys(args.layers),
            dry_run=args.dry_run,
            uf_max_parcelles=args.uf_max_parcelles,
        )
    )


if __name__ == "__main__":
    main()
