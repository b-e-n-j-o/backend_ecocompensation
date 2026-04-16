# -*- coding: utf-8 -*-
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
from pathlib import Path
from typing import Optional

log = logging.getLogger("annuaire")

# ---------------------------------------------------------------------------
# Chargement nomenclatures (avec cache)
# ---------------------------------------------------------------------------

_NOM_N2: Optional[dict] = None  # code 2 chiffres -> libelle + score + note
_NOM_N3: Optional[dict] = None  # code 4 chiffres -> libelle


def _charger_nomenclatures():
    global _NOM_N2, _NOM_N3
    if _NOM_N2 is not None:
        return

    base = Path(__file__).parent

    p2 = base / "categories_juridiques_niveau_2.json"
    if p2.exists():
        with open(p2, encoding="utf-8") as f:
            entries = json.load(f)
        _NOM_N2 = {
            str(e["code_juridique"]).zfill(2): {
                "libelle": e["categorie_juridique"],
                "score_axe1": e.get("score_axe1", 20),
                "note": e.get("note_scoring", ""),
            }
            for e in entries
        }
    else:
        log.warning("categories_juridiques_niveau_2.json introuvable")
        _NOM_N2 = {}

    p3 = base / "categories_juridiques_niveau_3.json"
    if p3.exists():
        with open(p3, encoding="utf-8") as f:
            _NOM_N3 = {
                str(e["code_juridique"]).zfill(4): e["categorie_juridique"]
                for e in json.load(f)
            }
    else:
        log.warning("categories_juridiques_niveau_3.json introuvable")
        _NOM_N3 = {}


def get_libelle_forme_juridique(code4: str) -> str:
    """
    Retourne le libelle precis (niveau 3/3) d'un code juridique a 4 chiffres.
    Fallback sur le libelle niveau 2/3 si code 4 inconnu.
    """
    _charger_nomenclatures()
    code4 = str(code4).strip().zfill(4)
    code2 = code4[:2]

    if code4 in _NOM_N3:
        return _NOM_N3[code4]
    if code2 in _NOM_N2:
        return f"{_NOM_N2[code2]['libelle']} (code {code4})"
    return f"Forme juridique inconnue (code {code4})"


# ---------------------------------------------------------------------------
# Surcharges niveau 3/3 - cas ou la nuance est forte dans une meme famille
# ---------------------------------------------------------------------------

AXE1_CODE_4: dict[str, tuple[int, str]] = {
    # Societes civiles (famille 65)
    "6515": (10, "SCI - structure dediee gestion immobiliere, cession sous accord associes"),
    "6516": (10, "SCI (variante)"),
    "6540": (10, "Societe civile immobiliere"),
    "6541": (12, "SCI construction-vente - logique promoteur, vente si projet abandonne"),
    "6534": (14, "GFA - Groupement Foncier Agricole, attachement patrimonial fort"),
    "6535": (14, "Groupement agricole foncier"),
    "6533": (16, "GAEC - exploitation en commun, decision collegiale exploitants"),
    "6536": (22, "Groupement forestier - patrimoine long terme, vente rare"),
    "6537": (20, "Groupement pastoral - attachement fort a l'usage collectif"),
    "6538": (16, "Groupement foncier et rural"),
    "6597": (16, "Societe civile d'exploitation agricole - attachement a l'outil de travail"),
    "6598": (16, "EARL - exploitation agricole"),
    "6521": (14, "SCPI - logique financiere pure, vente si rendement atteint"),
    "6539": (12, "Societe civile fonciere"),
    # Etablissements publics (famille 73)
    "7210": (37, "Commune - domaine public, vote conseil municipal"),
    "7220": (38, "Departement - domaine public"),
    "7230": (38, "Region - domaine public"),
    "7346": (35, "Communaute de communes - deliberation obligatoire"),
    "7343": (35, "Communaute urbaine - deliberation obligatoire"),
    "7344": (35, "Metropole - deliberation obligatoire"),
    "7348": (35, "Communaute d'agglomeration - deliberation obligatoire"),
    "7364": (33, "Etablissement d'hospitalisation - patrimoine affecte soin"),
    "7371": (30, "Office public HLM - vente encadree loi ELAN"),
    # SAFER
    "5430": (20, "SAFER SARL - peut ceder a projets environnementaux"),
    "5530": (20, "SAFER SA - idem"),
    # Associations/fondations specialisees
    "9230": (32, "Association reconnue d'utilite publique - patrimoine lie objet social"),
    "9300": (30, "Fondation reconnue - approbation autorite de tutelle requise"),
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
    """
    Score Axe 1 - Nature de la PM - /40.

    Cascade :
      1. Code 4 chiffres (AXE1_CODE_4) - nuances fortes connues
      2. Code 2 chiffres lu depuis categories_juridiques_niveau_2.json
      3. Fallback neutre 20/40
    """
    _charger_nomenclatures()

    code4 = str(data.get("nature_juridique", "")).strip().zfill(4)
    code2 = code4[:2]
    libelle_n3 = get_libelle_forme_juridique(code4)

    if code4 in AXE1_CODE_4:
        score, raison = AXE1_CODE_4[code4]
        return score, f"{libelle_n3} - {raison}"

    if code2 in _NOM_N2:
        entry = _NOM_N2[code2]
        score = entry["score_axe1"]
        note = entry["note"]
        return score, f"{libelle_n3} - {note}"

    log.warning(f"Code juridique '{code4}' non mappe - score neutre 20/40")
    return 20, f"{libelle_n3} - code non mappe, score neutre"


def enrichir_contexte_forme_juridique(ann: dict) -> dict:
    """
    Ajoute les libelles N2 et N3 dans le dict annuaire avant envoi a Gemini.
    """
    _charger_nomenclatures()
    code4 = str(ann.get("nature_juridique_code", "")).strip().zfill(4)
    code2 = code4[:2]

    ann["nature_juridique_libelle_n3"] = _NOM_N3.get(code4, f"code {code4} inconnu")
    ann["nature_juridique_libelle_n2"] = _NOM_N2.get(code2, {}).get(
        "libelle", f"famille {code2} inconnue"
    )
    return ann


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