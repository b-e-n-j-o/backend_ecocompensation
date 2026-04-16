"""
Module Rapport — Kerelia Dureté Foncière
=========================================
Génère un rapport narratif complet via Gemini.
Gemini reçoit toutes les données brutes + le guide méthodologique
et produit lui-même l'analyse et les scores.

Usage :
    python rapport.py --siren 892632365 --idus 86275000D0319
    python rapport.py --siren 892632365 --idus 86275000D0319 --output rapport.md
"""

import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path
from dotenv import load_dotenv

# Modules Kerelia
from annuaire import fetch_annuaire, fetch_annuaire_raw
from supabase_parcelles import fetch_parcelles_by_siren
from bodacc   import fetch_bodacc, fetch_bodacc_raw
from dvf      import lookup_dvf, lookup_dvf_raw
from rpg      import fetch_rpg_parcelle
from rpg      import _load_nomenclature
from sirene   import fetch_sirene
from urba     import run_urba_multi

load_dotenv()

log = logging.getLogger("rapport")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

# ---------------------------------------------------------------------------
# Guide méthodologique (extrait du PDF de consignes)
# Embarqué directement dans le prompt pour que Gemini puisse s'y référer
# ---------------------------------------------------------------------------

GUIDE_METHODOLOGIQUE = """
# GUIDE DE SCORING — DURETÉ FONCIÈRE DES PERSONNES MORALES (KERELIA / SNCRR)

## Objectif
Évaluer la difficulté d'acquisition d'un terrain identifié comme gisement potentiel
de compensation écologique, en fonction de la nature de son propriétaire (personne morale).
Score de 1 (acquisition très facile) à 100 (acquisition quasi impossible).

## Architecture du score (100 points)

### AXE 1 — Nature de la Personne Morale (40 points)
Le facteur le plus déterminant. Grille indicative :
- État français : 40/40 — inaliénabilité absolue (art. L.3111-1 CGP)
- Collectivités territoriales (communes, dép., régions) : 37/40 — domaine public + procédure politique
- EPCI, syndicats intercommunaux : 35/40
- Établissements publics (santé, enseignement) : 33/40 — patrimoine affecté mission service public
- Organismes HLM : 30/40 — vente très encadrée loi ELAN
- Établissements publics fonciers (EPF) : 28/40 — mission portage foncier, pas cession privée
- Structures environnementales (CEN, PNR, Conservatoire du littoral) : 38/40 — mission conservation
- SAFER : 20/40 — droit de préemption mais peut céder à projets environnementaux
- Concessionnaires réseau (SNCF, EDF, VNF) : 32/40 — patrimoine affecté exploitation réseau
- Grandes entreprises industrielles/commerciales (SA, grandes SARL) : 20/40 — comités d'investissement lourds
- Investisseurs professionnels (fonds, SCPI, assurances) : 15/40 — logique financière pure, vente si rendement atteint
- Promoteurs/aménageurs : 18/40 — vente si projet initial abandonné
- Sociétés commerciales classiques (SARL, SAS, SA PME) : 18/40 — décision rationnelle basée valeur actif
- Structures agricoles (GFA, GAEC, exploitants) : 14-20/40 — attachement fort à la terre, mais ORE/bail rural environnemental possible
- Groupements forestiers : 22/40 — patrimoine long terme, vente rare
- SCI familiale : 10/40 — structure dédiée gestion immobilière, vente courante sous réserve accord associés
- Associations/fondations : 26/40 — gouvernance associative, patrimoine lié objet social
- Établissements militaires/défense : 40/40 — même régime que l'État, déclassement par décret

### AXE 2 — Gouvernance et Complexité Décisionnelle (25 points)
En droit des sociétés civiles, la vente d'un immeuble dépasse les pouvoirs du gérant
et nécessite en principe l'unanimité des associés (art. 1852 Code civil). Grille :
- Copropriété ou BND (biens non délimités) : 25/25
- Indivision entre personnes morales : 22/25
- SCI avec plus de 5 associés : 20/25
- SA avec conseil d'administration : 18/25
- SCI avec 2 à 5 associés : 15/25
- SARL/SAS avec pluralité d'associés : 7/25
- Société à associé unique (EURL, SASU) : 2/25
- Personne morale publique (décision politique) : 20/25

### AXE 3 — Situation Financière et Statut Juridique (20 points)
La pression financière est un levier d'acquisition majeur. Grille :
- Société très rentable, forte capitalisation : 20/20 — aucune pression
- Société à l'équilibre, activité stable : 15/20 — vente envisageable si offre supérieure valeur comptable
- Société déficitaire mais active : 8/20 — terrain devient levier trésorerie
- Société inactive ou en sommeil : 3/20 — actif dormant, dirigeants réceptifs
- Procédure de sauvegarde : 5/20
- Redressement judiciaire : 3/20
- Liquidation judiciaire : 0/20 — vente obligatoire, opportunité maximale

### AXE 4 — Comportement Patrimonial et Ancienneté (15 points)
Grille basée sur la date d'acquisition reconstituée via DVF :
- Acquisition très récente (< 3 ans) : 15/15 — projet probablement en cours
- Détention courte (3-5 ans) : 12/15
- Détention moyenne (5-15 ans) : 8/15 — réceptif si terrain sans revenu
- Détention longue (15-30 ans) : 8/15 — projet initial abandonné
- Très longue détention (> 30 ans) : 12/15 — attachement fort ou actif oublié
- PM ayant réalisé des ventes récentes (< 5 ans) : 2/15 — dynamique active de cession
- PM en dissolution/liquidation amiable : 0/15 — tous actifs à céder

### RÈGLES DE SURCHARGE (s'ajoutent au score de base)
- Terrain dans domaine public (voirie, cours d'eau) : score forcé à 100
- Bail rural ou emphytéotique en cours : +15 points
- Obligation Réelle Environnementale (ORE) : -10 points (signal favorable)
- PM identifiée comme opérateur de compensation (CDC Biodiversité) : score forcé à 95
- DIA récente (Déclaration Intention Aliéner) : -20 points (intention de vendre confirmée)

## Grille d'interprétation finale
- 0-20 : Opportunité exceptionnelle → action immédiate
- 21-40 : Dureté faible → cible prioritaire, prospection directe
- 41-60 : Dureté modérée → cible secondaire, négociation 6-18 mois
- 61-80 : Dureté forte → veille foncière, cibler si enjeu écologique exceptionnel
- 81-100 : Dureté rédhibitoire → exclure du portefeuille

## Contexte opérationnel KERELIA / SNCRR
Les terrains ciblés sont en zones agricoles, naturelles ou forestières, destinés à la
compensation écologique (Sites Naturels de Compensation, Restauration et Renaturation).
Le scoring est complémentaire aux paramètres d'urbanisme et d'environnement traités
par ailleurs. L'enjeu est de prioriser la prospection et d'allouer efficacement les ressources
de négociation sur les terrains les plus mobilisables.
"""


