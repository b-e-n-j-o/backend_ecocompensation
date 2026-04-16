# -*- coding: utf-8 -*-
"""
batch_durete.py — Pipeline batch Dureté Foncière Kerelia
=========================================================
Itère sur un pool de ~50 propriétaires (SIREN + IDUs) et produit
pour chacun un score /100 + explication Gemini, sauvegardé en JSONL.

Entrée  : fichier CSV ou JSON (voir formats acceptés ci-dessous)
Sortie  : fichier JSONL (1 ligne = 1 résultat SIREN)

Formats CSV acceptés :
    siren,idus
    892632365,"86275000D0319,86275000D0320"
    123456789,

    → Si colonne idus absente ou vide : IDUs récupérés depuis Supabase

Formats JSON acceptés :
    [
      {"siren": "892632365", "idus": ["86275000D0319"]},
      {"siren": "123456789"}   ← IDUs depuis Supabase
    ]

Usage :
    python batch_durete.py --input parcelles.csv --output resultats.jsonl
    python batch_durete.py --input parcelles.json --output resultats.jsonl --workers 3
    python batch_durete.py --input parcelles.csv --output resultats.jsonl --no-rpg --dry-run
    python batch_durete.py --input parcelles.csv --output resultats.jsonl --resume
"""

import argparse
import csv
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

# Modules Kerelia
try:
    # Import package (usage backend/uvicorn)
    from .annuaire import fetch_annuaire, scorer_axe1, enrichir_contexte_forme_juridique
    from .bodacc import fetch_bodacc
    from .dvf import lookup_dvf
    from .rpg import fetch_rpg_parcelle, scorer_surcharge_bail_rural, decompose_idu
    from .rpg import _load_nomenclature
    from .sirene import fetch_sirene, signal_stabilite
    from .supabase_parcelles import fetch_parcelles_by_siren
except ImportError:
    # Import script (python batch_durete.py depuis le dossier V3_POOL)
    from annuaire import fetch_annuaire, scorer_axe1, enrichir_contexte_forme_juridique
    from bodacc import fetch_bodacc
    from dvf import lookup_dvf
    from rpg import fetch_rpg_parcelle, scorer_surcharge_bail_rural, decompose_idu
    from rpg import _load_nomenclature
    from sirene import fetch_sirene, signal_stabilite
    from supabase_parcelles import fetch_parcelles_by_siren

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("batch_durete")


