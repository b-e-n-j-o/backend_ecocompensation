#!/usr/bin/env python3
"""Dit si une parcelle, ou un bloc de parcelles contiguës, passerait un filtre.

La géométrie testée est l'union des parcelles cadastrales (références idu).
Chaque critère du JSON est évalué seul, dans l'ordre du pipeline. Le premier
échec est celui qui écarterait la géométrie. Les suivants sont aussi indiqués.

Depuis backend/ :

  python scripts/tester_filtre_parcelle.py \\
    --project-id <uuid> \\
    --filtre filtre.json \\
    --idu 33063000AB0123 --idu 33063000AB0124

Le JSON est celui envoyé au filtre (min_area_ha, cesbio_libelles, fauna_criteria, …)
ou le filter_config enregistré sur un run.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import text

from api.recherche_de_foncier.filtrage.layers.national_exclusions import DEFAULT_EXCLUDED_LAYERS
from api.recherche_de_foncier.filtrage.layers.filter_pipeline import (
    FaunaCriterion,
    FilterConfig,
    FloraCriterion,
    build_filter_clauses,
    filter_params,
    _has_source_geometry,
)
from db import get_engine


def config_from_dict(raw: dict) -> FilterConfig:
    fauna: list[FaunaCriterion] = []
    for fc in raw.get("fauna_criteria") or []:
        if not isinstance(fc, dict):
            continue
        name = str(fc.get("species") or fc.get("tax_nom_val") or "").strip()
        if not name:
            continue
        fauna.append(
            FaunaCriterion(
                species=name,
                dist_m=float(fc.get("dist_m", fc.get("radius_m", 0)) or 0),
                aliases=[str(a).strip() for a in (fc.get("aliases") or []) if str(a).strip()],
            )
        )
    flora: list[FloraCriterion] = []
    for fc in raw.get("flora_criteria") or []:
        if not isinstance(fc, dict):
            continue
        key = str(fc.get("taxon_key") or "").strip()
        if not key:
            continue
        flora.append(
            FloraCriterion(
                taxon_key=key,
                label=str(fc.get("label") or key),
                dist_m=float(fc.get("dist_m") or 0),
            )
        )
    excluded = raw.get("excluded_layers")
    if excluded is None:
        excluded_layers = list(DEFAULT_EXCLUDED_LAYERS)
    else:
        excluded_layers = [str(x).strip() for x in excluded if str(x).strip()]
    miller = raw.get("miller_thresh", raw.get("miller_threshold"))
    return FilterConfig(
        min_area_ha=float(raw.get("min_area_ha") or 0),
        miller_thresh=None if miller is None else float(miller),
        cesbio_libelles=[str(x).strip() for x in (raw.get("cesbio_libelles") or []) if str(x).strip()],
        fauna_criteria=fauna,
        fauna_combine=str(raw.get("fauna_combine") or "or"),
        fauna_joins=list(raw.get("fauna_joins") or []),
        flora_criteria=flora,
        flora_combine=str(raw.get("flora_combine") or "or"),
        flora_joins=list(raw.get("flora_joins") or []),
        bio_combine=str(raw.get("bio_combine") or "or"),
        search_mode=str(raw.get("search_mode") or "exclude_source_buffer"),
        zone_humide_mode=str(raw.get("zone_humide_mode") or "ignore"),
        zones_humides_probables_mode=str(raw.get("zones_humides_probables_mode") or "ignore"),
        min_zone_humide_ha=float(raw.get("min_zone_humide_ha") or 0),
        excluded_layers=excluded_layers,
        troncons_hydros_max_dist_m=raw.get("troncons_hydros_max_dist_m"),
        surfaces_hydros_max_dist_m=raw.get("surfaces_hydros_max_dist_m"),
        same_bassin_versant=bool(raw.get("same_bassin_versant")),
        arrachage_vignes=bool(raw.get("arrachage_vignes")),
        arrachage_vignes_types=[str(x).strip() for x in (raw.get("arrachage_vignes_types") or []) if str(x).strip()],
    )


def load_geometry(conn, idus: list[str]) -> tuple[str, float, list[str]]:
    rows = conn.execute(
        text("""
            SELECT idu
            FROM ecocompensation.parcelles
            WHERE idu = ANY(CAST(:idus AS text[]))
              AND geom_2154 IS NOT NULL
        """),
        {"idus": idus},
    ).scalars().all()
    found = {str(i) for i in rows}
    missing = [i for i in idus if i not in found]
    if missing:
        raise SystemExit(f"Parcelles absentes du cadastre : {', '.join(missing)}")
    wkt, area_m2 = conn.execute(
        text("""
            SELECT
                ST_AsText(ST_UnaryUnion(ST_Collect(geom_2154))),
                ST_Area(ST_UnaryUnion(ST_Collect(geom_2154)))
            FROM ecocompensation.parcelles
            WHERE idu = ANY(CAST(:idus AS text[]))
        """),
        {"idus": idus},
    ).one()
    return str(wkt), float(area_m2 or 0), missing


def clause_holds(conn, clause: str, params: dict) -> bool:
    row = conn.execute(
        text(f"""
            SELECT ({clause}) AS ok
            FROM (
                SELECT
                    ST_SetSRID(ST_GeomFromText(:wkt), 2154) AS geom_2154,
                    CAST(:project_id AS uuid) AS project_id
            ) p
        """),
        params,
    ).one()
    return bool(row.ok)


def main() -> None:
    ap = argparse.ArgumentParser(description="Teste un filtre sur une ou plusieurs parcelles.")
    ap.add_argument("--project-id", required=True, help="Projet dont l'AOI et les couches clippées servent au croisement")
    ap.add_argument("--filtre", required=True, help="Fichier JSON du filtre")
    ap.add_argument("--idu", action="append", required=True, help="Référence parcellaire, répétable")
    ap.add_argument("--json", action="store_true", help="Sortie JSON")
    args = ap.parse_args()

    idus = list(dict.fromkeys(i.strip() for i in args.idu if i.strip()))
    raw = json.loads(Path(args.filtre).read_text())
    if isinstance(raw.get("filter_config"), dict):
        raw = raw["filter_config"]
    if isinstance(raw.get("options"), dict):
        raw = {**raw["options"], **{k: v for k, v in raw.items() if k != "options"}}
    if not raw.get("fauna_criteria") and raw.get("faune_criteria"):
        raw["fauna_criteria"] = raw["faune_criteria"]
    veg = raw.get("vegetation_hybride")
    if not raw.get("cesbio_libelles") and isinstance(veg, dict):
        raw["cesbio_libelles"] = veg.get("cesbio_libelles") or []
    config = config_from_dict(raw)

    engine = get_engine()
    with engine.connect() as conn:
        wkt, area_m2, _ = load_geometry(conn, idus)
        surface_ha = area_m2 / 10_000
        has_source = _has_source_geometry(engine, args.project_id)
        if config.search_mode == "within_foncier" and has_source:
            spatial_mode = "within_foncier"
        elif has_source:
            spatial_mode = "exclude_source"
        else:
            spatial_mode = None

        keys, labels, clauses = build_filter_clauses(config, spatial_mode=spatial_mode)
        params = filter_params(args.project_id, config)
        params["wkt"] = wkt

        steps = []
        first_fail = None
        for key, label, clause in zip(keys, labels, clauses):
            if key == "min_area_ha":
                ok = surface_ha + 1e-9 >= float(config.min_area_ha)
                detail = f"{surface_ha:.2f} ha (seuil {config.min_area_ha:g} ha)"
            else:
                ok = clause_holds(conn, clause, params)
                detail = None
            steps.append({"key": key, "label": label, "ok": ok, "detail": detail})
            if not ok and first_fail is None:
                first_fail = steps[-1]

    passed = first_fail is None
    report = {
        "idus": idus,
        "nb_parcelles": len(idus),
        "surface_ha": round(surface_ha, 4),
        "passe": passed,
        "premier_echec": None if passed else {"key": first_fail["key"], "label": first_fail["label"]},
        "criteres": steps,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    print(f"{len(idus)} parcelle(s) — {surface_ha:.2f} ha")
    for step in steps:
        mark = "oui" if step["ok"] else "non"
        extra = f" — {step['detail']}" if step.get("detail") else ""
        print(f"  [{mark}] {step['label']}{extra}")
    if passed:
        print("La géométrie passe le filtre.")
    else:
        print(f"La géométrie ne passe pas le filtre : {first_fail['label']}.")


if __name__ == "__main__":
    main()
