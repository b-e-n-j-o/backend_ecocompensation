# -*- coding: utf-8 -*-
"""
icpe.py — Module ICPE (Installations Classées pour la Protection de l'Environnement)
Fetch les installations classées à proximité d'une parcelle via API Géorisques.
Filtre par intersection spatiale avec la géométrie réelle de la parcelle.

Source : https://www.georisques.gouv.fr/api/v1/installations_classees

Usage standalone :
    python icpe.py --idu 321190000E0252 --verbose
    python icpe.py --idu 862750000D0319 --verbose
"""

import argparse
import io
import json
import logging
import requests
from shapely.geometry import Point, shape
import geopandas as gpd

log = logging.getLogger("icpe")

ICPE_URL    = "https://www.georisques.gouv.fr/api/v1/installations_classees"
WFS_IGN     = "https://data.geopf.fr/wfs/ows"
PAGE_SIZE   = 100
RAYON_EXTRA = 200  # buffer en mètres autour de la bbox parcelle

CRITICITE_ETAT = {
    "en exploitation avec titre":  1,
    "en exploitation sans titre":  2,
    "à l'arrêt définitif":        0,
    "en cours de cessation":       1,
    "non exploité":                0,
    "inconnu":                     0,
}

SEVESO_SCORES = {
    "seuil haut": 3,
    "seuil bas":  2,
    "non seveso": 0,
}


def get_criticite_icpe(site: dict) -> int:
    etat   = (site.get("etatActivite") or "").lower()
    seveso = (site.get("statutSeveso") or "").lower()
    score  = CRITICITE_ETAT.get(etat, 0)
    return max(score, SEVESO_SCORES.get(seveso, 0))


# ============================================================
# 1. DÉCOMPOSITION IDU + GÉOMÉTRIE PARCELLE
# ============================================================
def decompose_idu(idu: str) -> tuple[str, str, str]:
    """
    Décompose un IDU en (insee, section, numero) pour l'appel WFS IGN.

    Formats IDU rencontrés :
      - MAJIC public   : 86275000D0319  → milieu '000D'  → section '0D'
      - DVF Etalab     : 862750000E0452 → milieu '0000E' → section '0E'
      - Section double : 86070000AN0063 → milieu '000AN' → section 'AN'

    Règle WFS IGN confirmée par test :
      - Section 1 lettre  : '0D', '0E'  → 1 zéro préfixe
      - Section 2 lettres : 'AN', 'ZB'  → pas de préfixe
    """
    idu = idu.strip().upper().replace(" ", "")
    insee       = idu[:5]
    numero      = idu[-4:]
    milieu      = idu[5:-4]
    section_raw = milieu.lstrip("0") or milieu[-1]
    section     = ("0" + section_raw) if len(section_raw) == 1 else section_raw
    log.debug("decompose_idu(%s) → insee=%s, section=%s, numero=%s", idu, insee, section, numero)
    return insee, section, numero


def fetch_geom_parcelle(idu: str) -> dict | None:
    """Retourne la géométrie GeoJSON (EPSG:4326) de la parcelle via WFS IGN."""
    try:
        insee, section, numero = decompose_idu(idu)
    except Exception as e:
        log.warning(f"decompose_idu échoué pour {idu} : {e}")
        return None

    params = {
        "service":      "WFS",
        "version":      "2.0.0",
        "request":      "GetFeature",
        "typeNames":    "CADASTRALPARCELS.PARCELLAIRE_EXPRESS:parcelle",
        "srsName":      "EPSG:4326",
        "outputFormat": "application/json",
        "CQL_FILTER":   f"code_insee='{insee}' AND section='{section}' AND numero='{numero}'",
    }

    try:
        r = requests.get(WFS_IGN, params=params, timeout=30)
        r.raise_for_status()
        gdf = gpd.read_file(io.BytesIO(r.content))
        if gdf.empty:
            log.warning(f"WFS IGN : parcelle introuvable pour IDU={idu}")
            return None
        geom = gdf.to_crs("EPSG:4326").geometry.iloc[0]
        return geom.__geo_interface__
    except Exception as e:
        log.warning(f"Fetch géométrie WFS échoué pour {idu} : {e}")
        return None


# ============================================================
# 2. FETCH ICPE PAR CENTROÏDE + RAYON (paginé)
# ============================================================
def fetch_icpe_around(geojson_geometry: dict, verbose: bool = False) -> list[dict]:
    """Fetch toutes les ICPE dans le rayon autour de la géométrie, avec pagination."""
    poly     = shape(geojson_geometry)
    centroid = poly.centroid
    _, _, maxx, maxy = poly.bounds

    distance_deg = centroid.distance(Point(maxx, maxy))
    rayon        = int(distance_deg * 111000) + RAYON_EXTRA
    rayon        = min(rayon, 10000)

    if verbose:
        print(f"  📡 ICPE fetch : centroïde=({centroid.y:.5f},{centroid.x:.5f}) rayon={rayon}m")

    all_sites = []
    page      = 1

    while True:
        params = {
            "latlon":    f"{centroid.x},{centroid.y}",
            "rayon":     rayon,
            "page":      page,
            "page_size": PAGE_SIZE,
        }
        try:
            r = requests.get(ICPE_URL, params=params, timeout=30)
            r.raise_for_status()
            data     = r.json()
            entities = data.get("data", [])
            if not entities:
                break
            all_sites.extend(entities)
            if verbose:
                print(f"  ↳ Page {page} : {len(entities)} sites ({len(all_sites)} total)")
            if page >= data.get("total_pages", 1):
                break
            page += 1
        except Exception as e:
            log.warning(f"ICPE fetch page {page} : {e}")
            break

    return all_sites