# ---------------------------------------------------------------------------
# Guide méthodologique (repris de rapport.py)
# ---------------------------------------------------------------------------
GUIDE_METHODOLOGIQUE = """
# GUIDE DE SCORING — DURETÉ FONCIÈRE DES PERSONNES MORALES (KERELIA / SNCRR)

## Objectif
Évaluer la difficulté d'acquisition d'un terrain identifié comme gisement potentiel
de compensation écologique, en fonction de la nature de son propriétaire (personne morale).
Score de 1 (acquisition très facile) à 100 (acquisition quasi impossible).

## Architecture du score (100 points)

### AXE 1 — Nature de la Personne Morale (40 points)
- État français : 40/40
- Collectivités territoriales (communes, dép., régions) : 37/40
- EPCI, syndicats intercommunaux : 35/40
- Établissements publics (santé, enseignement) : 33/40
- Organismes HLM : 30/40
- Structures environnementales (CEN, PNR, Conservatoire du littoral) : 38/40
- SAFER : 20/40
- Grandes entreprises industrielles/commerciales (SA, grandes SARL) : 20/40
- Investisseurs professionnels (fonds, SCPI, assurances) : 15/40
- Promoteurs/aménageurs : 18/40
- Sociétés commerciales classiques (SARL, SAS, SA PME) : 18/40
- Structures agricoles (GFA, GAEC, exploitants) : 14-20/40
- Groupements forestiers : 22/40
- SCI familiale : 10/40
- Associations/fondations : 26/40

### AXE 2 — Gouvernance et Complexité Décisionnelle (25 points)
- Copropriété ou BND : 25/25
- Indivision entre personnes morales : 22/25
- SCI avec plus de 5 associés : 20/25
- SA avec conseil d'administration : 18/25
- SCI avec 2 à 5 associés : 15/25
- SARL/SAS avec pluralité d'associés : 7/25
- Société à associé unique (EURL, SASU) : 2/25
- Personne morale publique : 20/25

### AXE 3 — Situation Financière et Statut Juridique (20 points)
- Société très rentable, forte capitalisation : 20/20
- Société à l'équilibre, activité stable : 15/20
- Société déficitaire mais active : 8/20
- Société inactive ou en sommeil : 3/20
- Procédure de sauvegarde : 5/20
- Redressement judiciaire : 3/20
- Liquidation judiciaire : 0/20

### AXE 4 — Comportement Patrimonial et Ancienneté (15 points)
- Acquisition très récente (< 3 ans) : 15/15
- Détention courte (3-5 ans) : 12/15
- Détention moyenne (5-15 ans) : 8/15
- Détention longue (15-30 ans) : 8/15
- Très longue détention (> 30 ans) : 12/15
- PM ayant réalisé des ventes récentes (< 5 ans) : 2/15
- PM en dissolution/liquidation amiable : 0/15

### RÈGLES DE SURCHARGE
- Terrain dans domaine public : score forcé à 100
- Bail rural ou emphytéotique en cours : +15 points
- ORE : -10 points
- DIA récente : -20 points

## Grille d'interprétation finale
- 0-20 : Opportunité exceptionnelle
- 21-40 : Dureté faible
- 41-60 : Dureté modérée
- 61-80 : Dureté forte
- 81-100 : Dureté rédhibitoire
"""


# ---------------------------------------------------------------------------
# Lecture du fichier d'entrée
# ---------------------------------------------------------------------------

