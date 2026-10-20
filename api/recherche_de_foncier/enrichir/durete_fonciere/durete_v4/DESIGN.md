# Dureté foncière V4 — choix de conception

> Analyse de la situation foncière d'une personne morale à partir de son SIREN :
> **score déterministe traçable** + **indice de confiance** + **recherche web sourcée (option)** + **synthèse courte**.
> Drop-in : `durete_v4.service.run_durete_for_siren` garde la signature et le format de sortie V3.

---

## 1. Ce qui ne va pas en V3 (audit)

### Bugs qui faussent les résultats aujourd'hui

| # | Module | Problème | Effet | Correction V4 |
|---|---|---|---|---|
| 1 | `bodacc.py` | Teste `familleavis in ("liquidation","redressement","sauvegarde")` ; la famille réelle est `collective`, la nature est dans le JSON `jugement` | **Aucune procédure collective n'est jamais détectée** → axe 3 « 0/20 liquidation » inatteignable | Parsing de `jugement` + classement ouverture / plan / cession / clôture, sur l'annonce **la plus récente** |
| 2 | `bodacc.py` | `q=siren` full-text | Annonces d'**autres** sociétés qui mentionnent le SIREN (acquéreur, associé PM) attribuées à la PM | Filtre strict sur `registre` / `numeroIdentification` |
| 3 | `bodacc.py` | `statut_bodacc` écrasé au fil de la boucle | Une création ancienne peut masquer une radiation selon l'ordre de tri | Tri explicite, l'annonce la plus récente fait foi |
| 4 | `bodacc.py` | API Opendatasoft v1 | Dépréciée | Explore API v2.1 |
| 5 | `annuaire.py` | `finances.get("ca")` alors que `finances` = `{"2024": {"ca", "resultat_net"}}` | **La seule donnée financière gratuite n'est jamais lue** | Lecture par exercice, tendance sur 2 ans |
| 6 | `annuaire.py` | `per_page=1` sur recherche full-text, sans vérif du SIREN | Risque de profil d'une autre entité | Match exact du SIREN, sinon `{}` |
| 7 | `annuaire.py` | Commissaires aux comptes comptés comme décideurs | Axe 2 surévalué (SA, associations) | Exclus |
| 8 | `annuaire.py` | Codes `6515`, `6516` dans la table axe 1 | N'existent pas dans la nomenclature INSEE | Supprimés ; ajout SA HLM, SEM, EPLE, SAFER à directoire |
| 9 | `rpg.py` | Toute intersection compte (liseré d'1 m² d'un îlot voisin) | **Bail rural surdétecté** | Seuil : ≥ 10 % de la parcelle ou ≥ 2 500 m² |
| 10 | `rpg.py` | Surfaces en EPSG:3857 | ×~2 en Gironde | Lambert-93 |
| 11 | `rpg.py` | Millésime en erreur réseau = « non agricole » | Casse la continuité → bail certain déclassé | Statut `indisponible`, ignoré dans la continuité |
| 12 | `rpg.py` | `scorer_surcharge_bail_rural(rpg, ann, sirene)` ignore `ann`/`sirene` | Un GAEC/EARL qui déclare **sa propre** PAC reçoit +15 « bail rural » | Règle dépendante du propriétaire (exploitant → faire-valoir direct ; GFA → bail quasi certain) |
| 13 | `dvf.py` | CSV **départemental** × 6 millésimes × chaque parcelle, sans cache | Des centaines de Mo par SIREN en Gironde, batch très lent | CSV **par commune**, cache mémoire + disque |
| 14 | `dvf.py` | Millésimes codés en dur `2020…2025` | Vieillit en silence | Fenêtre glissante, 404 ignorés |
| 15 | `dvf.py` | « Aucune mutation » → score neutre 7 | Perd l'info disponible | Intervalle de détention [début fenêtre ; création PM] |
| 16 | `sirene.py` | **Clé INSEE en dur** dans le CLI | Fuite de secret | Supprimée — **à révoquer sur portail-api.insee.fr** |
| 17 | `batch_durete.py` | Gemini calcule axes 2-4 + total | ±3-5 pts entre deux runs, classement non fiable (cf. `doc_scoring_deterministe.md`) | Score 100 % Python |
| 18 | `batch_durete.py` | SDK `google-generativeai` | Remplacé par `google-genai` | `google-genai` |
| 19 | `durete_service.py` | `workers` accepté mais ignoré | — | Parallélisme réel, ordre conservé |

