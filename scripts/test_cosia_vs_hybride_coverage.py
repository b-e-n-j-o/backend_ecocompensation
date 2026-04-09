#!/usr/bin/env python3
"""
Test de couverture spatiale (COSIA vs BD_TOPO/CESBIO) sur un échantillon d'IDU.

Objectif:
- comparer la surface d'intersection "sum" (potentiellement avec double comptage)
- comparer la surface d'intersection "union" (sans double comptage intra-couche)

Par défaut, lit les IDU depuis `pool_de_parcelles_de_test.csv` (colonne `idu`).
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path
from statistics import mean

from sqlalchemy import text

# Permet d'importer db.py depuis le dossier backend
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import get_engine  # noqa: E402


BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_POOL_TEST_CSV = BACKEND_DIR / "pool_de_parcelles_de_test.csv"


def parse_idus_from_csv(path: str) -> list[str]:
    p = Path(path).expanduser()
    if not p.is_file():
        raise SystemExit(f"Fichier CSV introuvable: {p}")
    with p.open(encoding="utf-8-sig", newline="") as f:
        first = f.readline()
        delimiter = ";" if first.count(";") >= first.count(",") else ","
        f.seek(0)
        reader = csv.DictReader(f, delimiter=delimiter)
        if not reader.fieldnames:
            raise SystemExit("CSV sans en-tete")
        idu_key = next((h for h in reader.fieldnames if h and h.strip().lower() == "idu"), None)
        if not idu_key:
            raise SystemExit(f"Colonne 'idu' introuvable. Colonnes: {list(reader.fieldnames)}")
        out = []
        for row in reader:
            val = (row.get(idu_key) or "").strip()
            if val:
                out.append(val)
        return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Test couverture COSIA vs couche hybride par parcelle")
    ap.add_argument("--project-id", required=True, help="UUID projet")
    ap.add_argument(
        "--csv",
        nargs="?",
        const=str(DEFAULT_POOL_TEST_CSV),
        default=str(DEFAULT_POOL_TEST_CSV),
        metavar="FICHIER",
        help=f"CSV des IDU (defaut: {DEFAULT_POOL_TEST_CSV})",
    )
    ap.add_argument("--cosia-table", default="geo.cosia", help="Table COSIA")
    ap.add_argument(
        "--hybride-table",
        default="ecocompensation_results.bd_topo_et_cesbio",
        help="Table hybride",
    )
    ap.add_argument("--output", default="", help="Chemin sortie CSV resultat (optionnel)")
    args = ap.parse_args()

    idus = parse_idus_from_csv(args.csv)
    if not idus:
        raise SystemExit("Aucun IDU dans le CSV")

    sql = text(
        f"""
        WITH ids AS (
          SELECT unnest(CAST(:idus AS text[])) AS idu
        ),
        p AS (
          SELECT p.idu, p.geom_2154, p.code_insee,
                 ST_Area(ST_MakeValid(p.geom_2154)) AS parcel_m2
          FROM ecocompensation_results.parcelles p
          JOIN ids i ON i.idu = p.idu
          WHERE p.project_id = CAST(:pid AS uuid)
        ),
        cosia_raw AS (
          SELECT p.idu, c.geom_2154
          FROM p
          JOIN {args.cosia_table} c
            ON c.geom_2154 && p.geom_2154
           AND ST_Intersects(ST_MakeValid(p.geom_2154), ST_MakeValid(c.geom_2154))
           AND p.code_insee IS NOT NULL
           AND LENGTH(TRIM(p.code_insee)) >= 2
           AND c.dpt = LEFT(TRIM(p.code_insee), 2)
        ),
        hyb_raw AS (
          SELECT p.idu, v.geom_2154
          FROM p
          JOIN {args.hybride_table} v
            ON v.project_id = CAST(:pid AS uuid)
           AND v.geom_2154 && p.geom_2154
           AND ST_Intersects(ST_MakeValid(p.geom_2154), ST_MakeValid(v.geom_2154))
        ),
        cosia_sum AS (
          SELECT p.idu, SUM(ST_Area(ST_Intersection(ST_MakeValid(p.geom_2154), ST_MakeValid(c.geom_2154)))) AS cosia_sum_m2
          FROM p LEFT JOIN cosia_raw c ON c.idu = p.idu
          GROUP BY p.idu
        ),
        hyb_sum AS (
          SELECT p.idu, SUM(ST_Area(ST_Intersection(ST_MakeValid(p.geom_2154), ST_MakeValid(h.geom_2154)))) AS hyb_sum_m2
          FROM p LEFT JOIN hyb_raw h ON h.idu = p.idu
          GROUP BY p.idu
        ),
        cosia_union AS (
          SELECT p.idu,
                 ST_Area(
                   ST_Intersection(
                     ST_MakeValid(p.geom_2154),
                     COALESCE(ST_UnaryUnion(ST_Collect(c.geom_2154)), ST_GeomFromText('POLYGON EMPTY', 2154))
                   )
                 ) AS cosia_union_m2
          FROM p LEFT JOIN cosia_raw c ON c.idu = p.idu
          GROUP BY p.idu, p.geom_2154
        ),
        hyb_union AS (
          SELECT p.idu,
                 ST_Area(
                   ST_Intersection(
                     ST_MakeValid(p.geom_2154),
                     COALESCE(ST_UnaryUnion(ST_Collect(h.geom_2154)), ST_GeomFromText('POLYGON EMPTY', 2154))
                   )
                 ) AS hyb_union_m2
          FROM p LEFT JOIN hyb_raw h ON h.idu = p.idu
          GROUP BY p.idu, p.geom_2154
        )
        SELECT p.idu,
               p.parcel_m2,
               COALESCE(cu.cosia_union_m2, 0) AS cosia_union_m2,
               COALESCE(cs.cosia_sum_m2, 0) AS cosia_sum_m2,
               COALESCE(hu.hyb_union_m2, 0) AS hyb_union_m2,
               COALESCE(hs.hyb_sum_m2, 0) AS hyb_sum_m2,
               (COALESCE(cs.cosia_sum_m2, 0) - COALESCE(cu.cosia_union_m2, 0)) AS cosia_overlap_bonus_m2,
               (COALESCE(hs.hyb_sum_m2, 0) - COALESCE(hu.hyb_union_m2, 0)) AS hyb_overlap_bonus_m2
        FROM p
        LEFT JOIN cosia_union cu ON cu.idu = p.idu
        LEFT JOIN cosia_sum cs ON cs.idu = p.idu
        LEFT JOIN hyb_union hu ON hu.idu = p.idu
        LEFT JOIN hyb_sum hs ON hs.idu = p.idu
        ORDER BY p.idu
        """
    )

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(sql, {"pid": args.project_id, "idus": idus}).mappings().all()

    if not rows:
        raise SystemExit("Aucune parcelle trouvee pour ce project_id + CSV")

    print(f"project_id: {args.project_id}")
    print(f"source_csv: {args.csv}")
    print(f"parcelles: {len(rows)}")

    avg_cosia_cov = mean((float(r["cosia_union_m2"]) / float(r["parcel_m2"])) if float(r["parcel_m2"]) > 0 else 0.0 for r in rows)
    avg_hyb_cov = mean((float(r["hyb_union_m2"]) / float(r["parcel_m2"])) if float(r["parcel_m2"]) > 0 else 0.0 for r in rows)
    avg_cosia_overlap = mean(float(r["cosia_overlap_bonus_m2"]) for r in rows)
    avg_hyb_overlap = mean(float(r["hyb_overlap_bonus_m2"]) for r in rows)

    print(f"moyenne couverture union COSIA  : {avg_cosia_cov:.4f}")
    print(f"moyenne couverture union HYBRID : {avg_hyb_cov:.4f}")
    print(f"bonus overlap moyen COSIA (sum-union): {avg_cosia_overlap:.2f} m2")
    print(f"bonus overlap moyen HYBRID(sum-union): {avg_hyb_overlap:.2f} m2")
    print()

    print("Top 10 ecarts (hyb_union - cosia_union) :")
    top = sorted(rows, key=lambda r: float(r["hyb_union_m2"]) - float(r["cosia_union_m2"]), reverse=True)[:10]
    for r in top:
        delta = float(r["hyb_union_m2"]) - float(r["cosia_union_m2"])
        print(
            f"  {r['idu']} | parcel={float(r['parcel_m2']):.1f} | "
            f"cosia_union={float(r['cosia_union_m2']):.1f} | "
            f"hyb_union={float(r['hyb_union_m2']):.1f} | delta={delta:.1f}"
        )

    if args.output:
        outp = Path(args.output).expanduser()
        outp.parent.mkdir(parents=True, exist_ok=True)
        with outp.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(
                [
                    "idu",
                    "parcel_m2",
                    "cosia_union_m2",
                    "cosia_sum_m2",
                    "hyb_union_m2",
                    "hyb_sum_m2",
                    "cosia_overlap_bonus_m2",
                    "hyb_overlap_bonus_m2",
                ]
            )
            for r in rows:
                w.writerow(
                    [
                        r["idu"],
                        round(float(r["parcel_m2"]), 3),
                        round(float(r["cosia_union_m2"]), 3),
                        round(float(r["cosia_sum_m2"]), 3),
                        round(float(r["hyb_union_m2"]), 3),
                        round(float(r["hyb_sum_m2"]), 3),
                        round(float(r["cosia_overlap_bonus_m2"]), 3),
                        round(float(r["hyb_overlap_bonus_m2"]), 3),
                    ]
                )
        print(f"\nResultat ecrit: {outp}")


if __name__ == "__main__":
    main()

