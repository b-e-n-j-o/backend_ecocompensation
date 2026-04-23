"""
Script de test rapide pour la logique metier subdivision_fiscale.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from tabulate import tabulate

HERE = Path(__file__).resolve().parents[2]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from core.parcelle import ParcelleRef, fetch_parcelles
from core.subdivision_fiscale.subdivision import compute_subdivision_result
from core.unites_foncieres import build_uf


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--insee", default="33234")
    parser.add_argument("--section", default="AM")
    parser.add_argument("--numero", default="0728")
    parser.add_argument("--commune", default="Latresne")
    args = parser.parse_args()

    refs = [
        ParcelleRef(
            section=args.section,
            numero=args.numero,
            insee=args.insee,
            commune=args.commune,
        )
    ]
    parcelles = fetch_parcelles(refs)
    ok = [p for p in parcelles if p.ok]
    if not ok:
        print("Parcelle introuvable.")
        return

    uf_gdf = build_uf(parcelles)
    res = compute_subdivision_result(uf_gdf, ok)

    print("\n--- RESULTAT SUBDIVISION FISCALE ---")
    print(f"Statut : {'SUBDIVISEE' if res['subdivisee'] else 'NON SUBDIVISEE'}")
    print(f"Nombre d'entites : {res['nb_entites']}")

    if res["rows"]:
        rows = [
            [
                r["idu_parcel"],
                r["lettre"],
                f"{r['surface_calc_m2']:.0f} m2",
                f"{r['pct_uf']:.1f} %",
            ]
            for r in res["rows"]
        ]
        print(tabulate(rows, headers=["IDU", "Subdivision", "Surface", "% UF"], tablefmt="rounded_grid"))
    else:
        print("Aucune subdivision trouvee.")


if __name__ == "__main__":
    main()