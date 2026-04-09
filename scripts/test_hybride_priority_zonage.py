#!/usr/bin/env python3
"""
Teste un zonage "hybride priorise" a la volee:
- source='bdtopo' prioritaire
- source='cesbio' conserve uniquement hors emprise bdtopo

Sorties:
- resume par parcelle (surface, total zonage, part couverte)
- detail par classe (m2 + ratio), avec somme des ratios ~100%
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

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
        out: list[str] = []
        for row in reader:
            val = (row.get(idu_key) or "").strip()
            if val:
                out.append(val)
        return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Test zonage hybride avec priorite bdtopo > cesbio")
    ap.add_argument("--project-id", required=True, help="UUID projet")
    ap.add_argument(
        "--csv",
        nargs="?",
        const=str(DEFAULT_POOL_TEST_CSV),
        default=str(DEFAULT_POOL_TEST_CSV),
        metavar="FICHIER",
        help=f"CSV des IDU (defaut: {DEFAULT_POOL_TEST_CSV})",
    )
    ap.add_argument("--hybride-table", default=DEFAULT_HYBRID_TABLE, help="Table hybride source")
    ap.add_argument("--out-summary", default="", help="CSV resume parcelle (optionnel)")
    ap.add_argument("--out-details", default="", help="CSV detail classes (optionnel)")
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
          SELECT p.idu, ST_MakeValid(p.geom_2154) AS parcel_geom
          FROM ecocompensation_results.parcelles p
          JOIN ids i ON i.idu = p.idu
          WHERE p.project_id = CAST(:pid AS uuid)
        ),
        inter_raw AS (
          SELECT
            p.idu,
            COALESCE(v.source, 'inconnu') AS source,
            COALESCE(v.libelle_prio, v.nature, v.libelle, 'inconnu') AS classe,
            ST_Intersection(p.parcel_geom, ST_MakeValid(v.geom_2154)) AS inter_geom
          FROM p
          JOIN {args.hybride_table} v
            ON v.project_id = CAST(:pid AS uuid)
           AND v.geom_2154 && p.parcel_geom
           AND ST_Intersects(p.parcel_geom, ST_MakeValid(v.geom_2154))
        ),
        inter_clean AS (
          SELECT idu, source, classe, inter_geom
          FROM inter_raw
          WHERE inter_geom IS NOT NULL
            AND NOT ST_IsEmpty(inter_geom)
        ),
        bd_union AS (
          SELECT
            idu,
            ST_UnaryUnion(ST_Collect(inter_geom)) AS g_bd
          FROM inter_clean
          WHERE source = 'bdtopo'
          GROUP BY idu
        ),
        prioritized AS (
          -- BD TOPO garde toute son emprise
          SELECT idu, classe, inter_geom
          FROM inter_clean
          WHERE source = 'bdtopo'

          UNION ALL

          -- CESBIO uniquement hors emprise BD TOPO
          SELECT
            c.idu,
            c.classe,
            ST_Difference(
              c.inter_geom,
              COALESCE(b.g_bd, ST_GeomFromText('POLYGON EMPTY', 2154))
            ) AS inter_geom
          FROM inter_clean c
          LEFT JOIN bd_union b ON b.idu = c.idu
          WHERE c.source = 'cesbio'
        ),
        prioritized_clean AS (
          SELECT idu, classe, inter_geom
          FROM prioritized
          WHERE inter_geom IS NOT NULL
            AND NOT ST_IsEmpty(inter_geom)
        ),
        class_area AS (
          SELECT
            idu,
            classe,
            ST_Area(ST_UnaryUnion(ST_Collect(inter_geom))) AS area_m2
          FROM prioritized_clean
          GROUP BY idu, classe
        ),
        totals AS (
          SELECT idu, SUM(area_m2) AS total_m2
          FROM class_area
          GROUP BY idu
        ),
        parcel_stats AS (
          SELECT p.idu, ST_Area(p.parcel_geom) AS parcel_m2
          FROM p
        )
        SELECT
          ca.idu,
          ps.parcel_m2,
          ca.classe,
          ca.area_m2,
          t.total_m2,
          CASE WHEN t.total_m2 > 0 THEN ca.area_m2 / t.total_m2 ELSE 0 END AS ratio
        FROM class_area ca
        JOIN totals t ON t.idu = ca.idu
        JOIN parcel_stats ps ON ps.idu = ca.idu
        ORDER BY ca.idu, ca.area_m2 DESC
        """
    )

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(sql, {"pid": args.project_id, "idus": idus}).mappings().all()

    if not rows:
        raise SystemExit("Aucun resultat (project_id/IDU/table)")

    print(f"project_id: {args.project_id}")
    print(f"source_csv: {args.csv}")
    print(f"table_hybride: {args.hybride_table}")
    print(f"parcelles trouvees: {len(set(str(r['idu']) for r in rows))}")
    print()

    by_idu: dict[str, dict] = {}
    for r in rows:
        idu = str(r["idu"])
        by_idu.setdefault(
            idu,
            {
                "parcel_m2": float(r["parcel_m2"]),
                "total_m2": float(r["total_m2"]),
                "classes": [],
            },
        )
        by_idu[idu]["classes"].append(
            {
                "classe": str(r["classe"]),
                "area_m2": float(r["area_m2"]),
                "ratio": float(r["ratio"]),
            }
        )

    print("Apercu (10 premieres parcelles):")
    for idu in sorted(by_idu.keys())[:10]:
        rec = by_idu[idu]
        total = rec["total_m2"]
        parcel = rec["parcel_m2"]
        cov = (100.0 * total / parcel) if parcel > 0 else 0.0
        top = rec["classes"][:3]
        top_txt = ", ".join(f"{c['classe']} {c['ratio'] * 100:.1f}%" for c in top)
        print(f"  {idu} | total={total:.1f} m2 | couverture={cov:.1f}% | top: {top_txt}")

    if args.out_summary:
        out = Path(args.out_summary).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["idu", "parcel_m2", "total_prioritized_m2", "coverage_pct"])
            for idu in sorted(by_idu.keys()):
                rec = by_idu[idu]
                parcel = rec["parcel_m2"]
                total = rec["total_m2"]
                coverage = (100.0 * total / parcel) if parcel > 0 else 0.0
                w.writerow([idu, round(parcel, 3), round(total, 3), round(coverage, 6)])
        print(f"\nResume ecrit: {out}")

    if args.out_details:
        out = Path(args.out_details).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["idu", "classe", "area_m2", "ratio"])
            for idu in sorted(by_idu.keys()):
                for c in by_idu[idu]["classes"]:
                    w.writerow([idu, c["classe"], round(c["area_m2"], 3), round(c["ratio"], 8)])
        print(f"Details ecrits: {out}")


if __name__ == "__main__":
    main()

