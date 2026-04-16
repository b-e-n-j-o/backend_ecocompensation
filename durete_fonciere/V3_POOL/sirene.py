# -*- coding: utf-8 -*-
"""
sirene.py — Module INSEE Sirene
Récupère les données Sirene d'une unité légale par SIREN.

Apport spécifique vs Annuaire Entreprises :
- trancheEffectifsUniteLegale : "NN" / "00" / "01"... (plus fiable que caractereEmployeur)
- nombrePeriodesUniteLegale   : signal de stabilité / tension interne
- periodesUniteLegale         : historique complet des modifications

Usage standalone :
    python sirene.py --siren 892632365
"""

import os
import argparse
import logging
import requests

log = logging.getLogger("sirene")

# ============================================================
# CONFIG
# ============================================================
BASE_URL = "https://api.insee.fr/api-sirene/3.11"

TRANCHES_EFFECTIFS = {
    "NN": "Unité non employeuse (jamais de salarié)",
    "00": "0 salarié au 31/12 (a employé dans l'année)",
    "01": "1-2 salariés",
    "02": "3-5 salariés",
    "03": "6-9 salariés",
    "11": "10-19 salariés",
    "12": "20-49 salariés",
    "21": "50-99 salariés",
    "22": "100-199 salariés",
    "31": "200-249 salariés",
    "32": "250-499 salariés",
    "41": "500-999 salariés",
    "42": "1 000-1 999 salariés",
}


# ============================================================
# FETCH
# ============================================================
def fetch_sirene_raw(siren: str, api_key: str = None) -> dict:
    """Retourne la réponse brute de l'API Sirene pour un SIREN."""
    key = api_key or os.environ.get("INSEE_API_KEY", "")
    if not key:
        raise ValueError("INSEE_API_KEY manquante — set env var ou passe api_key=")

    r = requests.get(
        f"{BASE_URL}/siren/{siren}",
        headers={"X-INSEE-Api-Key-Integration": key},
        timeout=15,
    )

    if r.status_code == 404:
        log.warning(f"Sirene : SIREN {siren} introuvable")
        return {}
    if r.status_code == 403:
        log.warning(f"Sirene : accès refusé (unité à diffusion restreinte)")
        return {}

    r.raise_for_status()
    return r.json().get("uniteLegale", {})


def fetch_sirene(siren: str, api_key: str = None) -> dict:
    """
    Retourne un dict normalisé avec les champs utiles pour le scoring.
    Ne lève jamais d'exception — retourne {} si indisponible.
    """
    try:
        raw = fetch_sirene_raw(siren, api_key)
        if not raw:
            return {}

        # Dernière période = état actuel
        periodes = raw.get("periodesUniteLegale", [])
        derniere = periodes[0] if periodes else {}

        tranche = raw.get("trancheEffectifsUniteLegale")

        return {
            # Effectifs — clé pour la surcharge bail rural
            "trancheEffectifsUniteLegale":      tranche,
            "tranche_label":                    TRANCHES_EFFECTIFS.get(tranche, tranche or "NC"),
            "anneeEffectifsUniteLegale":        raw.get("anneeEffectifsUniteLegale"),

            # Stabilité structurelle — signal Axe 2
            "nombrePeriodesUniteLegale":        raw.get("nombrePeriodesUniteLegale", 1),
            "dateDernierTraitement":            raw.get("dateDernierTraitementUniteLegale"),

            # Confirmation identité (cross-check Annuaire)
            "denomination":                     derniere.get("denominationUniteLegale"),
            "categorieJuridique":               derniere.get("categorieJuridiqueUniteLegale"),
            "etatAdministratif":                derniere.get("etatAdministratifUniteLegale"),
            "activitePrincipale":               derniere.get("activitePrincipaleUniteLegale"),
            "caractereEmployeur":               derniere.get("caractereEmployeurUniteLegale"),

            # Historique brut (pour Gemini / rapport narratif)
            "periodes":                         periodes,
        }

    except Exception as e:
        log.warning(f"Sirene indisponible pour {siren} : {e}")
        return {}