def lire_input(path: str) -> list[dict]:
    """
    Lit un CSV ou JSON et retourne une liste de dicts :
      [{"siren": "...", "idus": [...] or []}, ...]
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Fichier introuvable : {path}")

    ext = p.suffix.lower()

    if ext == ".json":
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        result = []
        for row in data:
            siren = str(row.get("siren", "")).strip()

            # Accepte "idus" (liste), "idu" (string singulier), ou rien
            if "idus" in row:
                idus = row["idus"]
                if isinstance(idus, str):
                    idus = [i.strip() for i in idus.split(",") if i.strip()]
            elif "idu" in row:
                idu_val = str(row["idu"]).strip()
                idus = [idu_val] if idu_val else []
            else:
                idus = []

            result.append({
                "siren":          siren,
                "idus":           idus,
                # Champs optionnels pré-remplis — évitent un appel Annuaire si déjà connus
                "denomination":   row.get("denomination", "").strip(),
                "forme_juridique": row.get("forme_juridique", "").strip(),
            })
        return result

    elif ext == ".csv":
        result = []
        with open(p, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                siren    = str(row.get("siren", "")).strip()
                idus_raw = (row.get("idus") or row.get("idu") or "").strip()
                idus     = [i.strip() for i in idus_raw.split(",") if i.strip()] if idus_raw else []
                result.append({
                    "siren":           siren,
                    "idus":            idus,
                    "denomination":    row.get("denomination", "").strip(),
                    "forme_juridique": row.get("forme_juridique", "").strip(),
                })
        return result

    else:
        raise ValueError(f"Format non supporté : {ext} (acceptés : .csv, .json)")


# ---------------------------------------------------------------------------
# Collecte des données brutes (sans urba, sans rapport complet)
# ---------------------------------------------------------------------------

def collecter_donnees(siren: str, idus: list[str], avec_rpg: bool = True) -> dict:
    """
    Collecte Annuaire + BODACC + Sirene + DVF×N + RPG×N.
    Retourne le contexte brut destiné au prompt Gemini.
    """
    nom = _load_nomenclature()

    contexte = {
        "siren":              siren,
        "idus":               idus,
        "date_analyse":       date.today().isoformat(),
        "annuaire":           {},
        "sirene":             {},
        "bodacc":             {},
        "parcelles":          [],
        "avertissements":     [],
    }

    # 1. Annuaire Entreprises
    ann = fetch_annuaire(siren)
    if ann:
        contexte["annuaire"] = {
            "denomination":          ann.get("nom_complet"),
            "siren":                 ann.get("siren"),
            "nature_juridique_code": ann.get("nature_juridique"),
            "statut":                ann.get("etat_administratif"),
            "naf":                   ann.get("activite_principale"),
            "date_creation":         ann.get("date_creation"),
            "adresse":               ann.get("siege", {}).get("adresse"),
            "commune":               ann.get("siege", {}).get("libelle_commune"),
            "est_service_public":    ann.get("complements", {}).get("est_service_public"),
            "est_association":       ann.get("complements", {}).get("est_association"),
            "dirigeants": [
                {
                    "nom":     d.get("nom"),
                    "prenom":  d.get("prenoms", d.get("prenom")),
                    "qualite": d.get("qualite"),
                    "annee_naissance": d.get("annee_de_naissance"),
                }
                for d in ann.get("dirigeants", [])
            ],
        }
        # Enrichissement libellés N2/N3 pour Gemini
        enrichir_contexte_forme_juridique(contexte["annuaire"])

        # Calcul déterministe Axe 1 — score Python pur, non délégué à Gemini
        score_axe1, note_axe1 = scorer_axe1(ann)
        contexte["axe1_deterministe"] = {
            "score":  score_axe1,
            "note":   note_axe1,
            "source": "calcul Python automatique (nomenclature INSEE niveau 2/3)",
        }
        log.info(f"  Axe 1 déterministe : {score_axe1}/40 — {note_axe1}")
    else:
        contexte["avertissements"].append("Annuaire Entreprises : SIREN introuvable")

    # 2. Sirene INSEE
    try:
        contexte["sirene"] = fetch_sirene(siren) or {}
    except Exception as e:
        contexte["avertissements"].append(f"Sirene indisponible : {e}")

    # 3. BODACC
    bod = fetch_bodacc(siren)
    contexte["bodacc"] = {
        "nb_annonces":          bod.get("nb_annonces"),
        "procedure_collective": bod.get("procedure_collective"),
        "type_procedure":       bod.get("type_procedure"),
        "capital_social_eur":   bod.get("capital_social"),
        "statut_bodacc":        bod.get("statut_bodacc"),
        "associes_bodacc":      bod.get("associes_bodacc"),
    }

    # 4. DVF + RPG par parcelle
    date_creation_pm = contexte["annuaire"].get("date_creation")

    for idu in idus:
        parcelle_ctx = {"idu": idu}

        # DVF
        try:
            dvf = lookup_dvf(idu, date_creation_pm=date_creation_pm)
            parcelle_ctx["dvf"] = {
                "nb_mutations":      dvf.nb_mutations,
                "statut":            dvf.statut.value if dvf.statut else None,
                "date_acquisition":  dvf.date_acquisition,
                "valeur_acquisition_eur": dvf.valeur_acquisition,
                "nature_culture":    dvf.nature_culture,
                "signal":            dvf.signal,
                "avertissements":    dvf.avertissements,
            }
        except Exception as e:
            parcelle_ctx["dvf"] = {"erreur": str(e)}
            contexte["avertissements"].append(f"DVF erreur {idu} : {e}")

        # RPG
        if avec_rpg:
            try:
                insee, section, numero = decompose_idu(idu)
                rpg = fetch_rpg_parcelle(insee, section, numero)
                summary = rpg.get("summary", {})
                by_year = rpg.get("by_year", {})

                historique_rpg = {}
                for year, data in by_year.items():
                    if data.get("status") == "agricole":
                        cultures_libelles = {
                            code: nom["code_cultu"].get(code, nom["culture_d1"].get(code, code))
                            for code in data.get("cultures", {})
                        }
                        historique_rpg[str(year)] = {
                            "status":    "agricole",
                            "surface_m2": data.get("total_m2"),
                            "cultures":  cultures_libelles,
                        }
                    else:
                        historique_rpg[str(year)] = {"status": data.get("status")}

                parcelle_ctx["rpg"] = {
                    "occupation_agricole":  summary.get("occupation_agricole"),
                    "nb_annees_agricoles":  summary.get("nb_annees_agricoles"),
                    "bail_rural_certain":   summary.get("bail_rural_certain"),
                    "bail_rural_probable":  summary.get("bail_rural_probable"),
                    "codes_cultures":       summary.get("codes_cultures"),
                    "historique_par_annee": historique_rpg,
                }
            except Exception as e:
                parcelle_ctx["rpg"] = {"erreur": str(e)}
                contexte["avertissements"].append(f"RPG erreur {idu} : {e}")

        contexte["parcelles"].append(parcelle_ctx)

    return contexte


# ---------------------------------------------------------------------------
# Prompt Gemini — version light (score + explication courte)
# ---------------------------------------------------------------------------

def construire_prompt_batch(contexte: dict) -> str:
    ctx_json = json.dumps(contexte, indent=2, ensure_ascii=False)

    axe1 = contexte.get("axe1_deterministe", {})
    score_axe1 = axe1.get("score", "?")
    note_axe1  = axe1.get("note", "non calculé")

    return f"""Tu es un expert en prospection foncière spécialisé dans la compensation écologique pour le bureau d'études Kerelia.

