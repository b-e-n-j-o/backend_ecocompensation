"""Smoke SQL : Postgres doit accepter chaque requête, même si elle ne touche aucune ligne.

    TEST_PG_URL=postgresql+psycopg://postgres:test@127.0.0.1:54329/postgres \\
        pytest tests/test_sql_smoke.py -m smoke -v

    python tests/test_sql_smoke.py --url postgresql+psycopg://postgres:test@127.0.0.1:54329/postgres --only enrich-fauna -v
"""
from __future__ import annotations

import argparse
import ast
import logging
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

log = logging.getLogger("sql_smoke")

PID = uuid.UUID("11111111-1111-1111-1111-111111111111")
BIND = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")

# Rempli après un passage réel. strict=True : un correctif qui fait passer le test
# doit retirer l'entrée, pas la laisser verte par accident.
XFAIL: dict[str, str] = {}

SCAN_FILES = [
    "api/recherche_de_foncier/filtrage/layers/filter_pipeline.py",
    "api/recherche_de_foncier/filtrage/layers/enrich_uf.py",
    "api/recherche_de_foncier/filtrage/layers/flora_filter.py",
    "api/recherche_de_foncier/filtrage/layers/rpg/rpg_ilots/enrich.py",
    "api/recherche_de_foncier/filtrage/layers/rpg/rpg_ilots/build.py",
    "api/recherche_de_foncier/filtrage/layers/rpg/rpg_ilots/pool.py",
    "api/recherche_de_foncier/filtrage/layers/rpg/rpg_ilots/api.py",
    "api/recherche_de_foncier/routers/fauna_router.py",
    "api/recherche_de_foncier/routers/foncier_router.py",
    "api/recherche_de_foncier/routers/hydro_preview.py",
    "api/analyse-foncier/service.py",
    "api/reserve_fonciere/ajout_de_foncier/faune/faune_import.py",
    "api/reserve_fonciere/ajout_de_foncier/flore/flore_import.py",
    "api/recherche_de_foncier/enrichir/pool/pool_service.py",
    "main.py",
    "filter_orchestrator.py",
    "orchestrator.py",
]


@dataclass
class SmokeCase:
    id: str
    sql: str
    params: dict
    origin: str
    empty_array: bool = False


def _literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                return None
        return "".join(parts)
    return None


def _static_sql() -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    for rel in SCAN_FILES:
        path = BACKEND / rel
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name != "text" or not node.args:
                continue
            sql = _literal(node.args[0])
            if not sql or len(sql.strip()) < 12:
                continue
            found.append((f"{rel}:{node.lineno}", rel, sql))
    return found


def _is_array_param(name: str) -> bool:
    n = name.lower()
    return any(
        token in n
        for token in (
            "idus", "names", "batch", "labels", "deps", "aliases", "libelles",
            "types", "cesbio", "keys", "vegetation",
        )
    )


def _value(name: str, *, empty: bool) -> object:
    n = name.lower()
    if _is_array_param(n):
        return [] if empty else ["Crapaud calamite (Le)"]
    if n in {"pid", "project_id", "aid", "aoi_id"} or n.endswith("_id"):
        return PID
    if n in {"lim", "limit", "keep"} or n.endswith("_count"):
        return 10
    if any(token in n for token in ("dist", "buf", "miller", "area", "ha", "m2")):
        return 100.0
    if "wkt" in n or n in {"geom", "geometry"}:
        return "POINT(400000 6400000)"
    if n in {"key", "val", "status", "s"}:
        return "ready"
    return "Crapaud calamite (Le)"


def _params(sql: str, *, empty: bool) -> dict:
    names = []
    for name in BIND.findall(sql):
        if name not in names:
            names.append(name)
    return {name: _value(name, empty=empty) for name in names}