# ============================================================
# 3. FILTRAGE SPATIAL + EXTRACTION COMPLÈTE
# ============================================================
def filtrer_et_scorer(sites: list[dict], geojson_geometry: dict) -> list[dict]:
    """
    Filtre les sites dans ou à moins de 50m de la parcelle.
    Retourne tous les attributs disponibles.
    """
    poly    = shape(geojson_geometry)
    results = []

    for site in sites:
        lon = site.get("longitude")
        lat = site.get("latitude")
        if lon is None or lat is None:
            continue

        pt            = Point(lon, lat)
        dans_parcelle = poly.contains(pt)
        proche        = poly.distance(pt) * 111000 < 50

        if not (dans_parcelle or proche):
            continue

        criticite   = get_criticite_icpe(site)
        rubriques   = site.get("rubriques") or []
        inspections = site.get("inspections") or []
        docs        = site.get("documentsHorsInspection") or []

        # Dernière inspection
        last_insp     = inspections[0] if inspections else None
        last_insp_url = (last_insp or {}).get("fichierInspection", {}).get("urlFichier")

        # Rubriques complètes
        rubriques_liste = [
            {
                "numero":   r.get("numeroRubrique"),
                "nature":   r.get("nature"),
                "alinea":   r.get("alinea"),
                "regime":   r.get("regimeAutoriseAlinea"),
                "quantite": r.get("quantiteTotale"),
                "unite":    r.get("unite"),
                "date":     r.get("dateMotif"),
            }
            for r in rubriques
        ]

        # Inspections complètes
        inspections_liste = [
            {
                "date": i.get("dateInspection"),
                "nom":  (i.get("fichierInspection") or {}).get("nomFichier"),
                "type": (i.get("fichierInspection") or {}).get("typeFichier"),
                "url":  (i.get("fichierInspection") or {}).get("urlFichier"),
            }
            for i in inspections
        ]

        # Documents hors inspection
        documents_liste = [
            {
                "nom":  d.get("nomFichier"),
                "type": d.get("typeFichier"),
                "date": d.get("dateFichier"),
                "url":  d.get("urlFichier"),
            }
            for d in docs
        ]

        results.append({
            # ── Identité ──
            "raison_sociale":       site.get("raisonSociale"),
            "adresse":              " ".join(filter(None, [
                                        site.get("adresse1"),
                                        site.get("adresse2"),
                                        site.get("adresse3"),
                                        site.get("codePostal"),
                                        site.get("commune"),
                                    ])),
            "code_postal":          site.get("codePostal"),
            "commune":              site.get("commune"),
            "code_insee":           site.get("codeInsee"),
            "code_naf":             site.get("codeNaf"),
            "siret":                site.get("siret"),
            "code_aiot":            site.get("codeAIOT"),
            "service_aiot":         site.get("serviceAIOT"),
            "date_maj":             site.get("date_maj"),
            # ── Statut ──
            "etat_activite":        site.get("etatActivite"),
            "regime":               site.get("regime"),
            "statut_seveso":        site.get("statutSeveso"),
            "ied":                  site.get("ied"),
            "priorite_nationale":   site.get("prioriteNationale"),
            "industrie":            site.get("industrie"),
            "bovins":               site.get("bovins"),
            "porcs":                site.get("porcs"),
            "volailles":            site.get("volailles"),
            "carriere":             site.get("carriere"),
            "eolienne":             site.get("eolienne"),
            # ── Géo ──
            "longitude":            lon,
            "latitude":             lat,
            "coordX_2154":          site.get("coordonneeXAIOT"),
            "coordY_2154":          site.get("coordonneeYAIOT"),
            "dans_parcelle":        dans_parcelle,
            # ── Rubriques (substances dangereuses) ──
            "nb_rubriques":         len(rubriques),
            "rubriques_liste":      rubriques_liste,
            # ── Inspections ──
            "nb_inspections":       len(inspections),
            "inspections_liste":    inspections_liste,
            "derniere_inspection":  last_insp.get("dateInspection") if last_insp else None,
            "url_derniere_inspection": last_insp_url,
            # ── Documents ──
            "nb_documents":         len(docs),
            "documents_liste":      documents_liste,
            # ── Scoring ──
            "criticite":            criticite,
        })

    return sorted(results, key=lambda x: x["criticite"], reverse=True)


