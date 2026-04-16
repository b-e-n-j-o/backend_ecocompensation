# -*- coding: utf-8 -*-
"""
basol_par_coords.py — Lookup SSP/CASIAS par coordonnées (point ou bbox parcelle)
Vérifie si une parcelle est concernée par un site BASOL/CASIAS via l'API Géorisques.

Usage standalone :
    # Par point (lat/lon)
    python basol_par_coords.py --lat 43.8407 --lon 0.10243

    # Par IDU (fetch géométrie parcelle, puis centroïde)
    python basol_par_coords.py --idu 321190000E0252

    # Rayon personnalisé
    python basol_par_coords.py --lat 43.8407 --lon 0.10243 --rayon 200
"""

import argparse
import io
import json
import logging
import requests
import geopandas as gpd

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("basol_par_coords")

BASE_URL  = "https://www.georisques.gouv.fr/api/v1/ssp"
WFS_IGN   = "https://data.geopf.fr/wfs/ows"
RAYON_DEF = 500  # mètres — assez large pour couvrir une parcelle agricole


# ============================================================
# DÉCOMPOSITION IDU
# ============================================================
def decompose_idu(idu: str) -> tuple:
    idu         = idu.strip().upper().replace(" ", "")
    insee       = idu[:5]
    numero      = idu[-4:]
    milieu      = idu[5:-4]
    section_raw = milieu.lstrip("0") or milieu[-1]
    section     = ("0" + section_raw) if len(section_raw) == 1 else section_raw
    return insee, section, numero


def coords_depuis_idu(idu: str) -> tuple[float, float] | None:
    """Retourne (lat, lon) du centroïde de la parcelle via WFS IGN."""
    try:
        insee, section, numero = decompose_idu(idu)
        params = {
            "service":      "WFS",
            "version":      "2.0.0",
            "request":      "GetFeature",
            "typeNames":    "CADASTRALPARCELS.PARCELLAIRE_EXPRESS:parcelle",
            "srsName":      "EPSG:4326",
            "outputFormat": "application/json",
            "CQL_FILTER":   f"code_insee='{insee}' AND section='{section}' AND numero='{numero}'",
        }
        r   = requests.get(WFS_IGN, params=params, timeout=30)
        r.raise_for_status()
        gdf = gpd.read_file(io.BytesIO(r.content)).to_crs("EPSG:4326")
        if gdf.empty:
            log.warning(f"Parcelle introuvable : {idu}")
            return None
        centroid = gdf.geometry.iloc[0].centroid
        log.info(f"Parcelle {idu} → centroïde lat={centroid.y:.6f}, lon={centroid.x:.6f}")
        return round(centroid.y, 6), round(centroid.x, 6)
    except Exception as e:
        log.error(f"WFS IGN erreur pour {idu} : {e}")
        return None


