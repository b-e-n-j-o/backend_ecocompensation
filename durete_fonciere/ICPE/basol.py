# -*- coding: utf-8 -*-
"""
basol.py — Module SSP/CASIAS (ex-BASOL) via API Géorisques
Recherche par coordonnées lat/lon + filtre sur identifiant_casias.
Appelé depuis friches.py quand un site_numero_basol est détecté.

Source : https://www.georisques.gouv.fr/api/v1/ssp
Paramètres : latlon + rayon (max 10 000m)

Usage standalone :
    python basol.py --numero MPY3203027 --lat 43.8407 --lon 0.10243
    python basol.py --numero MPY3203027 --lat 43.8407 --lon 0.10243 --verbose
"""

import argparse
import logging
import json
import requests

log = logging.getLogger("basol")

BASE_URL = "https://www.georisques.gouv.fr/api/v1/ssp"
RAYON_DEFAUT = 1000  # mètres

CRITICITE_STATUT = {
    "en cours d'instruction":      2,
    "en cours de travaux":         2,
    "en surveillance":             2,
    "en arret":                    1,
    "en arrêt":                    1,
    "traité - usage sensible":     1,
    "traite - usage sensible":     1,
    "traité - usage non sensible": 0,
    "traite - usage non sensible": 0,
    "sans suite":                  0,
    "non renseigné":               0,
}


def get_criticite(statut: str) -> int:
    if not statut:
        return 1
    s = statut.lower().strip()
    for key, val in CRITICITE_STATUT.items():
        if key in s:
            return val
    return 1


def fetch_ssp(lat: float, lon: float, rayon: int = RAYON_DEFAUT,
              verbose: bool = False) -> dict:
    params = {
        "latlon":    f"{lon},{lat}",
        "rayon":     min(rayon, 10000),
        "page_size": 50,
    }
    if verbose:
        print(f"  📡 SSP Géorisques lat={lat}, lon={lon}, rayon={rayon}m")
    try:
        r = requests.get(BASE_URL, params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.Timeout:
        log.warning("SSP Géorisques : timeout")
        return {}
    except Exception as e:
        log.warning(f"SSP Géorisques : erreur {e}")
        return {}


def normaliser_casias(entry: dict) -> dict:
    statut    = entry.get("statut") or ""
    criticite = get_criticite(statut)

    alertes = []
    if criticite >= 2:
        alertes.append(f"Site SSP/CASIAS actif ({statut}) — pollution en cours de traitement")
    elif criticite == 1:
        alertes.append(f"Site SSP/CASIAS en arrêt ({statut}) — historique de pollution")

    fiche = entry.get("fiche_risque")
    if fiche:
        alertes.append(f"Fiche risque disponible : {fiche}")

    return {
        "identifiant_ssp":    entry.get("identifiant_ssp"),
        "identifiant_casias": entry.get("identifiant_casias"),
        "nom_etablissement":  entry.get("nom_etablissement"),
        "adresse":            entry.get("adresse"),
        "code_insee":         entry.get("code_insee"),
        "nom_commune":        entry.get("nom_commune"),
        "statut":             statut,
        "date_maj":           entry.get("date_maj"),
        "fiche_risque":       fiche,
        "criticite":          criticite,
        "surcharge_basol":    criticite,
        "alertes":            alertes,
    }


def run_basol(numero_basol: str, lat: float, lon: float,
              rayon: int = RAYON_DEFAUT, verbose: bool = False) -> dict:
    """
    Lookup SSP/CASIAS par numéro BASOL + coordonnées.
    Appelé depuis friches.py — lat/lon proviennent des champs
    cartofriches.lat / cartofriches.long de la ligne friche.
    """
    if not numero_basol or str(numero_basol).strip() in ("", "None", "null"):
        return {"trouve": False, "note": "Numéro BASOL vide"}
    if lat is None or lon is None:
        return {"trouve": False, "note": "Coordonnées manquantes"}

    numero = str(numero_basol).strip()
    data   = fetch_ssp(lat, lon, rayon=rayon, verbose=verbose)
    if not data:
        return {"trouve": False, "note": "API SSP Géorisques indisponible"}

    casias_list = data.get("casias", {}).get("data", [])
    match = next(
        (c for c in casias_list if c.get("identifiant_casias") == numero),
        None
    )

    # Élargissement automatique si non trouvé
    if not match and rayon < 2000:
        if verbose:
            print(f"  ↳ Non trouvé à {rayon}m, élargissement à 2000m...")
        return run_basol(numero, lat, lon, rayon=2000, verbose=verbose)

    if not match:
        return {
            "numero_basol": numero,
            "trouve":       False,
            "note":         f"CASIAS {numero} non trouvé dans un rayon de {rayon}m",
        }

    result = normaliser_casias(match)
    result["trouve"]           = True
    result["instructions"]     = data.get("instructions", {}).get("data", [])
    result["conclusions_sis"]  = data.get("conclusions_sis", {}).get("data", [])
    result["conclusions_sup"]  = data.get("conclusions_sup", {}).get("data", [])

    if verbose:
        print(f"  ✅ CASIAS {result['identifiant_casias']} — {result['statut']}")

    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--numero",  required=True)
    parser.add_argument("--lat",     type=float, required=True)
    parser.add_argument("--lon",     type=float, required=True)
    parser.add_argument("--rayon",   type=int, default=RAYON_DEFAUT)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  MODULE BASOL/SSP — {args.numero}")
    print(f"{'='*60}\n")

    result = run_basol(args.numero, args.lat, args.lon,
                       rayon=args.rayon, verbose=args.verbose)

    if not result.get("trouve"):
        print(f"  ℹ️  {result.get('note')}")
    else:
        print(f"  Identifiant SSP   : {result['identifiant_ssp']}")
        print(f"  Identifiant CASIAS: {result['identifiant_casias']}")
        print(f"  Établissement     : {result['nom_etablissement'] or '—'}")
        print(f"  Adresse           : {result['adresse']}")
        print(f"  Commune           : {result['nom_commune']} ({result['code_insee']})")
        print(f"  Statut            : {result['statut']}")
        print(f"  Date MAJ          : {result['date_maj']}")
        print(f"  Fiche risque      : {result['fiche_risque'] or '—'}")
        print(f"  Criticité         : {result['criticite']}/2")
        print(f"  Surcharge score   : +{result['surcharge_basol']} pts")
        nb_sis = len(result.get("conclusions_sis", []))
        nb_sup = len(result.get("conclusions_sup", []))
        if nb_sis:
            print(f"  SIS associés      : {nb_sis}")
        if nb_sup:
            print(f"  SUP associées     : {nb_sup}")
        if result["alertes"]:
            print(f"\n  Alertes :")
            for a in result["alertes"]:
                print(f"    ⚠️  {a}")

    print(f"\n  JSON brut :")
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print()