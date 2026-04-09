#!/usr/bin/env python3
"""
Audit des recouvrements geometriques intra-parcelle pour la couche
ecocompensation_results.bd_topo_et_cesbio.

Le script lit une liste d'IDU (CSV de test par defaut), calcule:
- sum_class_area_m2: somme des surfaces par classe (apres union intra-classe)
- union_area_m2: union globale des classes sur la parcelle
- overlap_bonus_m2: surplus = sum_class_area_m2 - union_area_m2
- overlap_ratio_pct: part de recouvrement relative a union_area_m2
- pair_overlap_m2: somme des intersections entre paires de classes
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path
from statistics import mean

from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import get_engine  # noqa: E402


BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_POOL_TEST_CSV = BACKEND_DIR / "pool_de_parcelles_de_test.csv"
DEFAULT_HYBRID_TABLE = "ecocompensation_results.bd_topo_et_cesbio"


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
        idus: list[str] = []
        for row in reader:
            val = (row.get(idu_key) or "").strip()
            if val:
                idus.append(val)
        return idus


def main() -> None:
    ap = argparse.ArgumentParser(description="Audit recouvrements geometries couche hybride")
    ap.add_argument("--project-id", required=True, help="UUID projet")
    ap.add_argument(
        "--csv",
        nargs="?",
        const=str(DEFAULT_POOL_TEST_CSV),
        default=str(DEFAULT_POOL_TEST_CSV),
        metavar="FICHIER",
        help=f"CSV des IDU (defaut: {DEFAULT_POOL_TEST_CSV})",
    )
    ap.add_argument(
        "--hybride-table",
        default=DEFAULT_HYBRID_TABLE,
        help=f"Table hybride (defaut: {DEFAULT_HYBRID_TABLE})",
    )
    ap.add_argument("--output", default="", help="Chemin CSV sortie (optionnel)")
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
          SELECT p.idu, p.geom_2154
          FROM ecocompensation_results.parcelles p
          JOIN ids i ON i.idu = p.idu
          WHERE p.project_id = CAST(:pid AS uuid)
        ),
        intersections AS (
          SELECT
            p.idu,
            COALESCE(v.libelle_prio, v.nature, v.libelle, 'inconnu') AS classe,
            ST_Intersection(ST_MakeValid(p.geom_2154), ST_MakeValid(v.geom_2154)) AS inter_geom
          FROM p
          JOIN {args.hybride_table} v
            ON v.project_id = CAST(:pid AS uuid)
           AND p.geom_2154 && v.geom_2154
           AND ST_Intersects(ST_MakeValid(p.geom_2154), ST_MakeValid(v.geom_2154))
        ),
        cleaned AS (
          SELECT idu, classe, inter_geom
          FROM intersections
          WHERE inter_geom IS NOT NULL
            AND NOT ST_IsEmpty(inter_geom)
        ),
        class_geoms AS (
          SELECT
            idu,
            classe,
            ST_UnaryUnion(ST_Collect(inter_geom)) AS class_geom
          FROM cleaned
          GROUP BY idu, classe
        ),
        class_areas AS (
          SELECT
            idu,
            SUM(ST_Area(class_geom)) AS sum_class_area_m2
          FROM class_geoms
          GROUP BY idu
        ),
        union_areas AS (
          SELECT
            idu,
            ST_Area(ST_UnaryUnion(ST_Collect(class_geom))) AS union_area_m2
          FROM class_geoms
          GROUP BY idu
        ),
        pair_overlap AS (
          SELECT
            a.idu,
            SUM(
              ST_Area(
                ST_Intersection(
                  ST_MakeValid(a.class_geom),
                  ST_MakeValid(b.class_geom)
                )
              )
            ) AS pair_overlap_m2
          FROM class_geoms a
          JOIN class_geoms b
            ON a.idu = b.idu
           AND a.classe < b.classe
           AND a.class_geom && b.class_geom
           AND ST_Intersects(ST_MakeValid(a.class_geom), ST_MakeValid(b.class_geom))
          GROUP BY a.idu
        )
        SELECT
          p.idu,
          COALESCE(ca.sum_class_area_m2, 0) AS sum_class_area_m2,
          COALESCE(ua.union_area_m2, 0) AS union_area_m2,
          COALESCE(ca.sum_class_area_m2, 0) - COALESCE(ua.union_area_m2, 0) AS overlap_bonus_m2,
          CASE
            WHEN COALESCE(ua.union_area_m2, 0) > 0
            THEN 100.0 * (COALESCE(ca.sum_class_area_m2, 0) - COALESCE(ua.union_area_m2, 0)) / ua.union_area_m2
            ELSE 0
          END AS overlap_ratio_pct,
          COALESCE(po.pair_overlap_m2, 0) AS pair_overlap_m2
        FROM p
        LEFT JOIN class_areas ca ON ca.idu = p.idu
        LEFT JOIN union_areas ua ON ua.idu = p.idu
        LEFT JOIN pair_overlap po ON po.idu = p.idu
        ORDER BY overlap_bonus_m2 DESC, p.idu
        """
    )

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(sql, {"pid": args.project_id, "idus": idus}).mappings().all()

    if not rows:
        raise SystemExit("Aucune parcelle trouvee pour ce project_id + CSV")

    print(f"project_id: {args.project_id}")
    print(f"source_csv: {args.csv}")
    print(f"table_hybride: {args.hybride_table}")
    print(f"parcelles: {len(rows)}")
    print()

    mean_bonus = mean(float(r["overlap_bonus_m2"]) for r in rows)
    mean_ratio = mean(float(r["overlap_ratio_pct"]) for r in rows)
    n_with_overlap = sum(1 for r in rows if float(r["overlap_bonus_m2"]) > 1e-6)
    print(f"parcelles avec recouvrement: {n_with_overlap}/{len(rows)}")
    print(f"overlap bonus moyen: {mean_bonus:.2f} m2")
    print(f"overlap ratio moyen: {mean_ratio:.2f} %")
    print()

    print("Top 15 parcelles par recouvrement:")
    for r in rows[:15]:
        print(
            f"  {r['idu']} | bonus={float(r['overlap_bonus_m2']):.1f} m2 | "
            f"ratio={float(r['overlap_ratio_pct']):.2f}% | "
            f"sum={float(r['sum_class_area_m2']):.1f} | union={float(r['union_area_m2']):.1f}"
        )

    if args.output:
        outp = Path(args.output).expanduser()
        outp.parent.mkdir(parents=True, exist_ok=True)
        with outp.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(
                [
                    "idu",
                    "sum_class_area_m2",
                    "union_area_m2",
                    "overlap_bonus_m2",
                    "overlap_ratio_pct",
                    "pair_overlap_m2",
                ]
            )
            for r in rows:
                w.writerow(
                    [
                        r["idu"],
                        round(float(r["sum_class_area_m2"]), 3),
                        round(float(r["union_area_m2"]), 3),
                        round(float(r["overlap_bonus_m2"]), 3),
                        round(float(r["overlap_ratio_pct"]), 6),
                        round(float(r["pair_overlap_m2"]), 3),
                    ]
                )
        print(f"\nResultat ecrit: {outp}")


if __name__ == "__main__":
    main()