# ---------------------------------------------------------------------------
# Collecte des données brutes
# ---------------------------------------------------------------------------

def collecter_donnees_brutes(siren: str, idus: list[str], avec_rpg: bool = True) -> dict:
    """
    Collecte toutes les données brutes depuis les 4 sources.
    Retourne un dict riche destiné à être passé à Gemini.
    """
    log.info(f"Collecte données brutes — SIREN={siren}, IDUs={idus}")

    contexte = {
        "siren": siren,
        "idus": idus,
        "date_analyse": date.today().isoformat(),
        "annuaire": {},
        "annuaire_raw": {},
        "sirene": {},
        "bodacc": {},
        "parcelles": [],
        "avertissements_collecte": [],
    }

    # Étape 0 : enrichissement Supabase si IDUs fournis manuellement
    # (ou récupération auto si non fournis)
    parcelles_meta = fetch_parcelles_by_siren(siren)
    if parcelles_meta:
        if not idus:
            idus = parcelles_meta.get("idus", [])
            contexte["idus"] = idus
        contexte["patrimoine_foncier"] = {
            "nb_parcelles_total":  parcelles_meta.get("nb_parcelles"),
            "surface_totale_m2":   parcelles_meta.get("surface_totale_m2"),
            "surface_totale_ha":   parcelles_meta.get("surface_totale_ha"),
            "departements":        parcelles_meta.get("departements"),
            "communes_insee":      parcelles_meta.get("communes_insee"),
            "parcelles_detail":    parcelles_meta.get("parcelles_detail", []),
        }
        log.info(
            f"Supabase : {parcelles_meta['nb_parcelles']} parcelles, "
            f"{parcelles_meta['surface_totale_ha']} ha"
        )

    # 1. Annuaire Entreprises
    log.info("Annuaire Entreprises ...")
    ann = fetch_annuaire(siren)
    if ann:
        contexte["annuaire_raw"] = ann
        contexte["annuaire"] = {
            "denomination":         ann.get("nom_complet"),
            "siren":                ann.get("siren"),
            "nature_juridique_code": ann.get("nature_juridique"),
            "statut":               ann.get("etat_administratif"),
            "naf":                  ann.get("activite_principale"),
            "naf_libelle":          ann.get("siege", {}).get("activite_principale"),
            "date_creation":        ann.get("date_creation"),
            "date_fermeture":       ann.get("date_fermeture"),
            "adresse":              ann.get("siege", {}).get("adresse"),
            "commune":              ann.get("siege", {}).get("libelle_commune"),
            "departement":          ann.get("siege", {}).get("departement"),
            "region":               ann.get("siege", {}).get("region"),
            "caractere_employeur":  ann.get("siege", {}).get("caractere_employeur"),
            "nb_etablissements":    ann.get("nombre_etablissements_ouverts"),
            "est_service_public":   ann.get("complements", {}).get("est_service_public"),
            "est_association":      ann.get("complements", {}).get("est_association"),
            "est_ess":              ann.get("complements", {}).get("est_ess"),
            "dirigeants": [
                {
                    "nom":     d.get("nom"),
                    "prenom":  d.get("prenoms", d.get("prenom")),
                    "qualite": d.get("qualite"),
                    "annee_naissance": d.get("annee_de_naissance"),
                    "nationalite": d.get("nationalite"),
                    "type":    d.get("type_dirigeant"),
                }
                for d in ann.get("dirigeants", [])
            ],
        }
    else:
        contexte["avertissements_collecte"].append("Annuaire Entreprises : SIREN introuvable")

    # 1bis. Sirene INSEE (optionnel) — effectifs/stabilité
    # (ne lève pas d'exception : fetch_sirene() renvoie {} si indisponible)
    try:
        contexte["sirene"] = fetch_sirene(siren) or {}
    except Exception as e:
        contexte["sirene"] = {}
        contexte["avertissements_collecte"].append(f"Sirene INSEE indisponible : {e}")

    # 2. BODACC
    log.info("BODACC ...")
    bod = fetch_bodacc(siren)
    contexte["bodacc"] = {
        "nb_annonces":          bod.get("nb_annonces"),
        "procedure_collective": bod.get("procedure_collective"),
        "type_procedure":       bod.get("type_procedure"),
        "capital_social_eur":   bod.get("capital_social"),
        "date_creation_rcs":    bod.get("date_creation_rcs"),
        "statut_bodacc":        bod.get("statut_bodacc"),
        "associes_bodacc":      bod.get("associes_bodacc"),
        "annonces":             bod.get("annonces_raw"),
    }

    # 3. DVF + RPG par parcelle
    nom = _load_nomenclature()
    date_creation_pm = contexte["annuaire"].get("date_creation")

    for idu in idus:
        log.info(f"Parcelle {idu} ...")
        parcelle_ctx = {"idu": idu}

        # DVF
        dvf = lookup_dvf(idu, date_creation_pm=date_creation_pm)
        parcelle_ctx["dvf"] = {
            "nb_mutations":      dvf.nb_mutations,
            "statut":            dvf.statut.value if dvf.statut else None,
            "date_acquisition":  dvf.date_acquisition,
            "valeur_acquisition_eur": dvf.valeur_acquisition,
            "nature_acquisition": dvf.nature_acquisition,
            "nature_culture":    dvf.nature_culture,
            "fiabilite_jdatat":  dvf.fiabilite_jdatat,
            "signal":            dvf.signal,
            "avertissements":    dvf.avertissements,
            "mutations_detail":  [
                {
                    "date":    m.date_mutation,
                    "nature":  m.nature_mutation,
                    "valeur":  m.valeur_fonciere,
                    "surface": m.surface_terrain,
                    "culture": m.nature_culture,
                    "commune": m.nom_commune,
                }
                for m in dvf.mutations
            ],
        }

        # RPG
        if avec_rpg:
            try:
                insee, section, numero = _decompose_idu_from_idu(idu)
                rpg = fetch_rpg_parcelle(insee, section, numero)
                summary = rpg.get("summary", {})
                by_year = rpg.get("by_year", {})

                # Construire historique lisible avec libellés
                historique_rpg = {}
                for year, data in by_year.items():
                    if data.get("status") == "agricole":
                        cultures_libelles = {
                            code: nom["code_cultu"].get(code, nom["culture_d1"].get(code, code))
                            for code in data.get("cultures", {})
                        }
                        historique_rpg[str(year)] = {
                            "status": "agricole",
                            "surface_m2": data.get("total_m2"),
                            "cultures": cultures_libelles,
                        }
                    else:
                        historique_rpg[str(year)] = {"status": data.get("status")}

                parcelle_ctx["rpg"] = {
                    "occupation_agricole": summary.get("occupation_agricole"),
                    "nb_annees_agricoles": summary.get("nb_annees_agricoles"),
                    "annees_agricoles":    summary.get("annees_agricoles"),
                    "continuite_forte":    summary.get("continuite_forte"),
                    "bail_rural_certain":  summary.get("bail_rural_certain"),
                    "bail_rural_probable": summary.get("bail_rural_probable"),
                    "codes_cultures":      summary.get("codes_cultures"),
                    "by_year_raw":         by_year,
                    "historique_par_annee": historique_rpg,
                }
            except Exception as e:
                log.error(f"RPG erreur {idu} : {e}")
                parcelle_ctx["rpg"] = {"erreur": str(e)}

        contexte["parcelles"].append(parcelle_ctx)

    # Zonage PLU (union des parcelles) — utilisé par la carte d'identité PDF
    if idus:
        try:
            log.info("Zonage PLU (urba) ...")
            contexte["urba"] = run_urba_multi(idus, verbose=False)
        except Exception as e:
            log.warning("Urba indisponible : %s", e)
            contexte["avertissements_collecte"].append(f"Zonage PLU : {e}")
            contexte["urba"] = {}

    return contexte