def _filter_cases() -> list[SmokeCase]:
    from api.recherche_de_foncier.filtrage.layers.filter_pipeline import (
        FaunaCriterion,
        FilterConfig,
        FloraCriterion,
        _build_filter_clauses,
        _filter_params,
    )

    specs: list[tuple[str, FilterConfig, str | None]] = []
    for layer in ("geomce", "preemption_ens", "ens", "natura_2000", "znieff_1", "znieff_2", "ebc"):
        specs.append((f"filter-excl-{layer}", FilterConfig(excluded_layers=[layer], miller_thresh=0.3), None))
    for mode in ("ignore", "intersect", "exclude"):
        specs.append((
            f"filter-zh-{mode}",
            FilterConfig(zone_humide_mode=mode, min_zone_humide_ha=0.5, excluded_layers=[], miller_thresh=0.3),
            None,
        ))
        specs.append((
            f"filter-zhp-{mode}",
            FilterConfig(zones_humides_probables_mode=mode, excluded_layers=[], miller_thresh=0.3),
            None,
        ))
    for dist, label in ((None, "none"), (0, "intersect"), (250, "buffer")):
        specs.append((
            f"filter-hydro-{label}",
            FilterConfig(
                troncons_hydros_max_dist_m=dist,
                surfaces_hydros_max_dist_m=dist,
                excluded_layers=[],
                miller_thresh=0.3,
            ),
            None,
        ))
    specs.append(("filter-cesbio", FilterConfig(cesbio_libelles=["Prairies"], excluded_layers=[], miller_thresh=0.3), None))
    specs.append(("filter-cesbio-vide", FilterConfig(cesbio_libelles=[], excluded_layers=[], miller_thresh=0.3), None))
    specs.append((
        "filter-faune-or",
        FilterConfig(
            fauna_criteria=[FaunaCriterion("Crapaud calamite (Le)", 500, ["Crapaud"])],
            fauna_combine="or",
            excluded_layers=[],
        ),
        None,
    ))
    specs.append((
        "filter-faune-and",
        FilterConfig(
            fauna_criteria=[
                FaunaCriterion("Crapaud calamite (Le)", 500, []),
                FaunaCriterion("Loutre d'Europe (La)", 1000, ["Loutre"]),
            ],
            fauna_joins=["and"],
            excluded_layers=[],
        ),
        None,
    ))
    specs.append((
        "filter-flore-and",
        FilterConfig(
            flora_criteria=[FloraCriterion("nom:robinia", "Robinier", 200)],
            flora_combine="and",
            excluded_layers=[],
        ),
        None,
    ))
    specs.append((
        "filter-bio-or",
        FilterConfig(
            fauna_criteria=[FaunaCriterion("Crapaud calamite (Le)", 500)],
            flora_criteria=[FloraCriterion("nom:robinia", "Robinier", 200)],
            bio_combine="or",
            excluded_layers=[],
        ),
        None,
    ))
    specs.append((
        "filter-vignes-types",
        FilterConfig(arrachage_vignes=True, arrachage_vignes_types=["arrachage"], excluded_layers=[]),
        None,
    ))
    specs.append((
        "filter-vignes-tous",
        FilterConfig(arrachage_vignes=True, arrachage_vignes_types=[], excluded_layers=[]),
        None,
    ))
    specs.append(("filter-within", FilterConfig(excluded_layers=[], miller_thresh=0.3), "within_foncier"))
    specs.append(("filter-hors-emprise", FilterConfig(excluded_layers=[], miller_thresh=0.3), "exclude_source"))
    specs.append((
        "filter-bassin",
        FilterConfig(same_bassin_versant=True, excluded_layers=[], miller_thresh=0.3),
        None,
    ))

    cases = []
    for name, cfg, spatial in specs:
        _keys, _labels, clauses = _build_filter_clauses(cfg, spatial_mode=spatial)
        where = " AND ".join(f"({clause})" for clause in clauses)
        sql = f"SELECT COUNT(*) FROM ecocompensation_results.parcelles p WHERE {where}"
        params = _filter_params(str(PID), cfg)
        cases.append(SmokeCase(name, sql, params, "filter_pipeline.py:_build_filter_clauses"))
        if any(_is_array_param(key) for key in params):
            empty = dict(params)
            for key in empty:
                if _is_array_param(key):
                    empty[key] = []
            cases.append(SmokeCase(name + "-any-vide", sql, empty, cases[-1].origin, empty_array=True))
    return cases


def collect_cases() -> list[SmokeCase]:
    cases: list[SmokeCase] = []
    seen: set[str] = set()
    for origin, _rel, sql in _static_sql():
        base = origin.replace("/", "-").replace(".", "-").replace(":", "-")
        if base in seen:
            base = f"{base}-{len(seen)}"
        seen.add(base)
        cases.append(SmokeCase(base, sql, _params(sql, empty=False), origin))
        if re.search(r"ANY\(:[A-Za-z_]", sql):
            cases.append(SmokeCase(base + "-any-vide", sql, _params(sql, empty=True), origin, empty_array=True))
    cases.extend(_filter_cases())
    return cases


CASES = collect_cases()


def run_case(conn: Connection, case: SmokeCase) -> None:
    started = time.perf_counter()
    log.info("SQL %s\n%s\nparams=%s", case.id, case.sql.strip(), case.params)
    conn.execute(text(case.sql), case.params)
    log.info("ok %s %.3fs", case.id, time.perf_counter() - started)


def _marks(case: SmokeCase):
    marks = [pytest.mark.db, pytest.mark.smoke]
    if case.id in XFAIL:
        marks.append(pytest.mark.xfail(strict=True, reason=XFAIL[case.id]))
    return marks


@pytest.mark.parametrize(
    "case",
    [pytest.param(case, id=case.id, marks=_marks(case)) for case in CASES],
)
def test_sql_accepte(db, case: SmokeCase):
    try:
        run_case(db, case)
    except Exception as exc:
        if case.id in XFAIL:
            pytest.xfail(f"{type(exc).__name__}: {exc}")
        raise


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--only", default="")
    parser.add_argument("-v", action="store_true")
    args = parser.parse_args()
    if args.v:
        logging.getLogger().setLevel(logging.DEBUG)
    from tests.db.schema_loader import load_schema
    from sqlalchemy import create_engine

    target = load_schema(args.url)
    engine = create_engine(target)
    assert engine.dialect.driver == "psycopg"
    selected = [case for case in CASES if not args.only or args.only in case.id]
    failed = 0
    for case in selected:
        conn = engine.connect()
        trans = conn.begin()
        try:
            run_case(conn, case)
            print(f"OK  {case.id}")
        except Exception as exc:
            failed += 1
            print(f"ERR {case.id} {type(exc).__name__}: {exc}")
        finally:
            trans.rollback()
            conn.close()
    engine.dispose()
    print(f"{len(selected) - failed}/{len(selected)} acceptées")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
