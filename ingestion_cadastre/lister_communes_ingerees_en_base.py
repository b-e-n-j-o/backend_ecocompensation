#!/usr/bin/env python3
import argparse
import csv
import json
import os
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv


load_dotenv()

CSV_DEFAULT = "/Volumes/T7/Travaux_Freelance/KERELIA/CUAs/INTERSECTION_PIPELINE/LATRESNE/cua_latresne_v4/CONFIG/v_commune_2025.csv"


def load_communes_csv(csv_path: str) -> dict[str, str]:
    """
    Retourne un mapping {code_insee -> nom_commune} depuis v_commune_2025.csv.
    On privilégie NCCENR, fallback sur NCC.
    """
    mapping: dict[str, str] = {}
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            insee = (row.get("COM") or "").strip()
            if not insee:
                continue
            nom = (row.get("NCCENR") or row.get("NCC") or "").strip()
            if not nom:
                continue
            mapping[insee] = nom
    return mapping


def upsert_communes_table(csv_path: str, output_json: str | None = None) -> None:
    dsn = os.getenv("SUPABASE_DIRECT_URL")
    if not dsn:
        raise RuntimeError("Variable d'env SUPABASE_DIRECT_URL manquante")

    insee_to_nom = load_communes_csv(csv_path)

    sql_counts = """
        SELECT
            code_dep,
            code_insee,
            COUNT(*)::bigint AS nb_parcelles
        FROM parcelles.parcelles
        WHERE code_insee IS NOT NULL
        GROUP BY code_dep, code_insee
        ORDER BY code_dep, code_insee
    """

    with psycopg2.connect(dsn, sslmode="require") as conn:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE SCHEMA IF NOT EXISTS parcelles;
                CREATE TABLE IF NOT EXISTS parcelles.communes (
                    code_insee   text PRIMARY KEY,
                    code_dep     text NOT NULL,
                    nom_commune  text,
                    nb_parcelles bigint NOT NULL,
                    maj_le       timestamptz NOT NULL DEFAULT now()
                );
                """
            )

            cur.execute(sql_counts)
            rows = cur.fetchall()

            payload = [
                (
                    code_insee,
                    code_dep,
                    insee_to_nom.get(code_insee),
                    int(nb_parcelles),
                )
                for code_dep, code_insee, nb_parcelles in rows
            ]

            if payload:
                execute_values(
                    cur,
                    """
                    INSERT INTO parcelles.communes
                        (code_insee, code_dep, nom_commune, nb_parcelles)
                    VALUES %s
                    ON CONFLICT (code_insee) DO UPDATE SET
                        code_dep = EXCLUDED.code_dep,
                        nom_commune = EXCLUDED.nom_commune,
                        nb_parcelles = EXCLUDED.nb_parcelles,
                        maj_le = now()
                    """,
                    payload,
                    page_size=1000,
                )

            conn.commit()

    print(f"OK: {len(rows)} communes upsertées dans parcelles.communes")

    if output_json:
        data = [
            {
                "code_dep": code_dep,
                "code_insee": code_insee,
                "nom_commune": insee_to_nom.get(code_insee),
                "nb_parcelles": int(nb_parcelles),
            }
            for code_dep, code_insee, nb_parcelles in rows
        ]
        out = Path(output_json)
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"OK: export JSON dans {out.resolve()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Construit/actualise parcelles.communes depuis parcelles.parcelles + v_commune_2025.csv"
    )
    parser.add_argument("--csv-path", default=CSV_DEFAULT, help="Chemin du CSV v_commune_2025.csv")
    parser.add_argument(
        "--output-json",
        default=None,
        help="Chemin de sortie JSON optionnel (ex: communes_parcelles.json)",
    )
    args = parser.parse_args()

    upsert_communes_table(csv_path=args.csv_path, output_json=args.output_json)