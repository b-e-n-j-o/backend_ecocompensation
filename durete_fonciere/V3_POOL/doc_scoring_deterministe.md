# Scoring déterministe — Notes d'implémentation

## Pourquoi

Dans `batch_durete.py`, le score est entièrement calculé par Gemini.
Avantage : flexible, nuancé. Inconvénient : deux appels sur le même SIREN
peuvent donner des résultats légèrement différents (±3-5 pts), ce qui fausse
les comparaisons dans un batch de 50 propriétaires.

L'approche hybride recommandée :

```
scoring.py Python (déterministe) → score reproductible → classement fiable
                  +
Gemini (prompt court)            → explication narrative seulement
```

---

## Ce qui existe déjà

`scoring.py` calcule déjà les 4 axes de façon déterministe :

| Axe | Fonction | Source |
|-----|----------|--------|
| Axe 1 | `scorer_axe1(ann)` | `annuaire.py` — table `AXE1_CODE` par code INSEE |
| Axe 2 | `scorer_axe2(ann)` + `signal_stabilite(sirene)` | `annuaire.py` + `sirene.py` |
| Axe 3 | `scorer_axe3_complet(ann, bod)` | `bodacc.py` — procédures + capital |
| Axe 4 | `lookup_dvf(idu).score_axe4` | `dvf.py` — ancienneté reconstitutée |
| Surcharge bail | `scorer_surcharge_bail_rural(rpg, ann, sirene)` | `rpg.py` |

`scorer_durete()` dans `scoring.py` orchestre déjà tout ça et retourne
un `ScoreDurete` avec `score_final` calculé en Python pur.

---

## Ce qu'il faudrait modifier dans `batch_durete.py`

### 1. Remplacer `collecter_donnees()` + appel Gemini scorer par `scorer_durete()`

```python
# Au lieu de :
contexte = collecter_donnees(siren, idus)
gemini   = appeler_gemini(construire_prompt_batch(contexte), model)
score    = gemini["score_final"]

# Faire :
from scoring import scorer_durete
score_obj = scorer_durete(siren, idus, avec_rpg=avec_rpg)
score     = score_obj.score_final
```

### 2. Prompt Gemini allégé — explication seulement

Gemini ne calcule plus le score, il l'explique.
Le prompt devient beaucoup plus court et moins coûteux :

```python
def construire_prompt_explication(score_obj, contexte_minimal: dict) -> str:
    return f"""Tu es expert en prospection foncière pour Kerelia.

Voici le scoring calculé automatiquement pour la personne morale {score_obj.denomination} (SIREN {score_obj.siren}) :

- Axe 1 (Nature PM)     : {score_obj.axe1}/40 — {score_obj.note_axe1}
- Axe 2 (Gouvernance)   : {score_obj.axe2}/25 — {score_obj.note_axe2}
- Axe 3 (Finances)      : {score_obj.axe3}/20 — {score_obj.note_axe3}
- Axe 4 (Patrimoine)    : {score_obj.axe4_max}/15 — {score_obj.note_axe4_max}
- Surcharge bail rural  : +{score_obj.surcharge_bail_max}
- **SCORE FINAL         : {score_obj.score_final}/100 — {score_obj.niveau_durete}**

Données complémentaires :
{json.dumps(contexte_minimal, indent=2, ensure_ascii=False)}

Rédige en 5-6 phrases une synthèse opérationnelle :
- Pourquoi ce score (profil de la PM, gouvernance, situation)
- Le principal levier d'acquisition s'il existe
- Le principal point de blocage
- La recommandation concrète

Réponds UNIQUEMENT avec un objet JSON : {{"explication": "..."}}
"""
```

### 3. Structure de sortie — inchangée

Le JSONL de sortie garde le même format, avec en plus les notes de chaque axe
issues du calcul Python (déjà dans `ScoreDurete`) :

```json
{
  "siren": "...",
  "score_final": 34,         ← Python déterministe
  "niveau_durete": "Dureté faible",
  "explication": "...",       ← Gemini narratif
  "detail_axes": {
    "axe1": 10, "axe1_note": "SCI — structure dédiée gestion immobilière",
    ...
  }
}
```

---

## Avantages concrets pour le batch

| | Actuel (tout Gemini) | Hybride (Python + Gemini explication) |
|---|---|---|
| Reproductibilité | ✗ ±3-5 pts | ✓ 100% stable |
| Coût tokens Gemini | ~800 tokens/SIREN | ~200 tokens/SIREN |
| Vitesse | identique | légèrement plus rapide |
| Qualité explication | ✓ | ✓ (même niveau) |
| Classement fiable | ✗ | ✓ |

---

## Ce qui reste à scorer côté Python (pas encore couvert)

Quelques surcharges du guide ne sont pas encore implémentées dans `scoring.py`
et nécessiteraient un développement :

- **DIA récente** (`-20 pts`) — nécessite une source de données (pas d'API publique gratuite connue)
- **ORE** (`-10 pts`) — idem, registre non encore intégré
- **Domaine public** (score forcé à 100) — déductible partiellement du code nature juridique (7xxx)
  mais mériterait une règle explicite dans `scorer_axe1`

Pour l'instant, ces cas sont couverts par l'appréciation Gemini.
Dans une version déterministe complète, il faudrait soit les ignorer (noter dans les avertissements),
soit les laisser à Gemini uniquement pour ces cas particuliers.