# ============================================================
# 4. SCORING GLOBAL
# ============================================================
def scorer_icpe(sites_filtres: list[dict]) -> dict:
    if not sites_filtres:
        return {
            "nb_icpe":        0,
            "surcharge_icpe": 0,
            "alertes":        [],
            "note":           "Aucune ICPE dans ou à proximité de la parcelle",
        }

    alertes       = []
    surcharge_max = 0

    for s in sites_filtres:
        c   = s["criticite"]
        nom = s["raison_sociale"] or "?"
        surcharge_max = max(surcharge_max, c)

        if c >= 3:
            alertes.append(f"ICPE SEVESO seuil haut : {nom}")
        elif c == 2:
            alertes.append(f"ICPE SEVESO seuil bas / sans titre : {nom}")
        elif c == 1:
            alertes.append(f"ICPE en exploitation : {nom}")

        if s.get("ied"):
            alertes.append(f"IED (directive émissions industrielles) : {nom}")
        if s.get("priorite_nationale"):
            alertes.append(f"Site priorité nationale : {nom}")

    return {
        "nb_icpe":        len(sites_filtres),
        "surcharge_icpe": surcharge_max,
        "alertes":        alertes,
        "note":           None,
        "sites":          sites_filtres,
    }


# ============================================================
# 5. POINT D'ENTRÉE PRINCIPAL
# ============================================================
def run_icpe(idu: str, geojson_geometry: dict = None, verbose: bool = False) -> dict:
    """
    Analyse ICPE pour un IDU.
    Si geojson_geometry est fourni, évite l'appel WFS IGN.
    """
    if geojson_geometry is None:
        if verbose:
            print(f"  🗺️  Fetch géométrie WFS IGN pour IDU={idu}")
        geojson_geometry = fetch_geom_parcelle(idu)
        if geojson_geometry is None:
            return {
                "idu":            idu,
                "nb_icpe":        0,
                "surcharge_icpe": 0,
                "alertes":        [],
                "note":           f"Géométrie parcelle introuvable pour IDU={idu}",
            }

    sites_bruts   = fetch_icpe_around(geojson_geometry, verbose=verbose)
    sites_filtres = filtrer_et_scorer(sites_bruts, geojson_geometry)

    if verbose:
        print(f"  ✅ {len(sites_bruts)} sites fetchés → {len(sites_filtres)} dans/proche parcelle")

    result        = scorer_icpe(sites_filtres)
    result["idu"] = idu
    return result


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Analyse ICPE par IDU")
    parser.add_argument("--idu",     required=True)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  MODULE ICPE — IDU : {args.idu}")
    print(f"{'='*60}\n")

    result = run_icpe(args.idu, verbose=True)

    print(f"\n  Nb ICPE           : {result['nb_icpe']}")
    print(f"  Surcharge score   : +{result['surcharge_icpe']} pts")
    if result.get("note"):
        print(f"  ℹ️  {result['note']}")

    for s in result.get("sites", []):
        loc = "dans parcelle" if s["dans_parcelle"] else "proche (<50m)"
        print(f"\n  [{loc}] {s['raison_sociale']}")
        print(f"    Adresse         : {s['adresse']}")
        print(f"    SIRET           : {s['siret']} | NAF : {s['code_naf']}")
        print(f"    Code AIOT       : {s['code_aiot']} | {s['service_aiot']}")
        print(f"    État            : {s['etat_activite'] or '—'}")
        print(f"    Régime          : {s['regime']}")
        print(f"    Seveso          : {s['statut_seveso'] or 'Non Seveso'}")
        print(f"    IED             : {'oui' if s['ied'] else 'non'}")
        print(f"    Date MAJ        : {s['date_maj'] or '—'}")
        print(f"    Criticité       : {s['criticite']}/3")

        if s["rubriques_liste"]:
            print(f"    Rubriques ({s['nb_rubriques']}) :")
            for r in s["rubriques_liste"]:
                q = f" — {r['quantite']} {r['unite']}" if r.get("quantite") else ""
                print(f"      • [{r['numero']}] {r['nature']}{q}")

        if s["inspections_liste"]:
            print(f"    Inspections ({s['nb_inspections']}) :")
            for i in s["inspections_liste"]:
                print(f"      • {i['date']} — {i['nom'] or i['type'] or '?'}")
                if i.get("url"):
                    print(f"        {i['url']}")

        if s["documents_liste"]:
            print(f"    Documents ({s['nb_documents']}) :")
            for d in s["documents_liste"]:
                print(f"      • [{d['type']}] {d['nom']} ({d['date'] or '?'})")
                if d.get("url"):
                    print(f"        {d['url']}")

    if result.get("alertes"):
        print(f"\n  Alertes :")
        for a in result["alertes"]:
            print(f"    ⚠️  {a}")

    print(f"\n  JSON brut :")
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print()