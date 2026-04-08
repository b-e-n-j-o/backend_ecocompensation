# -*- coding: utf-8 -*-
"""
rapport_icpe_llm.py — Rapport narratif ICPE via Gemini
Kerelia — Pipeline Dureté Foncière

Orchestre :
  1. icpe.py    → fetch des données ICPE + téléchargement PDFs
  2. prompt_icpe.py → prompt expert compensation écologique
  3. gemini_utils.py → appel LLM

Usage :
    python rapport_icpe_llm.py --idu 32119000AH0096
    python rapport_icpe_llm.py --idu 32119000AH0096 --output rapport_icpe.md
    python rapport_icpe_llm.py --idu 32119000AH0096 --no-download --model gemini-2.5-pro-preview-03-25
    python rapport_icpe_llm.py --idu 862750000D0319 --ctx-only
"""

import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path
from dotenv import load_dotenv

from icpe import run_icpe
from gemini_utils import appeler_gemini, MODELE_DEFAUT

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("rapport_icpe_llm")


# ============================================================
# PROMPT ICPE (repris et finalisé depuis prompt_icpe.py)
# ============================================================
PROMPT_RAPPORT_ICPE = """
Tu es un expert en droit de l'environnement et en installations classées pour la protection \
de l'environnement (ICPE), au service d'un opérateur de compensation écologique cherchant \
à acquérir ou contractualiser des terrains agricoles ou naturels pour des projets de \
compensation (SNCRR — Sites Naturels de Compensation, Restauration et Renaturation).

Tu reçois ci-dessous les données ICPE d'une parcelle analysée dans le cadre d'un scoring \
de dureté foncière. Ces données proviennent de l'API Géorisques (installations_classees) \
et ont été filtrées spatialement pour ne retenir que les sites dans ou à moins de 50 mètres \
de la parcelle.

---
DONNÉES ICPE :
{icpe_json}
---

FICHIERS TÉLÉCHARGÉS (PDFs Géorisques disponibles localement) :
{fichiers_telecharges}
---

DATE D'ANALYSE : {date_analyse}
---

Sur la base de ces données (certains champs peuvent être null ou absents — ne les invente pas),
rédige un rapport structuré en français selon le plan suivant :

## 1. Synthèse exécutive (3-5 phrases)
Résume la situation ICPE de la parcelle en une lecture immédiate : y a-t-il des installations \
actives, quel est le niveau de risque global, et quelle est la conclusion préliminaire pour \
un projet de compensation écologique ?

## 2. Analyse site par site
Pour chaque site retenu, développe les points suivants :

### [Nom du site] — [Dans la parcelle / À proximité (<50m)]
- **Identité** : raison sociale, SIRET, code AIOT, service instructeur
- **Statut réglementaire** : régime ICPE (Autorisation / Enregistrement / Déclaration / Non ICPE),
  état d'activité, classification Seveso le cas échéant, IED
- **Activité** : interprète le code NAF et les rubriques pour expliquer en termes concrets
  ce que fait l'établissement et quels sont les risques associés (substances, volumes, procédés).
  Si les rubriques sont vides, indique-le explicitement.
- **Historique réglementaire** : commente les documents administratifs disponibles
  (arrêtés de mise en demeure, prescriptions complémentaires) — leur nombre, leur fréquence,
  ce qu'ils révèlent sur la relation avec l'inspection des installations classées.
  Si aucun document n'est disponible, indique-le.
- **Inspections** : date de la dernière inspection, fréquence observée si plusieurs disponibles.

## 3. Documents disponibles pour analyse approfondie
Liste les fichiers téléchargés (fournis dans FICHIERS TÉLÉCHARGÉS ci-dessus) et indique pour \
chaque type de document ce qu'on pourrait y trouver d'utile :

- **Rapports d'inspection publiables** : conclusions de l'inspecteur DREAL sur la conformité,
  non-conformités relevées, prescriptions imposées, délais de mise en conformité
- **Arrêtés de mise en demeure** : nature des infractions constatées,
  sanctions applicables, obligations de l'exploitant
- **Arrêtés de prescriptions complémentaires** : nouvelles contraintes imposées,
  évolutions du périmètre réglementaire, mesures de surveillance environnementale requises

Si aucun fichier n'a été téléchargé (no-download ou aucun document disponible), indique-le \
et précise quels documents pourraient être pertinents à consulter sur Géorisques.

## 4. Mise en perspective compensation écologique
Analyse la compatibilité de la situation ICPE avec un projet de compensation :

- Une ICPE en exploitation active sur la parcelle ou à proximité immédiate est-elle
  compatible avec une démarche de compensation (qualité écologique, continuité, absence de perturbation) ?
- Les rubriques déclarées génèrent-elles des risques de pollution des sols ou des eaux souterraines
  susceptibles de compromettre la valeur écologique du terrain ?
- Un historique de mises en demeure signale-t-il un exploitant peu rigoureux sur le plan environnemental,
  ce qui renforcerait les risques de pollution diffuse ?
- Dans le cas d'une ICPE à l'arrêt ou en cessation, la dépollution a-t-elle vraisemblablement été réalisée
  ou y a-t-il des incertitudes à lever ?
- Si aucune ICPE n'est détectée, conclure explicitement que le terrain est a priori exempt de
  contraintes ICPE dans le rayon analysé.

## 5. Recommandations opérationnelles
Formule 2 à 4 recommandations concrètes à l'attention de l'opérateur de compensation :
- Actions à mener avant toute décision d'acquisition ou de contractualisation
- Documents à consulter en priorité parmi ceux téléchargés
- Vérifications complémentaires à réaliser (BASIAS, BASOL, étude de sol, etc.)
- Posture recommandée vis-à-vis du site (exclusion, vigilance, acceptable sous conditions)

---
Règles de rédaction :
- Factuel et sourcé : cite toujours les données du JSON pour étayer tes affirmations
- Si un champ est null ou absent, ne l'invente pas — indique que l'information n'est pas disponible
- Ton professionnel, dense, orienté décision opérationnelle
- Longueur : 600 à 900 mots
"""