# ============================================================
# LOGIQUE SCORING — utilisée par rpg.py / scoring.py
# ============================================================
def est_zero_salarie(sirene: dict, ann: dict = None) -> bool:
    """
    Détermine si la PM est non employeuse.
    Priorité : Sirene (trancheEffectifs) > Annuaire (caractereEmployeur).

    NN  → jamais employeur → True  (bail rural très probable)
    00  → a employé dans l'année mais plus au 31/12 → True (bail rural possible)
    None/absent → fallback Annuaire
    01+ → a des salariés → False
    """
    tranche = sirene.get("trancheEffectifsUniteLegale") if sirene else None

    if tranche == "NN":
        return True
    if tranche == "00":
        return True  # prudent : on considère non-employeur pour le bail
    if tranche and tranche not in ("NN", "00"):
        return False  # a des salariés déclarés

    # Fallback Annuaire si Sirene indisponible
    if ann:
        return ann.get("caractereEmployeur") in ("N", None, "")

    return True  # inconnu → on assume non-employeur (conservateur)


def signal_stabilite(sirene: dict) -> tuple[int, str]:
    """
    Retourne (delta_score_axe2, note) basé sur l'historique des périodes.

    0 modification  → structure stable, pas de surcharge
    1-2 modifications → neutre
    3+ modifications  → légère surcharge complexité (+2 pts Axe 2)
    """
    nb = sirene.get("nombrePeriodesUniteLegale", 1) if sirene else 1

    if nb == 1:
        return 0, "Structure stable — aucune modification depuis création"
    elif nb <= 2:
        return 0, f"{nb} période(s) — modifications mineures"
    else:
        return 2, f"{nb} périodes — restructurations multiples, complexité accrue"


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Données Sirene INSEE par SIREN")
    parser.add_argument("--siren", required=True)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--raw", action="store_true", help="Sortie brute JSON (uniteLegale) et arrêt")
    args = parser.parse_args()

    key = args.api_key or os.environ.get("INSEE_API_KEY", "47d2b5e8-5ce6-4339-92b5-e85ce653394c")
    if args.raw:
        import json
        raw = fetch_sirene_raw(args.siren, api_key=key)
        print(json.dumps(raw, ensure_ascii=False, indent=2))
        raise SystemExit(0)

    data = fetch_sirene(args.siren, api_key=key)

    if not data:
        print(f"⚠️  Aucune donnée Sirene pour {args.siren}")
    else:
        print(f"\n{'='*55}")
        print(f"  SIRENE — {args.siren}")
        print(f"{'='*55}")
        print(f"  Dénomination       : {data.get('denomination')}")
        print(f"  Catégorie jur.     : {data.get('categorieJuridique')}")
        print(f"  État admin.        : {data.get('etatAdministratif')}")
        print(f"  NAF                : {data.get('activitePrincipale')}")
        print(f"  Tranche effectifs  : {data.get('trancheEffectifsUniteLegale')} — {data.get('tranche_label')}")
        print(f"  Année effectifs    : {data.get('anneeEffectifsUniteLegale') or 'non renseignée'}")
        print(f"  Nb périodes        : {data.get('nombrePeriodesUniteLegale')}")
        print(f"  Dernier MAJ        : {data.get('dateDernierTraitement')}")

        zero_sal = est_zero_salarie(data)
        delta, note_stab = signal_stabilite(data)
        print(f"\n  → Zero salarié     : {'✅ OUI' if zero_sal else '❌ NON'}")
        print(f"  → Stabilité        : {note_stab} (delta axe2 = +{delta})")

        periodes = data.get("periodes", [])
        print(f"\n  Historique ({len(periodes)} période(s)) :")
        for p in periodes:
            fin = p.get("dateFin") or "en cours"
            print(f"    {p['dateDebut']} → {fin} | NAF={p.get('activitePrincipaleUniteLegale')} | catJur={p.get('categorieJuridiqueUniteLegale')}")
        print()