### Problèmes de méthode
- **Deux moteurs** (`scoring.py` déterministe et `batch_durete.py` Gemini) qui divergent ; le backend utilisait le moins fiable.
- **Aucune notion de confiance** : un 45 fait de valeurs neutres (SCI sans comptes, sans mutation DVF) ressemblait à un 45 solide.
- `signal_stabilite` (Sirene) : `nombrePeriodesUniteLegale` augmente à chaque changement de nom/NAF/ESS — pas un indicateur de restructuration. Retiré du score.
- Capital social (BODACC) comme proxy de santé financière : non pertinent (une SCI à 1 000 € de capital peut détenir des millions d'actifs). Retiré du score, conservé en info.

---

## 2. Architecture V4

```
SIREN (+ IDU optionnels)
 │
 ├─ sources/            fetch_* (réseau, tolérant) + analyser_* (PUR, testable)
 │   ├─ parcelles.py    fichier PM DGFiP → IDU, patrimoine total, co-titulaires, démembrement
 │   ├─ annuaire.py     identité, forme, NAF, dirigeants, finances, compléments
 │   ├─ bodacc.py       procédures, dissolution, radiation, dépôts de comptes
 │   ├─ dvf.py          mutations par commune → intervalle de détention
 │   ├─ rpg.py          continuité PAC (L93, seuils)
 │   ├─ gpu.py          zonage PLU/CC (NOUVEAU)
 │   └─ sirene.py       optionnel, historique informatif
 │
 ├─ scoring.py          règles déterministes → Facteurs tracés, axes, surcharges, confiance, recommandations
 ├─ web_research.py     (option) recherche web sourcée en 2 étapes
 ├─ narration.py        LLM explique (ne calcule pas) ; repli déterministe sans LLM
 ├─ pipeline.py         orchestration, sources injectables (`Sources`)
 ├─ service.py          façade drop-in V3
 ├─ cache.py            même table Postgres, version liée aux barèmes
 └─ cli.py              analyse / batch / resume
```

**Choix clés**
- **Séparation fetch / analyse** : toute la logique est dans des fonctions pures → 48 tests hors-ligne, déterminisme vérifié.
- **Sources injectables** : `Sources(fetch_zonage=module_urba_kerfoncier, parcelles=SqlProvider(conn))` — on branche vos modules existants (onglet Urba, cadastre Etalab en base) sans dupliquer les appels.
- **Réseau mutualisé** (`net.py`) : session unique, retries 429/5xx avec `Retry-After`, throttling Annuaire (7 req/s), cache mémoire + disque (`DURETE_CACHE_DIR`).
- **Parcelles analysées en parallèle** (DVF + RPG + GPU), millésimes RPG en parallèle.
- **Dégradation explicite** : une source muette ne plante rien, elle baisse l'indice de confiance. Sous 40/100, `score_fiable=False` et le résultat **n'est pas mis en cache**.

---

## 3. Scoring

Le **guide méthodologique** (4 axes /100 + surcharges, grille 0-20/…/81-100) reste la référence ; barèmes centralisés dans `config.py` (un seul fichier à faire relire). Tout écart au guide est marqué `ext_v4`.

| Axe | V4 | Écarts vs V3 |
|---|---|---|
| **1 Nature** /40 | Structure environnementale détectée par le nom (CEN, Conservatoire, PNR, ONF, OFB, fédérations) → 38 (le guide le prévoyait, V3 non) → table code 4 chiffres → **NAF pour sociétés commerciales** (promoteur 41.10 → 18, marchand de biens 68.10Z → 15, fonds → 15, agricole 01. → 16) → GE/ETI → 20 → famille 2 chiffres → neutre 20 | NAF et GE/ETI = `ext_v4` alignés sur les catégories du guide |
| **2 Gouvernance** /25 | Copropriété 25 · **indivision entre PM (fichier PM)** 22 · public 20 · SA 18 · association/fondation 18 · société civile : associés RNE si publiés (1→2, 2-5→15, >5→20), sinon gérants avec **confiance faible** · sociétés commerciales 4/7/12 · **+3 démembrement**, **+2 dirigeant PM** | CAC exclus ; plus de +2 Sirene |
| **3 Finances** /20 | Procédure en cours (liquidation 0, cession 2, RJ 3, sauvegarde 5) · plan adopté 8 · **dissolution amiable 3** · cessée/radiée 3 · public 18 · **finances Annuaire** : 2 pertes consécutives 6, perte 8, CA ≥ 10 M€ ou RN ≥ 1 M€ 20, sinon 15 · GE/ETI 18 · sinon neutre 12 (confiance faible) | Capital retiré |
| **4 Patrimoine** /15 | Barème du guide appliqué à : mutation observée → ancienneté ; sinon **intervalle** [début fenêtre DVF ; création PM] → classe commune ou point médian (confiance faible) ; apport en société détecté ; dissolution → 0 (guide). Agrégation : pire parcelle | Intervalle au lieu de 7 neutre |

**Surcharges**
- **Bail rural** dépendant du propriétaire : exploitant (GAEC, EARL, SCEA, NAF 01) → +5 faire-valoir direct ; GFA + PAC → +15 ; tiers ≥ 5 ans consécutifs → +15 ; 3-4 ans ou > 50 % → +8 ; ponctuel → +3. **Pondérée par la surface** en multi-parcelles (V3 : la pire parcelle, même minuscule, appliquait +15 à tout le lot).
- **Droit réel de tiers** (emphytéose, bail à construction, via `code_droit` du fichier PM) → +15 (guide).
- **Zonage constructible** `ext_v4` : part U × 10 + part AU × 8 + STECAL × 3 (A/N : 0). Désactivable (`DURETE_SURCHARGE_ZONAGE=0`). C'est le principal déterminant de l'attente de prix d'un propriétaire ; à faire valider par l'urbaniste avec votre futur score urba.
- **Domaine public** : jamais déduit automatiquement (une parcelle **cadastrée** d'une commune est le plus souvent du domaine **privé**) ; forcé à 100 uniquement via `domaine_public_confirme=[idu]`.
- **ORE / DIA** : pas de source ouverte nationale → listés en avertissement « non évalués ».

**Indice de confiance 0-100** : −35 sans Annuaire, −10 par axe à confiance faible (−3 moyenne), −10 RPG manquant, −5 zonage manquant, −5 parcelles absentes du fichier PM. ≥ 75 haute, ≥ 50 moyenne.

**Signaux non scorés** (UI + synthèse) : dirigeant ≥ 75 ans (transmission), modifications BODACC récentes, procédure clôturée, patrimoine total, bio/ESS, PM non propriétaire sur une parcelle, mutation postérieure au millésime du fichier PM (vendue ?), **déprise PAC** (plus déclarée depuis N → fin de bail possible = levier).

**Recommandations de maîtrise foncière (règles)** : mandataire/liquidateur, convention/ORE pour public et acteurs environnementaux, négociation propriétaire + preneur (ORE sur fonds loué = accord préalable du preneur, art. L.132-3 C. env.), statuts RNE pour les sociétés civiles, approche multi-titulaires.

---

## 4. Recherche web (option `avec_recherche_web=True`)

But : ce que les API ne disent pas — projet d'aménagement ou de carrière, achat/vente de terrains dans la presse locale, contentieux, restructuration, objet réel d'une SCI/association, engagement environnemental.

**Garde-fous (2 appels)**
1. Appel **avec outil de recherche** (Gemini : Google Search grounding ; Mistral : outil `web_search` de l'API Conversations). On ne garde que les **segments appuyés par une source** renvoyée par l'outil (`grounding_supports` / `tool_reference`).
2. Appel **sans outil** qui ne fait que **classer** les extraits numérotés (catégorie, effet hausse/baisse, correspondance `siren` / `nom_commune` / `incertaine`). Tout constat non rattaché à un extrait sourcé ou à correspondance `incertaine` est écarté — essentiel pour les noms génériques (« SCI DE BEL AIR »).

**Les constats web n'entrent pas dans le score** : signaux à vérifier, affichés avec leur source.
**RGPD** : seules les données de la PM sont envoyées (dénomination, SIREN, communes) ; noms des dirigeants exclus par défaut (`inclure_dirigeants_web=False`). Pour votre argument souveraineté : `llm_provider="mistral"`.
**Coût** : avec Gemini 3, chaque requête de recherche lancée par le modèle est facturée → option à la demande depuis le front, pas en batch par défaut.

---

## 5. Sources

| Source | Statut V4 | Apport |
|---|---|---|
| Annuaire des Entreprises | ✅ corrigée | identité, NAF, dirigeants + âge, finances, compléments |
| BODACC v2.1 | ✅ corrigée | procédures, dissolution, radiation, comptes |
| DVF géolocalisées (communes) | ✅ corrigée | mutations, intervalle de détention |
| RPG 2010-2024 | ✅ corrigée | continuité PAC, déprise |
| Fichier des PM (DGFiP) | ✅ enrichi | IDU, patrimoine, co-titulaires, démembrement, droits réels |
| GPU / API Carto | 🆕 | zonage U/AU/A/N, parts de surface |
| Recherche web LLM | 🆕 option | constats sourcés |
| Sirene INSEE | ⏸ optionnel | redondant avec l'Annuaire |

**Pistes classées par rapport valeur / effort**
1. **RNE / INPI (compte gratuit)** — statuts PDF et actes : liste des associés de SCI, clauses d'agrément, majorités de cession, objet social → extraction LLM. Lève la principale incertitude de l'axe 2 (SCI). *Priorité 1.*
2. **Vos données internes** — croisement avec les autorisations Sitadel déjà persistées (PC/PA récent sur la parcelle = projet = dureté) et avec la filiation parcellaire Etalab (division récente = lotissement en préparation). Branchement direct via `Sources`.
3. **Prescriptions GPU** (votre module urba) — un **emplacement réservé** ouvre au propriétaire un droit de délaissement (le bénéficiaire peut devoir acquérir) ; EBC, SUP. Ajout d'une règle dès que le module expose ces entités.
4. **Fichiers fonciers / DV3F (Cerema, accès restreint)** — vraie date d'acte (`jdatat`), typologie vendeur/acquéreur → axe 4 exact et règle « ventes récentes de la PM » du guide. Accès via une collectivité cliente ou un mandat.
5. **RNA / JOAFE** — objet et statut RUP des associations.
6. **Fiche d'immeuble SPF (payante)** — servitudes, ORE, hypothèques : étape manuelle sur la shortlist.
À éviter : scraping Pappers/societe.com (CGU) ; bénéficiaires effectifs (accès restreint depuis 2022).

---

## 6. Compatibilité et migration

1. Copier `durete_v4/` dans le backend ; `pip install -r durete_v4/requirements.txt` (remplace `google-generativeai` par `google-genai`).
2. `from .durete_v4.service import run_durete_for_siren, run_durete_batch_in_memory` — mêmes signatures ; options V4 en kwargs (`avec_gpu`, `avec_recherche_web`, `llm_provider`, `domaine_public_confirme`).
3. Cache : `from .durete_v4.cache import get_cached_siren, set_cached_siren` — **même table, aucune migration** (ajouts dans `detail_axes.v4`). La version de cache suit `SCORING_VERSION` : changer un barème invalide l'ancien cache.
4. Sortie : clés V3 inchangées (`score_final`, `niveau_durete`, `explication`, `detail_axes.axeN/_note`, `surcharges`…) + `indice_confiance`, `confiance`, `score_fiable`, `recommandations`, `leviers`, `points_blocage`, `parcelles[]`, `bodacc`, `patrimoine`, `recherche_web`.
5. Variables d'environnement : `GEMINI_API_KEY` | `MISTRAL_API_KEY`, `DURETE_LLM_PROVIDER`, `DURETE_GEMINI_MODEL`, `DURETE_MISTRAL_MODEL`, `SUPABASE_URL(_PPM)`, `SUPABASE_KEY(_PPM)`, `DURETE_TABLE_PARCELLES_PM`, `DURETE_CACHE_DIR`, `DURETE_SURCHARGE_ZONAGE`, `INSEE_API_KEY` (optionnel).
6. **Révoquer la clé INSEE** exposée dans `sirene.py` V3.

---

## 7. Limites et points à valider

- **Endpoints non appelés en conditions réelles** : l'environnement de développement bloquait ces domaines. Le code suit les schémas documentés (Annuaire, BODACC v2.1, geo-dvf communes, WFS Géoplateforme, API Carto GPU, Gemini grounding, Mistral Conversations) avec parsing défensif, et 48 tests hors-ligne passent. **Premier geste** : `python -m durete_v4 analyse --siren 952824076 --idus 33213000BP0018 -v` puis vérifier en particulier les champs BODACC (`registre`, `jugement`) et la réponse API Carto `secteur-cc`.
- Barèmes `ext_v4` (NAF, zonage, faire-valoir direct +5, démembrement +3) : proposition à faire valider métier.
- DVF : pas d'Alsace-Moselle ni Mayotte (géré : statut `hors_dvf`), pas d'identité des parties, pas d'apports/successions.
- Fichier PM : les co-indivisaires personnes physiques n'y figurent pas.
- La correspondance des codes `code_droit` du fichier PM est limitée aux codes usuels ; vérifier les libellés de votre table.
- Les fixtures de test sont **synthétiques** (structure réelle, valeurs illustratives).

### Pool test (fixtures synthétiques, à refaire sur données réelles)

| PM | V3 (Gemini) | V4 | Lecture |
|---|---|---|---|
| SCI DE BEL AIR | 53 (10/20/15/8/+0) | **55** (10/10/12/8/+15) · conf. 70 | Axe 2 : le « 11 associés » V3 n'est pas étayé par les données publiques → neutre, confiance faible ; bail rural (vigne 7 ans) désormais vu |
| Fondation J. de Chabannes | 73 (26/20/15/12/+0) | **67** (26/18/15/8/+0) · conf. 90 | Finances 2024 lues ; détention par intervalle |
| CDC de Montesquieu | 83 (35/20/20/8/+0) | **98** (35/20/18/12/+13) · conf. 97 | Acquisition 2023, bail probable, 60 % en AU ; recommandation : convention/ORE plutôt qu'acquisition |