# ============================================================
# CONSTRUCTION DU CONTEXTE
# ============================================================
def construire_contexte_icpe(idu: str, download: bool = True) -> tuple[dict, list[str]]:
    """
    Lance run_icpe() et collecte les logs des fichiers téléchargés.
    Retourne (icpe_data, liste_fichiers_telecharges).
    """
    # Intercepter les logs de téléchargement
    fichiers_telecharges = []

    class FichierHandler(logging.Handler):
        def emit(self, record):
            msg = record.getMessage()
            if "[OK]" in msg:
                fichiers_telecharges.append(msg.replace("[OK]", "").strip())

    handler = FichierHandler()
    logging.getLogger("icpe").addHandler(handler)

    try:
        icpe_data = run_icpe(idu, download=download)
    finally:
        logging.getLogger("icpe").removeHandler(handler)

    return icpe_data, fichiers_telecharges


def construire_prompt(icpe_data: dict, fichiers_telecharges: list[str]) -> str:
    """
    Assemble le prompt final avec les données ICPE et les fichiers téléchargés.
    """
    icpe_json = json.dumps(icpe_data, indent=2, ensure_ascii=False, default=str)

    if fichiers_telecharges:
        fichiers_str = "\n".join(f"  - {f}" for f in fichiers_telecharges)
    else:
        fichiers_str = "  Aucun fichier téléchargé (mode --no-download ou aucun document disponible)."

    return PROMPT_RAPPORT_ICPE.format(
        icpe_json=icpe_json,
        fichiers_telecharges=fichiers_str,
        date_analyse=date.today().strftime("%d/%m/%Y"),
    )


# ============================================================
# PIPELINE PRINCIPAL
# ============================================================
def generer_rapport_icpe(
    idu:      str,
    download: bool  = True,
    model:    str   = MODELE_DEFAUT,
    output:   str   = None,
) -> str:
    """
    Pipeline complet :
      1. Fetch ICPE + téléchargement PDFs
      2. Construction prompt
      3. Appel Gemini
      4. Sauvegarde optionnelle

    Retourne le rapport en Markdown.
    """
    log.info(f"=== RAPPORT ICPE — IDU : {idu} ===")

    # 1. Données ICPE
    icpe_data, fichiers = construire_contexte_icpe(idu, download=download)
    log.info(f"{icpe_data.get('count', 0)} site(s) ICPE détecté(s)")
    if fichiers:
        log.info(f"{len(fichiers)} fichier(s) téléchargé(s) :")
        for f in fichiers:
            log.info(f"  → {f}")

    # 2. Prompt
    prompt = construire_prompt(icpe_data, fichiers)
    log.info(f"Prompt construit — {len(prompt):,} chars")

    # 3. Gemini
    rapport = appeler_gemini(prompt, model=model, temperature=0.3, max_tokens=8192)

    # 4. Sauvegarde
    if output:
        Path(output).write_text(rapport, encoding="utf-8")
        log.info(f"Rapport sauvegardé : {output}")

    return rapport


# ============================================================
# CLI
# ============================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Rapport narratif ICPE via Gemini")
    parser.add_argument("--idu",         required=True,
                        help="IDU de la parcelle (ex: 32119000AH0096)")
    parser.add_argument("--output",      default=None,
                        help="Fichier de sortie .md (ex: rapport_icpe.md)")
    parser.add_argument("--model",       default=MODELE_DEFAUT,
                        help=f"Modèle Gemini (défaut: {MODELE_DEFAUT})")
    parser.add_argument("--no-download", action="store_true",
                        help="Ne pas télécharger les PDFs (plus rapide)")
    parser.add_argument("--ctx-only",    action="store_true",
                        help="Affiche le contexte JSON + prompt sans appeler Gemini")
    args = parser.parse_args()

    if args.ctx_only:
        icpe_data, fichiers = construire_contexte_icpe(args.idu, download=False)
        prompt = construire_prompt(icpe_data, fichiers)
        print("=== DONNÉES ICPE ===")
        print(json.dumps(icpe_data, indent=2, ensure_ascii=False, default=str))
        print("\n=== PROMPT ===")
        print(prompt)
        sys.exit(0)

    rapport = generer_rapport_icpe(
        idu      = args.idu,
        download = not args.no_download,
        model    = args.model,
        output   = args.output,
    )

    print(rapport)