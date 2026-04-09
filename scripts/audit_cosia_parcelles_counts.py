#!/usr/bin/env python3
"""
Audit : pour une liste d’IDU, compte combien d’entités `geo.cosia` intersectent
chaque géométrie issue de la table parcelles résultats (par défaut
`ecocompensation_results.parcelles`), avec filtre département sur `dpt`.

Prérequis : variables d’environnement comme pour l’API (voir `backend/.env`).

Sources d’IDU (au choix) :
  --csv           Export classement (séparateur ; ou ,, colonne idu). Sans chemin :
                  `pool_de_parcelles_de_test.csv` à la racine du dossier backend.
  --idus / --idus-file

Exemples :
  cd backend && python3 scripts/audit_cosia_parcelles_counts.py \\
    --project-id <UUID_PROJET> --csv

  python3 scripts/audit_cosia_parcelles_counts.py \\
    --project-id <UUID_PROJET> \\
    --csv pool_de_parcelles_de_test.csv

  python3 scripts/audit_cosia_parcelles_counts.py --idus-file ids.txt --dpt 33

Optionnel : comparaison avec une étape « bbox des parcelles » (compte COSIA dans
ST_Extent) pour estimer le gain d’un pré-filtre spatial.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path
from statistics import mean

from sqlalchemy import text

# Permet d’importer db depuis le dossier backend
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import get_engine  # noqa: E402


DEFAULT_PARCELLES_TABLE = "ecocompensation_results.parcelles"
DEFAULT_COSIA_TABLE = "geo.cosia"

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_POOL_TEST_CSV = BACKEND_DIR / "pool_de_parcelles_de_test.csv"


def parse_idus_from_csv(path: str) -> list[str]:
    """CSV export classement : en-tête avec colonne `idu`, séparateur `;` ou `,`."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise SystemExit(f"Fichier CSV introuvable : {p}")
    with p.open(encoding="utf-8-sig", newline="") as f:
        first = f.readline()
        delimiter = ";" if first.count(";") >= first.count(",") else ","
        f.seek(0)
        reader = csv.DictReader(f, delimiter=delimiter)
        if not reader.fieldnames:
            raise SystemExit("CSV sans en-tête")
        idu_key = next(
            (h for h in reader.fieldnames if h and h.strip().lower() == "idu"),
            None,
        )
        if not idu_key:
            raise SystemExit(f"Colonne « idu » introuvable. Colonnes : {list(reader.fieldnames)}")
        out = []
        for row in reader:
            v = (row.get(idu_key) or "").strip()
            if v:
                out.append(v)
        return out