Ta mission : analyser la dureté foncière d'une personne morale propriétaire de parcelles, et produire un score /100 + une explication synthétique.

---

## GUIDE MÉTHODOLOGIQUE DE SCORING

{GUIDE_METHODOLOGIQUE}

---

## DONNÉES

```json
{ctx_json}
```

---

## CONSIGNE DE RÉPONSE

**IMPORTANT — AXE 1 PRÉ-CALCULÉ :**
L'Axe 1 (Nature de la PM) a été calculé automatiquement par le système à partir de la nomenclature juridique INSEE :
- Score Axe 1 : **{score_axe1}/40**
- Justification : {note_axe1}

Tu DOIS utiliser ce score Axe 1 tel quel sans le modifier. Tu peux enrichir la note si tu disposes d'éléments contextuels supplémentaires (nom de la société, objet social…), mais le score numérique {score_axe1} est fixe.

Détermine librement les Axes 2, 3, 4 et les surcharges à partir des données fournies.

Réponds UNIQUEMENT avec un objet JSON valide, sans aucun texte avant ou après, sans balises markdown.

Structure exacte attendue :
{{
  "axe1": {score_axe1},
  "axe1_note": "<enrichis si pertinent, sinon reprends : {note_axe1}>",
  "axe2": <int 0-25>,
  "axe2_note": "<justification courte>",
  "axe3": <int 0-20>,
  "axe3_note": "<justification courte>",
  "axe4": <int 0-15>,
  "axe4_note": "<justification courte>",
  "surcharges": <int (positif ou négatif)>,
  "surcharges_note": "<surcharges appliquées et pourquoi>",
  "score_final": <int 0-100>,
  "niveau_durete": "<Opportunité exceptionnelle|Dureté faible|Dureté modérée|Dureté forte|Dureté rédhibitoire>",
  "explication": "<Synthèse narrative de 5-8 phrases justifiant le score, les leviers d'acquisition et les points de vigilance>"
}}

