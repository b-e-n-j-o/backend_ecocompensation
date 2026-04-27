#!/usr/bin/env python3
import argparse
import csv
import json
from pathlib import Path

CSV_PATH = Path(
    "/Volumes/T7/Travaux_Freelance/KERELIA/CUAs/INTERSECTION_PIPELINE/LATRESNE/cua_latresne_v4/CONFIG/v_commune_2025.csv"
)
DEPARTEMENT = "33"
OUTPUT_DEFAULT = Path(__file__).resolve().parent / "payload_insees_gironde.json"


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


def ecrire_payload_json(insees: list[str], output_path: Path) -> None:
    payload = {"insees": insees}
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Construit un payload JSON d'INSEE Gironde pour "
            "POST /urban-documents/reglement-extractibilite/batch"
        )
    )
    parser.add_argument(
        "--csv-path",
        type=Path,
        default=CSV_PATH,
        help="Chemin du CSV v_commune_2025.csv",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=OUTPUT_DEFAULT,
        help="Chemin du JSON de sortie (payload batch)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limiter le nombre de communes testées (0 = toutes)",
    )
    args = parser.parse_args()

    if not args.csv_path.exists():
        raise FileNotFoundError(f"CSV introuvable: {args.csv_path}")

    communes_csv = lire_communes_csv_gironde(args.csv_path)
    items = sorted(communes_csv.items())
    if args.limit > 0:
        items = items[:args.limit]

    insees = [code_insee for code_insee, _ in items]
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    ecrire_payload_json(insees, args.output_json)

    print(f"Communes Gironde retenues: {len(insees)}")
    print(f"Sortie JSON: {args.output_json.resolve()}")



if __name__ == "__main__":
    main()