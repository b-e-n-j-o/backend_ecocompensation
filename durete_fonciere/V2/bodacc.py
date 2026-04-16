"""
Module BODACC — Kerelia Dureté Foncière
========================================
Source : https://bodacc-datadila.opendatasoft.com
Gratuit, sans clé.

Expose :
    fetch_bodacc(siren)          -> dict   # résumé structuré
    scorer_axe3_bodacc(bodacc)   -> (int, str)
    scorer_axe3_complet(annuaire, bodacc) -> (int, str)
"""

import requests
import json
import logging
import argparse
import sys
from typing import Optional

log = logging.getLogger("bodacc")

BASE = "https://bodacc-datadila.opendatasoft.com/api/records/1.0/search"
DATASET = "annonces-commerciales"


def fetch_bodacc_raw(siren: str) -> dict:
    """
    Renvoie la réponse brute JSON de l'API BODACC (opendatasoft).
    Utile en mode --raw (orchestration).
    """
    siren_clean = siren.replace(" ", "")
    try:
        r = requests.get(BASE, params={
            "dataset": DATASET,
            "q": siren_clean,
            "rows": 50,
            "sort": "dateparution",
        }, timeout=10)
        payload = None
        try:
            payload = r.json()
        except Exception:
            payload = None
        return {
            "http_status": r.status_code,
            "final_url": r.url,
            "json": payload,
            "text": None if payload is not None else (r.text or ""),
        }
    except requests.exceptions.Timeout:
        return {"http_status": None, "final_url": None, "json": None, "text": "Timeout BODACC"}
    except Exception as e:
        return {"http_status": None, "final_url": None, "json": None, "text": f"Erreur BODACC: {e}"}


def fetch_bodacc(siren: str) -> dict:
    """
    Interroge BODACC pour un SIREN donné.
    Retourne un dict structuré avec les infos utiles au scoring.
    """
    siren_clean = siren.replace(" ", "")
    result = {
        "siren": siren_clean,
        "nb_annonces": 0,
        "procedure_collective": False,
        "type_procedure": None,
        "capital_social": None,
        "date_creation_rcs": None,
        "statut_bodacc": None,    # "creation" / "modification" / "radiation"
        "associes_bodacc": [],    # liste des associés depuis annonce création
        "annonces_raw": [],
    }

    try:
        r = requests.get(BASE, params={
            "dataset": DATASET,
            "q": siren_clean,
            "rows": 50,
            "sort": "dateparution",
        }, timeout=10)

        if r.status_code != 200:
            log.error(f"BODACC HTTP {r.status_code}")
            return result

        data = r.json()
        records = data.get("records", [])
        result["nb_annonces"] = data.get("nhits", 0)
        log.info(f"BODACC : {result['nb_annonces']} annonce(s) trouvée(s)")

        for rec in records:
            f = rec.get("fields", {})
            famille = f.get("familleavis", "")
            result["annonces_raw"].append({
                "date": f.get("dateparution"),
                "famille": f.get("familleavis_lib"),
                "type": f.get("typeavis_lib"),
            })

            # Procédures collectives
            if famille in ("liquidation", "redressement", "sauvegarde"):
                result["procedure_collective"] = True
                result["type_procedure"] = famille
                log.warning(f"BODACC : procédure collective '{famille}' détectée")

            # Annonce de création → capital + associés
            if famille == "creation":
                result["statut_bodacc"] = "creation"
                result["date_creation_rcs"] = f.get("dateparution")

                # Parse listepersonnes (JSON embarqué dans string)
                lp_raw = f.get("listepersonnes", "")
                if lp_raw:
                    try:
                        lp = json.loads(lp_raw)
                        personne = lp.get("personne", {})

                        # Capital social
                        capital = personne.get("capital", {})
                        if capital.get("montantCapital"):
                            try:
                                result["capital_social"] = float(
                                    capital["montantCapital"].replace(",", ".")
                                )
                            except (ValueError, AttributeError):
                                pass

                        # Associés depuis champ "administration"
                        admin = personne.get("administration", "")
                        if admin:
                            result["associes_bodacc"] = _parser_associes(admin)

                    except (json.JSONDecodeError, AttributeError):
                        pass

            # Radiation
            if famille == "radiation":
                result["statut_bodacc"] = "radiation"

        log.info(
            f"BODACC résumé : capital={result['capital_social']}€, "
            f"procédure={result['procedure_collective']}, "
            f"nb_associés_bodacc={len(result['associes_bodacc'])}"
        )
        return result

    except requests.exceptions.Timeout:
        log.error("Timeout BODACC")
        return result
    except Exception as e:
        log.error(f"Erreur BODACC : {e}")
        return result