# ============================================================
# APPEL API SSP
# ============================================================
def fetch_ssp(lat: float, lon: float, rayon: int = RAYON_DEF) -> dict:
    """Appelle l'API SSP Géorisques et retourne la réponse brute."""
    params = {
        "latlon":    f"{lon},{lat}",
        "rayon":     min(rayon, 10000),
        "page_size": 50,
    }
    log.info(f"API SSP — lat={lat}, lon={lon}, rayon={rayon}m")
    try:
        r = requests.get(BASE_URL, params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.Timeout:
        log.error("Timeout API SSP")
        return {}
    except Exception as e:
        log.error(f"Erreur API SSP : {e}")
        return {}


# ============================================================
# PARSING RÉSULTATS
# ============================================================
def parser_resultats(data: dict) -> dict:
    """
    Extrait les données utiles de la réponse SSP.
    Retourne un dict structuré avec casias, instructions, sis, sup.
    """
    casias       = data.get("casias", {}).get("data", [])
    instructions = data.get("instructions", {}).get("data", [])
    sis          = data.get("conclusions_sis", {}).get("data", [])
    sup          = data.get("conclusions_sup", {}).get("data", [])

    def fmt_casias(e):
        return {
            "identifiant_ssp":    e.get("identifiant_ssp"),
            "identifiant_casias": e.get("identifiant_casias"),
            "nom":                e.get("nom_etablissement"),
            "adresse":            e.get("adresse"),
            "commune":            e.get("nom_commune"),
            "code_insee":         e.get("code_insee"),
            "statut":             e.get("statut"),
            "date_maj":           e.get("date_maj"),
            "fiche_risque":       e.get("fiche_risque"),
        }

    def fmt_instruction(e):
        return {
            "identifiant_ssp": e.get("identifiant_ssp"),
            "nom":             e.get("nom_etablissement"),
            "adresse":         e.get("adresse"),
            "commune":         e.get("nom_commune"),
            "statut":          e.get("statut"),
            "fiche_risque":    e.get("fiche_risque"),
        }

    def fmt_sis(e):
        return {
            "identifiant_ssp":      e.get("identifiant_ssp"),
            "id_sis":               e.get("id_sis"),
            "nom":                  e.get("nom"),
            "adresse":              e.get("adresse"),
            "commune":              e.get("nom_commune"),
            "statut_classification": e.get("statut_classification"),
            "superficie":           e.get("superficie"),
            "fiche_risque":         e.get("fiche_risque"),
        }

    return {
        "nb_casias":       len(casias),
        "nb_instructions": len(instructions),
        "nb_sis":          len(sis),
        "nb_sup":          len(sup),
        "concerne":        bool(casias or instructions or sis),
        "casias":          [fmt_casias(e) for e in casias],
        "instructions":    [fmt_instruction(e) for e in instructions],
        "sis":             [fmt_sis(e) for e in sis],
        "sup_ssp":         sup,  # brut — peu fréquent
    }


# ============================================================
# POINT D'ENTRÉE PRINCIPAL
# ============================================================
def run_basol_coords(lat: float, lon: float,
                     rayon: int = RAYON_DEF,
                     idu: str = None) -> dict:
    """
    Vérifie si un point (lat/lon) est concerné par un site SSP/CASIAS.
    Retourne un dict prêt à injecter dans le pipeline de scoring.
    """
    data    = fetch_ssp(lat, lon, rayon)
    result  = parser_resultats(data)
    result["lat"]   = lat
    result["lon"]   = lon
    result["rayon"] = rayon
    if idu:
        result["idu"] = idu
    return result


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Lookup SSP/CASIAS (ex-BASOL) par coordonnées ou IDU parcelle"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--idu",              help="IDU de la parcelle (fetch centroïde auto)")
    group.add_argument("--lat", type=float,  help="Latitude (avec --lon)")
    parser.add_argument("--lon",  type=float, help="Longitude (requis si --lat)")
    parser.add_argument("--rayon", type=int,  default=RAYON_DEF,
                        help=f"Rayon de recherche en mètres (défaut: {RAYON_DEF})")
    args = parser.parse_args()

    # Résolution des coordonnées
    if args.idu:
        coords = coords_depuis_idu(args.idu)
        if not coords:
            print(f"Impossible de récupérer les coordonnées pour IDU={args.idu}")
            exit(1)
        lat, lon = coords
        result = run_basol_coords(lat, lon, rayon=args.rayon, idu=args.idu)
    else:
        if args.lon is None:
            parser.error("--lon requis avec --lat")
        result = run_basol_coords(args.lat, args.lon, rayon=args.rayon)

    # Affichage
    print(f"\n{'='*60}")
    print(f"  SSP / CASIAS (ex-BASOL)")
    print(f"{'='*60}")
    print(f"  Coordonnées   : lat={result['lat']}, lon={result['lon']}")
    print(f"  Rayon         : {result['rayon']}m")
    print(f"  Concernée     : {'⚠️  OUI' if result['concerne'] else '✅ NON'}")
    print(f"  CASIAS        : {result['nb_casias']}")
    print(f"  Instructions  : {result['nb_instructions']}")
    print(f"  SIS           : {result['nb_sis']}")

    if result["casias"]:
        print(f"\n  CASIAS détectés :")
        for c in result["casias"]:
            print(f"    [{c['identifiant_casias'] or '?'}] {c['nom'] or '—'}")
            print(f"      Adresse  : {c['adresse'] or '—'} — {c['commune'] or '—'}")
            print(f"      Statut   : {c['statut'] or '—'}")
            print(f"      Date MAJ : {c['date_maj'] or '—'}")
            if c.get("fiche_risque"):
                print(f"      Fiche    : {c['fiche_risque']}")

    if result["instructions"]:
        print(f"\n  Instructions en cours :")
        for i in result["instructions"]:
            print(f"    {i['nom'] or '—'} — {i['statut'] or '—'}")

    if result["sis"]:
        print(f"\n  Secteurs d'Information sur les Sols (SIS) :")
        for s in result["sis"]:
            print(f"    [{s['id_sis']}] {s['nom'] or '—'} — {s['statut_classification'] or '—'}")
            if s.get("superficie"):
                print(f"      Surface : {s['superficie']} m²")

    print(f"\n  JSON brut :")
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print()