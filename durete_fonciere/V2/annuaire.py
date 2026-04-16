"""
Module Annuaire Entreprises — Kerelia Dureté Foncière
======================================================
Source : https://recherche-entreprises.api.gouv.fr
Gratuit, sans clé, données INSEE + RNE consolidées.

Expose :
    fetch_annuaire(siren) -> dict   # données brutes
    scorer_axe1(data)     -> (int, str)
    scorer_axe2(data)     -> (int, str)
    scorer_axe3_base(data) -> (int, str)
"""

import requests
import logging
import json
import argparse
import sys
from typing import Optional

log = logging.getLogger("annuaire")

# ---------------------------------------------------------------------------
# Mapping nature juridique INSEE → score Axe 1 (/40)
# Code à 4 chiffres INSEE
# ---------------------------------------------------------------------------

AXE1_CODE = {
    # État et assimilés — inaliénabilité absolue
    "7100": (40, "État français — domaine public inaliénable"),
    "7111": (40, "Commune — domaine public inaliénable"),
    "7112": (40, "Commune associée — domaine public inaliénable"),
    "7120": (38, "Département — domaine public"),
    "7130": (38, "Région — domaine public"),
    "7150": (38, "Collectivité territoriale à statut particulier"),
    "7160": (40, "Autre collectivité territoriale"),
    "7210": (40, "Établissement public national"),
    "7220": (38, "Établissement public local"),
    "7230": (35, "EPCI — intercommunalité"),
    "7312": (35, "Syndicat de communes"),
    "7321": (35, "Syndicat mixte"),
    # Organismes logement social
    "4110": (35, "Office public HLM — patrimoine affecté logement social"),
    "4120": (35, "SA HLM — patrimoine affecté logement social"),
    # Établissements publics de santé / enseignement
    "7340": (38, "Établissement public de santé"),
    "7350": (38, "Établissement public social ou médico-social"),
    "7381": (35, "Établissement public à caractère scientifique"),
    # Structures agricoles et forestières
    "6521": (14, "GFA — Groupement Foncier Agricole"),
    "6534": (14, "GFA — Groupement Foncier Agricole (autre)"),
    "6522": (16, "Groupement Forestier"),
    "6540": (18, "Société civile agricole"),
    "2120": (20, "GAEC — Groupement Agricole d'Exploitation en Commun"),
    # Sociétés civiles immobilières
    "6515": (10, "SCI — Société Civile Immobilière"),
    "6516": (10, "SCI — variante"),
    # Sociétés commerciales
    "5720": (15, "SASU — associé unique, décision rapide"),
    "5710": (16, "SAS — décision collective selon statuts"),
    "5498": (18, "SARL — pluralité d'associés fréquente"),
    "5485": (15, "EURL — associé unique"),
    "5310": (20, "SA à conseil d'administration — processus formel lourd"),
    "5370": (20, "SCA — commandite par actions"),
    "5306": (20, "SA à directoire"),
    # Associations
    "9220": (26, "Association loi 1901 — gouvernance associative"),
    "9221": (26, "Association déclarée"),
    # Fondations
    "9230": (28, "Fondation — patrimoine affecté mission d'intérêt général"),
    # Coopératives
    "2310": (22, "Coopérative agricole"),
    "5550": (22, "Société coopérative"),
}

# Fallback par libellé si code non reconnu
AXE1_LIBELLE = {
    "gfa":                    (14, "GFA — structure agricole patrimoniale"),
    "groupement foncier":     (14, "Groupement foncier"),
    "groupement forestier":   (16, "Groupement forestier"),
    "sci":                    (10, "SCI — structure dédiée gestion immobilière"),
    "société civile immobilière": (10, "SCI"),
    "sasu":                   (15, "SASU — associé unique"),
    "sas":                    (16, "SAS"),
    "sarl":                   (18, "SARL"),
    "eurl":                   (15, "EURL — associé unique"),
    "sa ":                    (20, "SA — processus décisionnel formel"),
    "commune":                (40, "Commune — domaine public"),
    "état":                   (40, "État — inaliénabilité absolue"),
    "département":            (38, "Département"),
    "région":                 (38, "Région"),
    "epci":                   (35, "EPCI"),
    "syndicat":               (35, "Syndicat"),
    "hlm":                    (35, "HLM — logement social"),
    "office public":          (35, "Office public"),
    "association":            (26, "Association"),
    "fondation":              (28, "Fondation"),
    "safer":                  (20, "SAFER — droit de préemption foncier rural"),
    "conservatoire":          (38, "Conservatoire — mission protection espaces naturels"),
    "parc naturel":           (38, "Parc naturel — mission conservation"),
}


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_annuaire(siren: str) -> dict:
    """
    Interroge l'API Annuaire Entreprises.
    Retourne le dict brut ou {} si non trouvé.
    """
    url = "https://recherche-entreprises.api.gouv.fr/search"
    try:
        r = requests.get(url, params={"q": siren, "per_page": 1}, timeout=10)
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            log.warning(f"SIREN {siren} introuvable dans Annuaire Entreprises")
            return {}
        data = results[0]
        log.info(f"Annuaire OK : {data.get('nom_complet')} ({data.get('nature_juridique')})")
        return data
    except requests.exceptions.Timeout:
        log.error("Timeout Annuaire Entreprises")
        return {}
    except Exception as e:
        log.error(f"Erreur Annuaire Entreprises : {e}")
        return {}