def parse_idus(raw: str | None, path: str | None) -> list[str]:
    if path:
        with open(path, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
        return lines
    if raw:
        return [x.strip() for x in raw.split(",") if x.strip()]
    raise SystemExit("Fournir --idus, --idus-file ou --csv")


def main() -> None:
    ap = argparse.ArgumentParser(description="Audit intersections COSIA × parcelles (par IDU)")
    ap.add_argument("--project-id", type=str, help="Filtre parcelles sur project_id (recommandé)")
    ap.add_argument("--idus", type=str, help="Liste d’IDU séparés par des virgules")
    ap.add_argument("--idus-file", type=str, help="Fichier texte : un IDU par ligne")
    ap.add_argument(
        "--csv",
        nargs="?",
        const=str(DEFAULT_POOL_TEST_CSV),
        default=None,
        metavar="FICHIER",
        help=(
            "CSV type export classement (colonnes ; ou ,, colonne idu). "
            f"Sans chemin : {DEFAULT_POOL_TEST_CSV.name} dans backend/"
        ),
    )
    ap.add_argument("--dpt", type=str, default="33", help="Filtre geo.cosia.dpt (défaut : 33)")
    ap.add_argument(
        "--parcelles-table",
        type=str,
        default=os.getenv("AUDIT_PARCELLES_TABLE", DEFAULT_PARCELLES_TABLE),
        help=f"Table parcelles (défaut : {DEFAULT_PARCELLES_TABLE})",
    )
    ap.add_argument(
        "--cosia-table",
        type=str,
        default=os.getenv("AUDIT_COSIA_TABLE", DEFAULT_COSIA_TABLE),
        help=f"Table COSIA (défaut : {DEFAULT_COSIA_TABLE})",
    )
    ap.add_argument("--bbox-stats", action="store_true", help="Affiche aussi COSIA(dpt) dans la bbox des parcelles")
    args = ap.parse_args()

    csv_path_used: str | None = None
    if args.csv is not None:
        csv_path_used = args.csv
        idus = parse_idus_from_csv(args.csv)
    elif args.idus or args.idus_file:
        idus = parse_idus(args.idus, args.idus_file)
    else:
        csv_path_used = str(DEFAULT_POOL_TEST_CSV)
        idus = parse_idus_from_csv(csv_path_used)

    if not idus:
        raise SystemExit("Aucun IDU")

    engine = get_engine()
    ptable = args.parcelles_table
    ctable = args.cosia_table
    dpt = args.dpt

    # LEFT JOIN : parcelles sans intersection COSIA → 0 (INNER JOIN les ferait disparaître)
    sql_parcelles = f"""
        SELECT p.idu, COUNT(c.id)::bigint AS n_cosia
        FROM {ptable} p
        LEFT JOIN {ctable} c
          ON c.dpt = :dpt
         AND p.geom_2154 && c.geom_2154
         AND ST_Intersects(ST_MakeValid(p.geom_2154), ST_MakeValid(c.geom_2154))
        WHERE p.idu = ANY(CAST(:idus AS text[]))
    """
    params: dict = {"dpt": dpt, "idus": idus}
    if args.project_id:
        sql_parcelles += " AND p.project_id = CAST(:pid AS uuid)"
        params["pid"] = args.project_id
    sql_parcelles += " GROUP BY p.idu ORDER BY p.idu"

    print(f"Table parcelles : {ptable}")
    print(f"COSIA           : {ctable}  (dpt = {dpt})")
    print(f"Nombre d’IDU    : {len(idus)}")
    if csv_path_used:
        print(f"Source CSV      : {csv_path_used}")
    if args.project_id:
        print(f"project_id      : {args.project_id}")
    print()

    t0 = time.perf_counter()
    with engine.connect() as conn:
        rows = conn.execute(text(sql_parcelles), params).mappings().all()
    elapsed = time.perf_counter() - t0

    by_idu = {str(r["idu"]): int(r["n_cosia"]) for r in rows}
    missing_parcel = [i for i in idus if i not in by_idu]

    for idu in idus:
        n = by_idu.get(idu, 0)
        flag = " (pas de ligne dans la table parcelles pour ce filtre)" if idu in missing_parcel else ""
        print(f"  {idu}  →  {n} entités COSIA{flag}")

    counts = [by_idu[i] for i in idus if i in by_idu]
    if counts:
        print()
        print(f"Temps requête   : {elapsed:.2f} s")
        print(f"Min / max       : {min(counts)} / {max(counts)}")
        print(f"Moyenne         : {mean(counts):.1f}")
    if missing_parcel:
        print()
        print(
            "IDU sans ligne dans la table parcelles (ou filtre project_id) :",
            ", ".join(missing_parcel),
        )

    if args.bbox_stats and idus:
        sql_bbox = f"""
            WITH parcels AS (
                SELECT ST_Union(ST_MakeValid(geom_2154)) AS g
                FROM {ptable}
                WHERE idu = ANY(CAST(:idus AS text[]))
                {"AND project_id = CAST(:pid AS uuid)" if args.project_id else ""}
            ),
            env AS (
                SELECT ST_Envelope(g) AS bbox FROM parcels WHERE g IS NOT NULL
            )
            SELECT COUNT(*)::bigint AS n_cosia_in_bbox
            FROM {ctable} c, env
            WHERE c.dpt = :dpt
              AND c.geom_2154 && env.bbox
              AND ST_Intersects(c.geom_2154, env.bbox)
        """
        params_b = {"dpt": dpt, "idus": idus}
        if args.project_id:
            params_b["pid"] = args.project_id
        t1 = time.perf_counter()
        with engine.connect() as conn:
            n_bbox = conn.execute(text(sql_bbox), params_b).scalar_one()
        print()
        print(f"[bbox] Entités COSIA (dpt={dpt}) intersectant la bbox des parcelles : {n_bbox}")
        print(f"[bbox] Temps : {time.perf_counter() - t1:.2f} s")


if __name__ == "__main__":
    main()