def collecter_donnees_raw(siren: str, idus: list[str]) -> dict:
    """
    Mode brut : renvoie uniquement les sorties JSON brutes obtenues par appels API
    pour Annuaire / BODACC / DVF. (RPG volontairement exclu ici.)
    """
    siren = siren.strip()
    return {
        "siren": siren,
        "idus": idus,
        "date_analyse": date.today().isoformat(),
        "annuaire_raw": fetch_annuaire_raw(siren),
        "sirene": fetch_sirene(siren),
        "bodacc_raw": fetch_bodacc_raw(siren),
        "dvf_raw": {idu: lookup_dvf_raw(idu) for idu in idus},
    }


# ---------------------------------------------------------------------------
# Construction du prompt Gemini
# ---------------------------------------------------------------------------

def construire_prompt(contexte: dict) -> str:
    ctx_json = json.dumps(contexte, indent=2, ensure_ascii=False)

    return f"""Tu es un expert en prospection foncière spécialisé dans la compensation écologique (SNCRR) pour le bureau d'études Kerelia.

Ta mission est de produire un rapport d'analyse complet de la dureté foncière d'une personne morale propriétaire de parcelles agricoles ou naturelles, en vue d'une potentielle acquisition pour compensation écologique.

---

## GUIDE MÉTHODOLOGIQUE DE SCORING

{GUIDE_METHODOLOGIQUE}

---

## DONNÉES COLLECTÉES

Voici l'ensemble des données brutes disponibles sur cette personne morale et ses parcelles :

```json
{ctx_json}
```

---

## INSTRUCTIONS DE RÉDACTION

Produis un rapport structuré en français, rédigé comme un document professionnel destiné à une équipe de prospection foncière. Le rapport doit :

1. **Être narratif et analytique**, pas juste une liste de données. Chaque axe doit faire l'objet d'un paragraphe d'analyse rédigé, qui contextualise les données, explique leur signification juridique et pratique, et tire des conclusions sur la facilité ou la difficulté d'acquisition.

2. **Établir un score pour chaque axe** selon le guide méthodologique, en justifiant précisément chaque note retenue. Ne pas se contenter de recopier les données — interpréter leur signification pour le scoring.

3. **Intégrer les données RPG et DVF** pour enrichir l'analyse patrimoniale et identifier les surcharges applicables (bail rural notamment).

4. **Proposer une stratégie opérationnelle** adaptée au profil spécifique de cette PM — pas un texte générique, mais des recommandations concrètes tenant compte de tous les éléments du dossier (profil familial, attachement agricole, bail rural, âges des associés, etc.).

5. **Mentionner les limites et incertitudes** des données disponibles.

---

## FORMAT DU RAPPORT

IMPORTANT : Rédige les sections 2 à 7 AVANT de calculer le score final.
La synthèse exécutive (section 1) doit être écrite EN DERNIER, après avoir
établi le score dans le tableau (section 8). Tu peux laisser un placeholder
[SCORE] dans la synthèse et le remplir à la fin, mais le score annoncé dans
la synthèse DOIT correspondre exactement au total du tableau section 8.

```
# RAPPORT DE DURETÉ FONCIÈRE
## [Dénomination] — SIREN [SIREN]
Date d'analyse : [date]

---

## SYNTHÈSE EXÉCUTIVE
[3-4 phrases résumant le profil et la conclusion principale — À RÉDIGER APRÈS le calcul du score]

**SCORE FINAL : [SERA COMPLÉTÉ AUTOMATIQUEMENT]/100 — [Niveau de dureté : Opportunité exceptionnelle / Dureté faible / Dureté modérée / Dureté forte / Dureté rédhibitoire]**

⚠ INSTRUCTION CRITIQUE : N'écris PAS de score numérique ici. Indique uniquement le niveau de dureté en texte (ex: Dureté forte). Le score numérique sera extrait automatiquement depuis le tableau section 8.
**Recommandation : [recommandation opérationnelle en 1 phrase]**

---

## 1. IDENTITÉ ET PROFIL DE LA PERSONNE MORALE
[Analyse narrative de la PM, sa nature, son histoire, sa gouvernance]

## 2. AXE 1 — NATURE DE LA PERSONNE MORALE (Score : XX/40)
[Analyse narrative + justification du score]

## 3. AXE 2 — GOUVERNANCE ET COMPLEXITÉ DÉCISIONNELLE (Score : XX/25)
[Analyse narrative + justification du score]

## 4. AXE 3 — SITUATION FINANCIÈRE ET STATUT JURIDIQUE (Score : XX/20)
[Analyse narrative + justification du score]

## 5. AXE 4 — COMPORTEMENT PATRIMONIAL ET ANCIENNETÉ (Score : XX/15)
[Analyse narrative + justification du score, en intégrant les données DVF]

## 6. ANALYSE DU FONCIER DÉTENU — PARCELLES ANALYSÉES
[Pour chaque parcelle : IDU, données DVF (date acq, valeur), historique RPG (cultures, continuité)]

## 7. RÈGLES DE SURCHARGE APPLICABLES
[Identifier et justifier chaque surcharge applicable]

## 8. CALCUL DU SCORE FINAL
| Composante | Score |
|---|---|
| Axe 1 — Nature PM | XX/40 |
| Axe 2 — Gouvernance | XX/25 |
| Axe 3 — Finances | XX/20 |
| Axe 4 — Patrimoine | XX/15 |
| Sous-total | XX/100 |
| Surcharge bail rural | +XX |
| **SCORE FINAL** | **XX/100** |

## 9. STRATÉGIE OPÉRATIONNELLE ET RECOMMANDATIONS
[Recommandations concrètes et contextualisées, leviers envisageables, points de vigilance]

## 10. LIMITES ET DONNÉES MANQUANTES
[Ce qu'on ne sait pas et comment l'obtenir]
```

Rédige maintenant le rapport complet.
"""


