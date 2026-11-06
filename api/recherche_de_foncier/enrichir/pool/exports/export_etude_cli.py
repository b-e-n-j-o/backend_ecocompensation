#!/usr/bin/env python3
"""
export_etude_cli.py — génère le GeoPackage d'étude d'un projet en local, avec logs.

    cd backend
    python -m api.recherche_de_foncier.enrichir.pool.exports.export_etude_cli \
        --project-id 3c127e45-b3de-4b1d-bfde-d86379062df9 --out etude.gpkg
    … --layers uf                 # UF seules (unites_foncieres, uf_parcelles, dictionnaire)
    … --force-metrics             # recalcule les métriques des parcelles d'UF
    … --check                     # relit le GPKG et affiche couches / nb d'objets / colonnes vides

Même contenu que GET /api/projects/{id}/export/etude.gpkg (sans run_id : derniers résultats).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

log = logging.getLogger("export_etude")


def _json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return None
    return v


def _load_parcelles(engine, project_id: str, proj: dict):
    """Parcelles du dernier classement (hors indésirables) + options + métriques pool."""
    from api.recherche_de_foncier.enrichir.pool import pool_service
    from filtre_options import FiltreOptions

    last = _json(proj.get("last_results")) or {}
    parcelles = last.get("parcelles") or []
    pool_run_id = last.get("pool_run_id")
    metrics = None
    with engine.begin() as conn:
        pool_service.ensure_tables(conn)
        if parcelles:
            parcelles = pool_service.filter_parcelles_excluding_project_indesirables(conn, project_id, parcelles)
        if pool_run_id and parcelles:
            metrics = pool_service.get_all_metrics_grouped_by_idu(conn, project_id, str(pool_run_id))
    return parcelles, FiltreOptions.from_dict(_json(proj.get("last_filter")) or {}), metrics


def check_gpkg(path: Path) -> None:
    import pyogrio

    for name, gtype in pyogrio.list_layers(path):
        df = pyogrio.read_dataframe(path, layer=name, read_geometry=False)
        empty = [c for c in df.columns if df[c].isna().all() or (df[c].astype(str).str.strip() == "").all()]
        log.info("  %-17s %-8s %5d objets, %3d colonnes%s", name, gtype or "table", len(df), len(df.columns),
                 f" — vides : {', '.join(empty)}" if empty else "")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project-id", required=True)
    ap.add_argument("--out", type=Path, default=None, help="Chemin du .gpkg (défaut : etude_<id8>.gpkg)")
    ap.add_argument("--layers", default="parcelles,uf,ilots", help="parcelles, uf, ilots (séparés par des virgules)")
    ap.add_argument("--force-metrics", action="store_true", help="Recalcule les métriques des parcelles d'UF")
    ap.add_argument("--check", action="store_true", help="Relit le GPKG produit et résume son contenu")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", datefmt="%H:%M:%S")

    from sqlalchemy import text

    from db import get_engine
    from api.recherche_de_foncier.enrichir.pool.exports import export_uf_layers
    from api.recherche_de_foncier.enrichir.pool.exports.export_etude_gpkg import write_etude_geopackage

    t0 = time.perf_counter()
    wanted = {x.strip().lower() for x in a.layers.split(",") if x.strip()}
    engine = get_engine()
    with engine.connect() as conn:
        proj = conn.execute(text("SELECT * FROM ecocompensation.projects WHERE id = CAST(:p AS uuid)"),
                            {"p": a.project_id}).mappings().one_or_none()
    if not proj:
        log.error("Projet %s introuvable", a.project_id)
        return 1
    proj = dict(proj)
    log.info("Projet « %s » — couches demandées : %s", proj.get("name"), ", ".join(sorted(wanted)))

    parcelles, options, metrics = _load_parcelles(engine, a.project_id, proj) if "parcelles" in wanted else ([], None, None)
    results_uf = _json(proj.get("last_results_uf"))
    if "uf" in wanted and a.force_metrics and results_uf:
        export_uf_layers.build_uf_export(engine, a.project_id, results_uf, force_metrics=True,
                                         log=lambda m: log.info(m))   # recalcul ; l'écriture réutilisera ce run

    out = a.out or Path(f"etude_{a.project_id[:8]}.gpkg")
    written = write_etude_geopackage(
        engine, a.project_id, out,
        include_parcelles="parcelles" in wanted, include_uf="uf" in wanted, include_ilots="ilots" in wanted,
        parcelles=parcelles, parcelle_options=options, aoi_id=str(proj.get("aoi_id") or ""),
        metrics_by_idu=metrics, results_uf=results_uf if isinstance(results_uf, dict) else None,
    )
    log.info("GeoPackage écrit : %s (%s) en %.1fs", out, ", ".join(written), time.perf_counter() - t0)
    if a.check:
        check_gpkg(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