def _parser_associes(administration: str) -> list[dict]:
    """
    Parse le champ 'administration' de l'annonce BODACC.
    Format : "Gérant Associé : Nom, Prénom, Associé : Nom2, Prénom2, ..."
    """
    import re
    associes = []
    # Split sur les virgules et chercher les patterns Nom/qualité
    parties = [p.strip() for p in administration.split(",")]
    i = 0
    while i < len(parties):
        part = parties[i]
        # Chercher une qualité connue
        qualite = None
        for q in ["Gérant Associé", "Gérant", "Associé", "Président", "Directeur"]:
            if q.lower() in part.lower():
                qualite = q
                break
        if qualite and ":" in part:
            nom_part = part.split(":", 1)[1].strip()
            # Le prénom est souvent dans la partie suivante
            prenom = parties[i+1].strip() if i+1 < len(parties) else ""
            associes.append({"qualite": qualite, "nom": nom_part, "prenom": prenom})
            i += 2
        else:
            i += 1
    return associes


def scorer_axe3_bodacc(bodacc: dict) -> tuple[int, str]:
    """Score Axe 3 basé sur BODACC seul (procédures + statut)."""
    if bodacc.get("procedure_collective"):
        t = bodacc.get("type_procedure", "").lower()
        if "liquidation" in t:
            return 0, "Liquidation judiciaire — vente obligatoire (BODACC)"
        elif "redressement" in t:
            return 3, "Redressement judiciaire — cession probable (BODACC)"
        elif "sauvegarde" in t:
            return 5, "Procédure de sauvegarde (BODACC)"
        return 4, "Procédure collective (type non précisé)"

    if bodacc.get("statut_bodacc") == "radiation":
        return 2, "Société radiée (BODACC) — actif résiduel"

    return None, "Aucune procédure collective détectée (BODACC)"


def scorer_axe3_complet(annuaire: dict, bodacc: dict) -> tuple[int, str]:
    """
    Axe 3 complet : combine statut Annuaire + procédures BODACC + capital BODACC.
    """
    # Procédure collective → priorité absolue
    score_proc, note_proc = scorer_axe3_bodacc(bodacc)
    if score_proc is not None:
        return score_proc, note_proc

    statut = annuaire.get("etat_administratif", "").upper()
    capital = bodacc.get("capital_social")

    if statut in ("C", "F"):
        return 3, "Société inactive/cessée — actif dormant, réceptifs à une offre"

    # Actif : affinage par capital
    if statut == "A":
        if capital is not None:
            if capital >= 500_000:
                return 18, f"Capital élevé ({capital:,.0f}€) — aucune pression financière"
            elif capital >= 100_000:
                return 14, f"Capital moyen ({capital:,.0f}€) — situation stable"
            elif capital > 0:
                return 9, f"Faible capital ({capital:,.0f}€) — légère pression possible"
        return 12, "Société active, capital inconnu — score neutre"

    return 12, f"Statut '{statut}' — score neutre"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch BODACC (Kerelia)")
    parser.add_argument("--siren", required=True, help="SIREN (9 chiffres)")
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Sortie brute JSON (réponse API BODACC) et rien d'autre",
    )
    args = parser.parse_args()

    if args.raw:
        print(json.dumps(fetch_bodacc_raw(args.siren), ensure_ascii=False, indent=2))
        sys.exit(0)

    # Mode normal : sortie structurée (résumé utile au scoring)
    print(json.dumps(fetch_bodacc(args.siren), ensure_ascii=False, indent=2))