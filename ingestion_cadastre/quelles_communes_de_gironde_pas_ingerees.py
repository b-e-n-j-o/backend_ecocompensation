#!/usr/bin/env python3
import csv
import os
from pathlib import Path

from dotenv import load_dotenv
import psycopg

CSV_PATH = Path(
    "/Volumes/T7/Travaux_Freelance/KERELIA/CUAs/INTERSECTION_PIPELINE/LATRESNE/cua_latresne_v4/CONFIG/v_commune_2025.csv"
)
DEPARTEMENT = "33"


def lire_communes_csv_gironde(csv_path: Path) -> dict[str, str]:
    """
    Retourne un dict {code_insee: libelle} pour les communes (TYPECOM=COM) du DEP=33.
    """
    communes = {}
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("DEP") == DEPARTEMENT and row.get("TYPECOM") == "COM":
                code_insee = (row.get("COM") or "").strip()
                libelle = (row.get("LIBELLE") or "").strip()
                if code_insee:
                    communes[code_insee] = libelle
    return communes


def lire_communes_db_gironde(conn) -> dict[str, str]:
    """
    Retourne un dict {code_insee: nom_commune} depuis parcelles.communes pour code_dep=33.
    """
    query = """
        SELECT code_insee, nom_commune
        FROM parcelles.communes
        WHERE code_dep = %s
    """
    with conn.cursor() as cur:
        cur.execute(query, (DEPARTEMENT,))
        rows = cur.fetchall()
    return {code_insee: (nom_commune or "") for code_insee, nom_commune in rows}


def main() -> None:
    load_dotenv()

    db_url = os.getenv("SUPABASE_DIRECT_URL")
    if not db_url:
        raise RuntimeError(
            "SUPABASE_DIRECT_URL introuvable. Ajoute-le dans ton .env puis relance."
        )

    if not CSV_PATH.exists():
        raise FileNotFoundError(f"CSV introuvable: {CSV_PATH}")

    communes_csv = lire_communes_csv_gironde(CSV_PATH)

    with psycopg.connect(db_url) as conn:
        communes_db = lire_communes_db_gironde(conn)

    codes_csv = set(communes_csv.keys())
    codes_db = set(communes_db.keys())

    manquantes_en_db = sorted(codes_csv - codes_db)
    en_db_mais_pas_csv = sorted(codes_db - codes_csv)

    print(f"Communes CSV Gironde (DEP=33): {len(codes_csv)}")
    print(f"Communes DB Gironde (code_dep=33): {len(codes_db)}")
    print()

    print(f"=== Communes manquantes en base ({len(manquantes_en_db)}) ===")
    for code in manquantes_en_db:
        print(f"{code} - {communes_csv.get(code, '')}")

    print()
    print(f"=== Communes en base mais absentes du CSV ({len(en_db_mais_pas_csv)}) ===")
    for code in en_db_mais_pas_csv:
        print(f"{code} - {communes_db.get(code, '')}")


if __name__ == "__main__":
    main()