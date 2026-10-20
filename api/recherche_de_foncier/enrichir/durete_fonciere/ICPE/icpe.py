# -*- coding: utf-8 -*-
"""
icpe.py — Module ICPE (Installations Classées pour la Protection de l'Environnement)
Analyse exhaustive + archivage automatique des documents PDF.

Usage standalone :
    python icpe.py --idu 32119000AH0096
    python icpe.py --idu 32119000AH0096 --no-download
    python icpe.py --idu 862750000D0319

Importable :
    from icpe import run_icpe
    result = run_icpe("32119000AH0096")
"""

import argparse
import io
import json
import logging
import os
import re
import requests
from shapely.geometry import Point, shape
import geopandas as gpd

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("icpe")

ICPE_URL       = "https://www.georisques.gouv.fr/api/v1/installations_classees"
WFS_IGN        = "https://data.geopf.fr/wfs/ows"
PAGE_SIZE      = 100
RAYON_BUFFER_M = 200
DOWNLOAD_DIR   = "downloads_icpe"

CRITICITE_ETAT = {
    "en exploitation avec titre":  1,
    "en exploitation sans titre":  2,
    "à l'arrêt définitif":        0,
    "en cours de cessation":       1,
    "non exploité":                0,
}
SEVESO_SCORES = {
    "seuil haut": 3,
    "seuil bas":  2,
    "non seveso": 0,
}


# ============================================================
# UTILS
# ============================================================
def slugify(text: str) -> str:
    return re.sub(r'[^\w\-]', '_', (text or "").strip())


def force_list(data) -> list:
    if data is None:   return []
    if isinstance(data, list): return data
    if isinstance(data, dict): return [data]
    return []


def get_criticite(site: dict) -> int:
    etat   = (site.get("etatActivite") or "").lower()
    seveso = (site.get("statutSeveso") or "").lower()
    score  = CRITICITE_ETAT.get(etat, 0)
    return max(score, SEVESO_SCORES.get(seveso, 0))