def fetch_annuaire_raw(siren: str) -> dict:
    """
    Alias explicite pour l'orchestration : renvoie la réponse brute Annuaire.
    (La fonction fetch_annuaire renvoie déjà le dict brut.)
    """
    return fetch_annuaire(siren)


# ---------------------------------------------------------------------------
# Scorers
# ---------------------------------------------------------------------------

def scorer_axe1(data: dict) -> tuple[int, str]:
    """Nature de la PM — /40"""
    code = str(data.get("nature_juridique", "")).strip()
    nom  = str(data.get("nom_complet", "")).lower()

    # 1. Par code INSEE exact
    if code in AXE1_CODE:
        score, note = AXE1_CODE[code]
        return score, f"Code juridique {code} → {note}"

    # 2. Par libellé dans le nom
    for keyword, (score, note) in AXE1_LIBELLE.items():
        if keyword in nom:
            return score, f"Nom contient '{keyword}' → {note}"

    # 3. Complements : est_service_public
    if data.get("complements", {}).get("est_service_public"):
        return 38, "Flagué service public → droit public"

    return 20, f"Code juridique '{code}' non reconnu — score neutre"


def scorer_axe2(data: dict) -> tuple[int, str]:
    """Gouvernance et complexité décisionnelle — /25"""
    dirigeants = data.get("dirigeants", [])
    nb = len(dirigeants)
    code = str(data.get("nature_juridique", "")).strip()
    nom  = data.get("nom_complet", "").lower()

    # Personnes morales publiques → décision politique
    if code.startswith("7") or "commune" in nom or "département" in nom:
        return 20, "Personne morale publique — décision politique soumise au vote"

    if nb == 0:
        return 10, "Nombre de décideurs inconnu"
    if nb == 1:
        # Vérifier si c'est bien un associé unique (EURL/SASU)
        qualites = [d.get("qualite", "").lower() for d in dirigeants]
        if any("unique" in q or "seul" in q for q in qualites):
            return 2, "Associé unique confirmé — décision immédiate"
        return 4, "1 seul dirigeant — décision rapide"
    if nb <= 3:
        return 8, f"{nb} décideurs — risque de blocage limité"
    if nb <= 6:
        return 15, f"{nb} décideurs — négociation complexe, risque de désaccord"
    return 22, f"{nb}+ décideurs — unanimité difficile, processus long"


def scorer_axe3_base(data: dict) -> tuple[int, str]:
    """
    Situation financière — /20
    Version de base sans BODACC (statut seul).
    Sera affinée par scorer_axe3_complet() dans l'orchestrateur.
    """
    statut = data.get("etat_administratif", "").upper()
    finances = data.get("finances")

    if statut in ("C", "F"):
        return 3, "Société inactive/cessée — actif dormant, dirigeants réceptifs"

    if statut == "A":
        if finances:
            # Si données financières disponibles (rare sur API gratuite)
            ca = finances.get("ca")
            if ca and ca > 1_000_000:
                return 18, f"Société très active (CA {ca:,.0f}€) — peu de pression financière"
        return 12, "Société active — situation financière inconnue, score neutre"

    return 12, f"Statut '{statut}' non interprété"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch Annuaire Entreprises (Kerelia)")
    parser.add_argument("--siren", required=True, help="SIREN (9 chiffres)")
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Sortie brute JSON (réponse Annuaire) et rien d'autre",
    )
    args = parser.parse_args()

    data = fetch_annuaire(args.siren)
    # En pratique fetch_annuaire est déjà « brut ». Le flag --raw force juste l'output JSON.
    if args.raw:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        sys.exit(0)

    # Mode normal : afficher aussi les scorings de base (utile en debug)
    s1, n1 = scorer_axe1(data) if data else (None, "Introuvable")
    s2, n2 = scorer_axe2(data) if data else (None, "Introuvable")
    s3, n3 = scorer_axe3_base(data) if data else (None, "Introuvable")
    print(json.dumps({
        "annuaire": data,
        "scoring": {
            "axe1": {"score": s1, "note": n1},
            "axe2": {"score": s2, "note": n2},
            "axe3_base": {"score": s3, "note": n3},
        }
    }, ensure_ascii=False, indent=2))