# ---------------------------------------------------------------------------
# Appel Gemini
# ---------------------------------------------------------------------------

def generer_rapport_gemini(prompt: str, model: str = "gemini-3.1-flash-lite-preview") -> str:
    """
    Appelle l'API Gemini et retourne le rapport généré.
    """
    try:
        import google.generativeai as genai
    except ModuleNotFoundError as e:
        raise ModuleNotFoundError(
            "Le package 'google-generativeai' n'est pas installé. "
            "Installe-le pour utiliser la génération Gemini, ou utilise --raw/--ctx-only."
        ) from e

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("Variable d'environnement GEMINI_API_KEY non définie")

    genai.configure(api_key=api_key)
    model_client = genai.GenerativeModel(model)

    log.info(f"Appel Gemini ({model}) — prompt {len(prompt):,} chars ...")
    response = model_client.generate_content(
        prompt,
        generation_config=genai.GenerationConfig(
            temperature=0.3,        # peu de créativité, rapport factuel
            max_output_tokens=8192,
        )
    )
    log.info("Gemini OK")
    return response.text


# ---------------------------------------------------------------------------
# Pipeline principal
# ---------------------------------------------------------------------------

def generer_rapport(
    siren:    str,
    idus:     list[str],
    avec_rpg: bool = True,
    model:    str  = "gemini-3.1-flash-lite-preview",
    output:   str  = None,
) -> str:
    """
    Pipeline complet : collecte → prompt → Gemini → rapport.
    """
    # 1. Collecte
    contexte = collecter_donnees_brutes(siren, idus, avec_rpg=avec_rpg)

    # 2. Prompt
    prompt = construire_prompt(contexte)

    # 3. Gemini
    rapport = generer_rapport_gemini(prompt, model=model)

    # 4. Sauvegarde optionnelle
    if output:
        Path(output).write_text(rapport, encoding="utf-8")
        log.info(f"Rapport sauvegardé : {output}")

    return rapport