RAPPEL :
- axe1 = {score_axe1} (fixe, ne pas modifier)
- score_final = {score_axe1} + axe2 + axe3 + axe4 + surcharges, plafonné à 100
- Sois précis dans les justifications, ne génère pas de texte générique
- Si des données sont manquantes, indique-le dans la note correspondante
"""


# ---------------------------------------------------------------------------
# Appel Gemini avec retry
# ---------------------------------------------------------------------------

def appeler_gemini(prompt: str, model: str, max_retries: int = 3) -> dict:
    """
    Appelle Gemini et parse le JSON retourné.
    Retry sur erreur API ou JSON invalide.
    """
    try:
        import google.generativeai as genai
    except ImportError:
        raise ImportError("pip install google-generativeai requis")

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY non définie")

    genai.configure(api_key=api_key)
    model_client = genai.GenerativeModel(model)

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            log.info(f"  Gemini tentative {attempt}/{max_retries} ...")
            response = model_client.generate_content(
                prompt,
                generation_config=genai.GenerationConfig(
                    temperature=0.2,
                    max_output_tokens=1024,
                ),
            )
            text = response.text.strip()

            # Nettoyage éventuel de balises markdown
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

            parsed = json.loads(text)
            return parsed

        except json.JSONDecodeError as e:
            last_err = f"JSON invalide : {e} — réponse brute : {text[:200]}"
            log.warning(f"  Gemini JSON parse échoué (tentative {attempt}) : {e}")
            time.sleep(2 ** attempt)

        except Exception as e:
            last_err = str(e)
            log.warning(f"  Gemini erreur API (tentative {attempt}) : {e}")
            time.sleep(2 ** attempt)

    raise RuntimeError(f"Gemini échoué après {max_retries} tentatives : {last_err}")


# ---------------------------------------------------------------------------
# Traitement d'un seul SIREN
# ---------------------------------------------------------------------------

def traiter_siren(
    siren:           str,
    idus_input:      list[str],
    avec_rpg:        bool,
    model:           str,
    denomination_input: str = "",
    forme_juridique_input: str = "",
) -> dict:
    """
    Pipeline complet pour un SIREN.
    Retourne un dict résultat prêt à être sérialisé en JSON.
    """
    ts_debut = datetime.now().isoformat()
    log.info(f"[{siren}] Début traitement")

    # Résolution IDUs : input > Supabase
    idus = idus_input
    denomination = ""

    if not idus:
        log.info(f"[{siren}] IDUs absents → fetch Supabase")
        meta = fetch_parcelles_by_siren(siren)
        if meta:
            idus        = meta.get("idus", [])
            denomination = meta.get("denomination", "")
            log.info(f"[{siren}] Supabase : {len(idus)} IDUs trouvés")
        else:
            log.warning(f"[{siren}] Aucune parcelle en Supabase")

    if not idus:
        return {
            "siren":        siren,
            "denomination": denomination,
            "idus":         [],
            "statut":       "erreur",
            "erreur":       "Aucun IDU disponible (ni en input ni en Supabase)",
            "ts":           ts_debut,
        }

    # Collecte données
    contexte = collecter_donnees(siren, idus, avec_rpg=avec_rpg)

    # Dénomination : priorité Annuaire > input > siren
    denomination = (
        contexte["annuaire"].get("denomination")
        or denomination_input
        or denomination
        or siren
    )

    # Forme juridique pré-remplie : injectée dans le contexte si Annuaire muet
    if forme_juridique_input and not contexte["annuaire"].get("nature_juridique_code"):
        contexte["annuaire"]["nature_juridique_code"] = forme_juridique_input
        contexte["annuaire"]["_source_forme_juridique"] = "input"

    # Prompt + Gemini
    prompt  = construire_prompt_batch(contexte)
    gemini  = appeler_gemini(prompt, model=model)

    ts_fin = datetime.now().isoformat()
    duree_s = round(
        (datetime.fromisoformat(ts_fin) - datetime.fromisoformat(ts_debut)).total_seconds(), 1
    )

    result = {
        "siren":          siren,
        "denomination":   denomination,
        "idus":           idus,
        "nb_parcelles":   len(idus),
        "statut":         "ok",
        "score_final":    gemini.get("score_final"),
        "niveau_durete":  gemini.get("niveau_durete"),
        "explication":    gemini.get("explication"),
        "detail_axes": {
            "axe1":            gemini.get("axe1"),
            "axe1_note":       gemini.get("axe1_note"),
            "axe2":            gemini.get("axe2"),
            "axe2_note":       gemini.get("axe2_note"),
            "axe3":            gemini.get("axe3"),
            "axe3_note":       gemini.get("axe3_note"),
            "axe4":            gemini.get("axe4"),
            "axe4_note":       gemini.get("axe4_note"),
            "surcharges":      gemini.get("surcharges"),
            "surcharges_note": gemini.get("surcharges_note"),
        },
        "avertissements": contexte.get("avertissements", []),
        "ts_debut":       ts_debut,
        "ts_fin":         ts_fin,
        "duree_s":        duree_s,
    }

    log.info(
        f"[{siren}] ✓ {denomination} — score {result['score_final']}/100 "
        f"({result['niveau_durete']}) en {duree_s}s"
    )
    return result


# ---------------------------------------------------------------------------
# Traitement avec retry (wrapper)
# ---------------------------------------------------------------------------

def traiter_siren_avec_retry(
    siren:      str,
    idus:       list[str],
    avec_rpg:   bool,
    model:      str,
    max_retries: int = 3,
    denomination_input: str = "",
    forme_juridique_input: str = "",
) -> dict:
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            return traiter_siren(siren, idus, avec_rpg, model,
                                 denomination_input=denomination_input,
                                 forme_juridique_input=forme_juridique_input)
        except Exception as e:
            last_err = str(e)
            log.warning(f"[{siren}] Erreur tentative {attempt}/{max_retries} : {e}")
            if attempt < max_retries:
                time.sleep(3 * attempt)

    log.error(f"[{siren}] Échec définitif après {max_retries} tentatives : {last_err}")
    return {
        "siren":   siren,
        "idus":    idus,
        "statut":  "erreur",
        "erreur":  last_err,
        "ts":      datetime.now().isoformat(),
    }


# ---------------------------------------------------------------------------
# Chargement des SIRENs déjà traités (pour --resume)
# ---------------------------------------------------------------------------

def charger_sirens_traites(output_path: str) -> set[str]:
    done = set()
    p = Path(output_path)
    if not p.exists():
        return done
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if obj.get("statut") == "ok":
                    done.add(str(obj["siren"]))
            except json.JSONDecodeError:
                pass
    log.info(f"Resume : {len(done)} SIREN(s) déjà traités avec succès")
    return done


# ---------------------------------------------------------------------------
# Pipeline batch principal
# ---------------------------------------------------------------------------

def run_batch(
    input_path:  str,
    output_path: str,
    avec_rpg:    bool = True,
    model:       str  = "gemini-2.0-flash",
    workers:     int  = 1,
    resume:      bool = False,
    dry_run:     bool = False,
) -> None:

    # Lecture input
    items = lire_input(input_path)
    log.info(f"Input : {len(items)} SIREN(s) à traiter depuis {input_path}")

    # Resume : filtrer déjà traités
    if resume:
        deja_traites = charger_sirens_traites(output_path)
        items_filtres = [i for i in items if i["siren"] not in deja_traites]
        log.info(f"Resume : {len(items) - len(items_filtres)} skippés → {len(items_filtres)} restants")
        items = items_filtres

    if dry_run:
        log.info("DRY RUN — affichage de l'input uniquement, pas d'appels API")
        for i, item in enumerate(items, 1):
            print(f"  [{i:02d}] SIREN={item['siren']} idus={item['idus'] or '(depuis Supabase)'}")
        return

    if not items:
        log.info("Rien à traiter.")
        return

    # Ouverture output en mode append (pour resume)
    mode = "a" if resume else "w"
    out_path = Path(output_path)

    compteurs = {"ok": 0, "erreur": 0}
    ts_global = datetime.now()

    with open(out_path, mode, encoding="utf-8") as out_f:

        if workers == 1:
            # Mode séquentiel — plus safe pour debug et quotas API
            for i, item in enumerate(items, 1):
                log.info(f"[{i}/{len(items)}] SIREN {item['siren']}")
                result = traiter_siren_avec_retry(
                    siren    = item["siren"],
                    idus     = item["idus"],
                    avec_rpg = avec_rpg,
                    model    = model,
                    denomination_input    = item.get("denomination", ""),
                    forme_juridique_input = item.get("forme_juridique", ""),
                )
                out_f.write(json.dumps(result, ensure_ascii=False) + "\n")
                out_f.flush()
                compteurs["ok" if result["statut"] == "ok" else "erreur"] += 1

        else:
            # Mode parallèle (attention aux quotas Gemini / DVF)
            log.warning(f"Mode parallèle : {workers} workers — attention aux rate limits API")
            futures = {}
            with ThreadPoolExecutor(max_workers=workers) as executor:
                for item in items:
                    fut = executor.submit(
                        traiter_siren_avec_retry,
                        item["siren"], item["idus"], avec_rpg, model, 3,
                        item.get("denomination", ""),
                        item.get("forme_juridique", ""),
                    )
                    futures[fut] = item["siren"]

                for fut in as_completed(futures):
                    siren = futures[fut]
                    try:
                        result = fut.result()
                    except Exception as e:
                        result = {
                            "siren":  siren,
                            "statut": "erreur",
                            "erreur": str(e),
                            "ts":     datetime.now().isoformat(),
                        }
                    out_f.write(json.dumps(result, ensure_ascii=False) + "\n")
                    out_f.flush()
                    compteurs["ok" if result["statut"] == "ok" else "erreur"] += 1

    duree_totale = round((datetime.now() - ts_global).total_seconds(), 1)

    log.info("=" * 60)
    log.info(f"BATCH TERMINÉ en {duree_totale}s")
    log.info(f"  ✓ Succès : {compteurs['ok']}")
    log.info(f"  ✗ Erreurs : {compteurs['erreur']}")
    log.info(f"  Output   : {out_path.resolve()}")
    log.info("=" * 60)


# ---------------------------------------------------------------------------
# Lecture rapide du JSONL de sortie
# ---------------------------------------------------------------------------

def lire_resultats(jsonl_path: str) -> list[dict]:
    """Utilitaire : lit le JSONL de sortie et retourne une liste triée par score."""
    results = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return sorted(
        [r for r in results if r.get("statut") == "ok"],
        key=lambda x: x.get("score_final", 999),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Pipeline batch dureté foncière — Kerelia",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--input",   required=True,  help="Fichier CSV ou JSON d'entrée")
    parser.add_argument("--output",  required=True,  help="Fichier JSONL de sortie")
    parser.add_argument("--model",   default="gemini-2.0-flash", help="Modèle Gemini")
    parser.add_argument("--workers", type=int, default=1, help="Nb workers parallèles (défaut=1)")
    parser.add_argument("--no-rpg",  action="store_true", help="Désactiver RPG (plus rapide)")
    parser.add_argument("--resume",  action="store_true", help="Reprendre un batch interrompu")
    parser.add_argument("--dry-run", action="store_true", help="Lister les SIRENs sans appels API")
    parser.add_argument(
        "--summary", metavar="JSONL",
        help="Affiche un résumé trié d'un fichier JSONL existant (sans lancer le batch)"
    )
    args = parser.parse_args()

    # Mode summary seul
    if args.summary:
        resultats = lire_resultats(args.summary)
        print(f"\n{'SIREN':<12} {'Score':>6}  {'Niveau':<28}  Dénomination")
        print("─" * 80)
        for r in resultats:
            print(
                f"{r['siren']:<12} {r.get('score_final', '?'):>6}  "
                f"{r.get('niveau_durete', '?'):<28}  {r.get('denomination', '')}"
            )
        print(f"\n{len(resultats)} résultat(s) OK\n")
        sys.exit(0)

    run_batch(
        input_path  = args.input,
        output_path = args.output,
        avec_rpg    = not args.no_rpg,
        model       = args.model,
        workers     = args.workers,
        resume      = args.resume,
        dry_run     = args.dry_run,
    )