# ============================================================
# TÉLÉCHARGEMENT PDF
# ============================================================
def download_pdf(url: str, folder: str, filename: str):
    """Télécharge un PDF Géorisques. Skip si déjà présent."""
    if not url:
        return
    filename = slugify(filename)
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    path = os.path.join(folder, filename)
    if os.path.exists(path):
        return
    try:
        r = requests.get(url, timeout=30, stream=True)
        if r.status_code == 200:
            with open(path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            log.info(f"  [OK] {filename}")
        else:
            log.warning(f"  [ÉCHEC {r.status_code}] {filename}")
    except Exception as e:
        log.error(f"  [ERREUR] {filename} : {e}")


# ============================================================
# 1. GÉOMÉTRIE PARCELLE
# ============================================================
def decompose_idu(idu: str) -> tuple:
    idu         = idu.strip().upper().replace(" ", "")
    insee       = idu[:5]
    numero      = idu[-4:]
    milieu      = idu[5:-4]
    section_raw = milieu.lstrip("0") or milieu[-1]
    section     = ("0" + section_raw) if len(section_raw) == 1 else section_raw
    return insee, section, numero


def fetch_geom_parcelle(idu: str):
    try:
        insee, section, numero = decompose_idu(idu)
        params = {
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeNames": "CADASTRALPARCELS.PARCELLAIRE_EXPRESS:parcelle",
            "srsName": "EPSG:4326", "outputFormat": "application/json",
            "CQL_FILTER": f"code_insee='{insee}' AND section='{section}' AND numero='{numero}'",
        }
        r   = requests.get(WFS_IGN, params=params, timeout=30)
        gdf = gpd.read_file(io.BytesIO(r.content))
        if gdf.empty:
            return None
        return gdf.to_crs("EPSG:4326").geometry.iloc[0].__geo_interface__
    except Exception as e:
        log.error(f"WFS IGN erreur : {e}")
        return None


# ============================================================
# 2. FETCH ICPE (paginé)
# ============================================================
def fetch_icpe_data(geojson_geometry: dict) -> list[dict]:
    poly     = shape(geojson_geometry)
    centroid = poly.centroid
    _, _, maxx, maxy = poly.bounds
    rayon = min(int(centroid.distance(Point(maxx, maxy)) * 111000) + RAYON_BUFFER_M, 10000)

    all_sites, page = [], 1
    while True:
        params = {
            "latlon":    f"{centroid.x},{centroid.y}",
            "rayon":     rayon,
            "page":      page,
            "page_size": PAGE_SIZE,
        }
        r   = requests.get(ICPE_URL, params=params, timeout=30)
        res = r.json()
        data = res.get("data", [])
        if not data:
            break
        all_sites.extend(data)
        if page >= res.get("total_pages", 1):
            break
        page += 1
    return all_sites


# ============================================================
# 3. TRAITEMENT : FILTRAGE + STRUCTURATION + TÉLÉCHARGEMENT
# ============================================================
def process_sites(sites_bruts: list[dict], geojson_geometry: dict,
                  download: bool = True) -> list[dict]:
    poly    = shape(geojson_geometry)
    results = []

    for site in sites_bruts:
        lon = site.get("longitude", 0)
        lat = site.get("latitude", 0)
        pt  = Point(lon, lat)

        dans_parcelle = poly.contains(pt)
        proche        = poly.distance(pt) * 111000 < 50

        if not (dans_parcelle or proche):
            continue

        nom  = site.get("raisonSociale") or "Inconnu"
        aiot = site.get("codeAIOT") or "SansAIOT"

        # ── Dossier de téléchargement ──
        site_folder = os.path.join(DOWNLOAD_DIR, slugify(f"{aiot}_{nom}"))
        if download:
            os.makedirs(site_folder, exist_ok=True)

        log.info(f"Traitement : {nom} ({aiot})")

        # ── Inspections ──
        inspections = []
        for i in force_list(site.get("inspections")):
            date_i = i.get("dateInspection") or "sans_date"
            fdata  = i.get("fichierInspection") or {}
            url    = fdata.get("urlFichier")
            nom_f  = fdata.get("nomFichier") or f"Inspection_{date_i}"
            if download and url:
                download_pdf(url, site_folder, f"INSPECTION_{date_i}_{nom_f}")
            inspections.append({
                "date": date_i,
                "nom":  nom_f,
                "url":  url,
            })

        # ── Documents administratifs ──
        documents = []
        for d in force_list(site.get("documentsHorsInspection")):
            date_d = d.get("dateFichier") or "sans_date"
            url    = d.get("urlFichier")
            type_d = d.get("typeFichier") or "Document"
            nom_f  = d.get("nomFichier") or f"{slugify(type_d)}_{date_d}"
            if download and url:
                download_pdf(url, site_folder, f"ADMIN_{date_d}_{nom_f}")
            documents.append({
                "type": type_d,
                "date": date_d,
                "url":  url,
            })

        # ── Rubriques ──
        rubriques = [
            {
                "n":        r.get("numeroRubrique"),
                "libelle":  r.get("nature"),
                "regime":   r.get("regimeAutoriseAlinea"),
                "capacite": f"{r.get('quantiteTotale', '')} {r.get('unite', '')}".strip(),
                "date":     r.get("dateMotif"),
            }
            for r in force_list(site.get("rubriques"))
        ]

        criticite = get_criticite(site)

        results.append({
            "identite": {
                "nom":     nom,
                "siret":   site.get("siret"),
                "code_aiot": aiot,
                "service": site.get("serviceAIOT"),
                "adresse": " ".join(filter(None, [
                    site.get("adresse1"), site.get("adresse2"),
                    site.get("adresse3"), site.get("commune"),
                ])),
                "code_postal": site.get("codePostal"),
                "code_insee":  site.get("codeInsee"),
                "code_naf":    site.get("codeNaf"),
            },
            "statut": {
                "etat":    site.get("etatActivite"),
                "regime":  site.get("regime"),
                "seveso":  site.get("statutSeveso"),
                "ied":     site.get("ied"),
                "priorite_nationale": site.get("prioriteNationale"),
                "industrie":  site.get("industrie"),
                "bovins":     site.get("bovins"),
                "porcs":      site.get("porcs"),
                "volailles":  site.get("volailles"),
                "carriere":   site.get("carriere"),
                "eolienne":   site.get("eolienne"),
                "maj":        site.get("date_maj"),
            },
            "geographie": {
                "longitude":    lon,
                "latitude":     lat,
                "coordX_2154":  site.get("coordonneeXAIOT"),
                "coordY_2154":  site.get("coordonneeYAIOT"),
                "dans_parcelle": dans_parcelle,
            },
            "donnees_techniques": {
                "rubriques":               rubriques,
                "inspections":             inspections,
                "documents_administratifs": documents,
            },
            "analyse": {
                "criticite_score": criticite,
                "url_fiche": f"https://www.georisques.gouv.fr/ecologie/installations/fiche/{aiot}",
                "dossier_local": site_folder if download else None,
            },
        })

    return results


# ============================================================
# 4. SCORING GLOBAL
# ============================================================
def scorer_icpe(sites: list[dict]) -> dict:
    if not sites:
        return {
            "nb_icpe": 0,
            "alertes": [],
            "note":    "Aucune ICPE dans ou à proximité de la parcelle",
        }

    alertes = []
    for s in sites:
        c   = s["analyse"]["criticite_score"]
        nom = s["identite"]["nom"]
        if c >= 3:
            alertes.append(f"ICPE SEVESO seuil haut : {nom}")
        elif c == 2:
            alertes.append(f"ICPE SEVESO seuil bas / sans titre : {nom}")
        elif c == 1:
            alertes.append(f"ICPE en exploitation : {nom}")
        if s["statut"].get("ied"):
            alertes.append(f"IED (directive émissions industrielles) : {nom}")
        if s["statut"].get("priorite_nationale"):
            alertes.append(f"Site priorité nationale : {nom}")

    return {
        "nb_icpe": len(sites),
        "alertes": alertes,
        "note":    None,
    }


# ============================================================
# 5. POINT D'ENTRÉE PRINCIPAL
# ============================================================
def run_icpe(idu: str, download: bool = True) -> dict:
    """
    Analyse ICPE complète pour un IDU.
    Retourne un dict avec les sites structurés + alertes.
    """
    log.info(f"Analyse ICPE pour l'IDU: {idu}")

    if download:
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    geom = fetch_geom_parcelle(idu)
    if not geom:
        return {
            "idu": idu, "nb_icpe": 0,
            "alertes": [], "note": "Géométrie parcelle introuvable",
        }

    sites_bruts = fetch_icpe_data(geom)
    sites       = process_sites(sites_bruts, geom, download=download)
    scoring     = scorer_icpe(sites)

    return {
        "idu":   idu,
        "count": len(sites),
        "sites": sites,
        **scoring,
    }


# ============================================================
# MAIN
# ============================================================
def _cleanup_downloads_for_result(result: dict):
    """
    Dry-run : supprime les fichiers/dossiers téléchargés pour ce résultat.
    On utilise les chemins 'dossier_local' présents dans result['sites'][*]['analyse'].
    """
    sites = result.get("sites") or []
    seen_dirs: set[str] = set()
    for s in sites:
        analyse = s.get("analyse") or {}
        d = analyse.get("dossier_local")
        if not d:
            continue
        if d in seen_dirs:
            continue
        seen_dirs.add(d)
        if os.path.isdir(d):
            # On supprime récursivement le contenu puis le dossier
            for root, dirs, files in os.walk(d, topdown=False):
                for name in files:
                    try:
                        os.remove(os.path.join(root, name))
                    except Exception as e:
                        log.warning(f"Impossible de supprimer le fichier {name} dans {root} : {e}")
                for name in dirs:
                    try:
                        os.rmdir(os.path.join(root, name))
                    except Exception as e:
                        log.warning(f"Impossible de supprimer le dossier {name} dans {root} : {e}")
            try:
                os.rmdir(d)
            except Exception as e:
                log.warning(f"Impossible de supprimer le dossier racine {d} : {e}")
        else:
            log.debug(f"Dossier local inexistant ou non dossier : {d}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyse ICPE par IDU")
    parser.add_argument("--idu", required=True)
    parser.add_argument(
        "--no-download",
        action="store_true",
        help="Ne pas télécharger les PDFs"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Télécharger puis supprimer les PDFs en fin de run"
    )
    args = parser.parse_args()

    # Si --no-download est actif, on ignore --dry-run
    download_flag = not args.no_download
    result = run_icpe(args.idu, download=download_flag)

    # Sortie JSON
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))

    # Si dry-run ET qu'on a effectivement téléchargé, on nettoie les dossiers locaux
    if args.dry_run and download_flag:
        log.info("Dry-run activé : suppression des fichiers téléchargés...")
        _cleanup_downloads_for_result(result)
        log.info("Dry-run : suppression terminée.")

    log.info(f"Analyse terminée. {result['count']} site(s) traité(s).")