# ---------------------------------------------------------------------------
# Helpers exposés aux autres modules
# ---------------------------------------------------------------------------

def _decompose_idu_from_idu(idu: str) -> tuple[str, str, str]:
    """Réexporte _decompose_idu depuis rpg.py pour usage dans rapport.py"""
    from scoring import _decompose_idu
    return _decompose_idu(idu)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Rapport narratif dureté foncière via Gemini")
    parser.add_argument("--siren",    required=True)
    parser.add_argument("--idus",     nargs="+", required=True)
    parser.add_argument("--no-rpg",   action="store_true")
    parser.add_argument("--model",    default="gemini-3.1-flash-lite-preview", help="Modèle Gemini")
    parser.add_argument("--output",   default=None, help="Fichier de sortie .md")
    parser.add_argument("--ctx-only", action="store_true", help="Affiche le contexte JSON sans appeler Gemini")
    parser.add_argument("--raw",      action="store_true", help="Sortie brute JSON (Annuaire/BODACC/DVF) et arrêt")
    args = parser.parse_args()

    if args.raw:
        raw = collecter_donnees_raw(args.siren, args.idus)
        print(json.dumps(raw, indent=2, ensure_ascii=False))
        sys.exit(0)

    if args.ctx_only:
        ctx = collecter_donnees_brutes(args.siren, args.idus, avec_rpg=not args.no_rpg)
        print(json.dumps(ctx, indent=2, ensure_ascii=False))
        sys.exit(0)

    rapport = generer_rapport(
        siren    = args.siren,
        idus     = args.idus,
        avec_rpg = not args.no_rpg,
        model    = args.model,
        output   = args.output,
    )

    print(rapport)