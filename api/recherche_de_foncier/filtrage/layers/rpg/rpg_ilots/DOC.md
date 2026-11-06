# Module `rpg_ilots` — Îlots agricoles RPG comme unités foncières (KerFoncier)

> Emplacement : `api/recherche_de_foncier/filtrage/layers/rpg_ilots/`
> Millésime par défaut : **RPG 2025** — département chargé : **Gironde (33)**
> Rédigé : novembre 2026

## 1. Objectif

KerFoncier propose aujourd'hui deux types de candidats :

- les **parcelles cadastrales seules** (pipeline de filtrage principal) ;
- les **unités foncières personnes morales (UF PM)** : parcelles contiguës d'un même SIREN, découpées en sous-ensembles.

Ce module ajoute un troisième type : les **îlots agricoles RPG**.

> **Une UF îlot RPG = l'ensemble des parcelles cadastrales qui composent un îlot RPG**, c'est-à-dire des parcelles contiguës exploitées par **un même agriculteur**.

L'intérêt métier est de proposer **des zones plus grandes que la parcelle seule**, déjà cohérentes sur le terrain : un seul exploitant, un seul tenant, et parfois un seul propriétaire personne morale (`pm_unique`).

## 2. Principe de séparation

- **Module dédié**, indépendant des UF PM. Le code PM n'est pas modifié.
- La duplication de code (enrichissement CESBIO / faune) est **assumée** : on sait où se trouve le code RPG et où se trouve le code PM.
- Les seuls points de contact avec l'existant sont :
  - l'appel dans `filter_orchestrator.py` ;
  - la réutilisation, en lecture seule, de `compute_subset_score_eco`, `lookup_prospects_by_siren` et `_empty_pm_payload` dans `uf_profiling.py`.

## 3. Prérequis en base (préparés hors appli)

Le module lit deux tables préparées une fois par millésime et par département, à l'aide de scripts hors appli :

| Table | Produite par | Contenu |
|---|---|---|
| `ecocompensation.rpg2025_ilots` | `ingest_rpg.py` puis `build_rpg_ilots_cadastre.py` | Géométrie de l'îlot RPG, culture et catégorie dominantes, parts de prairie permanente, jachère et vigne, indicateurs PM (`nb_pm`, `pm_dominant`, `pm_unique`) |
| `ecocompensation.rpg2025_ilot_membre` | `build_rpg_ilots_cadastre.py` | Lien îlot → parcelle cadastrale (`num_ilot`, `idu`, `code_dep`, `code_insee`, parts de surface) |
| `ecocompensation.cadastre_parcelles` | (existant) | Cadastre Etalab partitionné par département |

**Règle d'appartenance d'une parcelle à un îlot** : une parcelle cadastrale est membre d'un îlot si **au moins 50 %** de sa surface est dans l'îlot (paramètre `--seuil` de `build_rpg_ilots_cadastre.py`).

Pour la provenance et l'analyse des données, voir `analyse_rpg_2025.md`.

## 4. Fonctionnement

```
filtre parcelles terminé
        │  config.include_ilots_rpg = True
        ▼
runner.run_ilots_rpg_after_filter   (tâche de fond, comme la phase UF PM)
        │
        ├─ build.run    AOI → îlots intersectant l'AOI → parcelles membres DANS l'AOI
        │               → 1 ligne par îlot dans ecocompensation_results.ilots_rpg
        ├─ enrich.run   veg_libelles (CESBIO) + fauna_distances (distance à l'obs. la plus proche)
        ├─ pool.build_response
        │               filtre sur colonnes → score éco → prospect PM → classement
        └─ save_results → ecocompensation.projects.last_results_ilots_rpg (jsonb)
        │
        ▼
WS : progress (layer_key="ilots_rpg") … puis phase:ilots_rpg_ready | error
```

### 4.1 `params.py`

`IlotsRpgParams` contient les seuils et le millésime. `params_from_config(config)` reprend **les mêmes seuils que les UF PM** depuis le `FilterConfig` :

| Paramètre | Source | Défaut |
|---|---|---|
| `min_area_ha` | `config.min_area_ha` | 0 |
| `min_parcelles` | fixé dans le module | **2** |
| `miller_thresh` | `config.miller_thresh` (None → 0) | 0 |
| `cesbio_libelles` | `config.cesbio_libelles` | [] |
| `fauna_criteria` | `config.fauna_criteria` | [] |
| `study_type` | `zones_humides_intra` si `search_mode == "within_foncier"`, sinon `faune_buffer` | |
| `min_zone_humide_ha` | `config.min_zone_humide_ha` | 0 |
| `limit` | fixé dans le module | 100 |
| `millesime` | fixé dans le module | `"2025"` |

### 4.2 `build.py`

1. Sélectionne les îlots de `rpg{millesime}_ilots` qui intersectent l'AOI.
2. Récupère leurs parcelles membres (`rpg{millesime}_ilot_membre`) en gardant **seulement celles qui intersectent l'AOI**.
3. Pour chaque îlot :
   - `idus` : la liste des parcelles membres ;
   - `geom_2154` : l'union des parcelles (`ST_Union`, réduite aux polygones) ;
   - `surface_ha`, `miller` (4πA/P²) et `dist_centre_m` (distance au centroïde de l'AOI) ;
   - les attributs recopiés depuis l'îlot : culture et catégorie dominantes, parts, indicateurs PM.
4. Garde les îlots avec `nb_parcelles >= min_parcelles` et `surface_ha >= min_area_ha`.
5. Supprime puis réinsère les lignes du projet. On peut donc relancer sans créer de doublons.

### 4.3 `enrich.py`

C'est la même logique que `enrich_uf.py`, sur la table `ilots_rpg` :

- `veg_libelles` / `veg_profile` : classes `libelle_prio` de `ecocompensation.vegetation_sur_cesbio` (`VEG_TABLE`). Surface calculée parcelle par parcelle (dalle contenue : `ST_Area(dalle)`, sinon intersection), puis sommé sur l'îlot. Le pourcentage est cette somme divisée par la surface de l'îlot. Traité par lots de 100 îlots.
- `fauna_distances` : `{espèce: distance_m}` vers l'observation la plus proche dans `ecocompensation.fauna` (valeur `-1` si aucune). Traité par lots de 300 îlots.
- Les deux colonnes sont remises à zéro avant enrichissement.

### 4.4 `pool.py`

**Le filtrage se fait uniquement sur des colonnes déjà calculées**, sans jointure spatiale :

- `miller >= miller_thresh` ;
- en mode faune, `veg_libelles && cesbio_libelles` (si des libellés sont fournis) ;
- s'il y a des critères faune, la distance de la **première espèce** doit être comprise entre 0 et `dist_m` (même règle que les UF PM) ;
- en mode ZH, `zone_humide_ha` est la somme sur les parcelles de l'îlot **présentes dans le pool parcelles** (`ecocompensation_results.parcelles`), avec le seuil `>= min_zone_humide_ha` si celui-ci est > 0.

Le tri SQL se fait par surface humide en mode ZH, sinon par surface. Viennent ensuite :

- `score_eco` via `compute_subset_score_eco` (0 à 6 points), calculé avec **tous** les critères faune ;
- `pm_prospect` via `lookup_prospects_by_siren(pm_dominant)`, qui indique si le SIREN a déjà accueilli des mesures compensatoires ;
- le classement final par (`score_eco`, surface), qui fixe le `rang`.

### 4.5 `runner.py`

- `run_sync(...)` enchaîne build, enrich, pool et la sauvegarde. Il est utilisable hors async.
- `run_ilots_rpg_after_filter(...)` est la version async appelée par l'orchestrateur : exécution dans un executor, événements WebSocket, erreurs loggées sans faire échouer le filtre parcelles.
- `save_results(...)` crée au besoin la colonne `projects.last_results_ilots_rpg` puis l'écrit.

### 4.6 `cli.py`

```bash
python -m api.recherche_de_foncier.filtrage.layers.rpg_ilots.cli \
  --project-id <uuid> --aoi-id <uuid> \
  --min-area-ha 7 --miller 0.3 \
  --cesbio "Prairies" --fauna "Nom espèce:1000" \
  [--zh --min-zh-ha 1] [--steps build enrich pool] \
  [--no-profiling] [--no-save] [--out ilots.json] [-v]
```

- On peut lancer chaque étape seule (`--steps enrich pool` pour refiltrer sans reconstruire).
- La CLI affiche le top 10 dans le terminal.
- Connexion : `--pg-url` ou `PG_URL`. Pour lire le `.env`, brancher `pg_url_depuis_env()` dans `_engine()`.

## 5. Données produites

### 5.1 Table `ecocompensation_results.ilots_rpg`

Clé : `(project_id, num_ilot)`. Une ligne par îlot candidat et par projet.

| Colonne | Description |
|---|---|
| `idus` | Parcelles cadastrales de l'UF |
| `nb_parcelles`, `surface_ha` | Calculés sur l'union des parcelles membres dans l'AOI |
| `miller`, `dist_centre_m` | Compacité, distance au centre de l'AOI |
| `cat_dominante`, `code_group_dominant`, `code_cultu_dominant` | Culture dominante de l'îlot (pondérée par la surface) |
| `part_pp`, `part_jachere`, `part_vigne` | Parts de surface de l'îlot RPG |
| `nb_pm`, `pm_dominant`, `pm_unique` | Propriété personnes morales (voir l'analyse) |
| `veg_libelles`, `fauna_distances` | Enrichissement |
| `geom_2154` | Union des parcelles membres (index GIST) |

### 5.2 JSON `projects.last_results_ilots_rpg`

```json
{
  "source": "rpg_ilots",
  "millesime": "2025",
  "total_candidats": 42,
  "total_ilots": 17,
  "ilots": [
    {
      "rang": 1, "num_ilot": "2919586", "idus": ["33…", "…"],
      "nb_parcelles": 12, "surface_ha": 18.4, "miller": 0.41, "distance_centre_km": 3.2,
      "cat_dominante": "PP", "code_group_dominant": 18, "code_cultu_dominant": "PPH",
      "part_pp": 0.92, "part_jachere": 0.0, "part_vigne": 0.0,
      "nb_pm": 1, "pm_dominant": "123456789", "pm_unique": true,
      "veg_libelles": ["Prairies"], "fauna_distances": {"Espèce": 450}, "dist_fauna_m": 450,
      "zone_humide_ha": 0.0,
      "score_eco": {"total_score": 5, "max_score": 6, "breakdown": {}},
      "pm_prospect": {"siren": "123456789", "parcelle_deja_en_mc": false}
    }
  ]
}
```

## 6. Intégration dans le code existant

| Fichier | Modification |
|---|---|
| `filter_pipeline.py` (`FilterConfig`) | `include_ilots_rpg: bool = False` |
| Endpoint ou parsing de la requête → `FilterConfig` | Transmettre `include_ilots_rpg` comme `include_unites_foncieres` |
| `filter_orchestrator.py` | Import de `run_ilots_rpg_after_filter` ; `"include_ilots_rpg"` dans `_filter_options_json` ; `asyncio.create_task(run_ilots_rpg_after_filter(...))` après le bloc UF PM |
| Front | Module dédié « Îlots agricoles » (à faire) : types, appel API et lecture de `last_results_ilots_rpg`, couche carte (UF et contour de l'îlot), fiche îlot |

## 7. Choix d'implémentation (et pourquoi)

| Choix | Raison |
|---|---|
| **Pas de sous-ensembles** : l'UF est l'îlot entier | Un îlot est déjà une unité cohérente (un exploitant, d'un seul tenant). Les utilisateurs trouvaient les sous-ensembles PM trop touffus. On évite aussi l'explosion combinatoire (jusqu'à 364 parcelles par îlot en Gironde) |
| **Géométrie = union des parcelles cadastrales**, pas le contour RPG | Les contours RPG ne suivent pas le cadastre. On filtre et on affiche ce qui est réellement mobilisable (des parcelles entières avec leurs propriétaires) |
| **Seules les parcelles dans l'AOI sont gardées** | Cohérent avec les UF PM, qui partent des parcelles de l'AOI |
| **`min_parcelles = 2`** | Un îlot d'une seule parcelle cadastrale est déjà présent dans le pool parcelles |
| **Membre si au moins 50 % de la parcelle est dans l'îlot** | Évite les parcelles de bordure et garantit qu'une parcelle n'appartient qu'à un seul îlot |
| **Mêmes seuils que les UF PM** | Comportement homogène pour l'écologue |
| **Filtre faune sur la 1re espèce seulement** | Aligné sur `build_uf_pool_response`. Toutes les espèces comptent dans le score |
| **`min_zone_humide_ha` appliqué en mode ZH** | ⚠️ Différence avec le code PM, qui reçoit ce paramètre sans l'appliquer. À harmoniser |
| **Les UF îlots ne repassent pas le filtre parcelles** | Comme les UF PM : phase séparée, après le filtre parcelles |
| **Pré-calcul de la composition îlot → parcelles, filtrage à la volée** | La composition définit l'entité (comme `pm_ilot_membre`). Le reste dépend du projet |

## 8. Limites connues

- **Îlot coupé par la limite de l'AOI** : seule la partie dans l'AOI est gardée, l'UF peut donc être partielle.
- **Pas de dénomination** de la personne morale dominante, seulement le SIREN (`pm_dominant`).
- **Les personnes physiques ne sont pas identifiées** : les fichiers DGFiP ne couvrent que les personnes morales.
- **Un îlot regroupe les parcelles d'un exploitant, pas d'un propriétaire.** Un îlot peut couvrir plusieurs propriétaires (voir `nb_pm`, `part_pm_dominant`).
- **Seul le 33** est chargé pour l'instant. Les îlots d'autres départements renverront un résultat vide tant qu'ils ne sont pas ingérés.
- **Les identifiants RPG (`num_ilot`) changent chaque année** (générés aléatoirement par l'IGN). Ils ne permettent pas de suivre un îlot d'un millésime à l'autre.

## 9. Tests

`tests/test_rpg_ilots.py` tourne contre un **PostGIS local jetable**. Il refuse de s'exécuter si l'URL ne pointe pas sur localhost, car il recrée les schémas `ecocompensation` et `ecocompensation_results`.

```bash
TEST_PG_URL=postgresql+psycopg2://postgres:<mdp>@localhost/<base_test> pytest tests/test_rpg_ilots.py
```

Ce qui est couvert :
- construction (seuils, îlot partiellement hors AOI, relance sans doublons) ;
- enrichissement CESBIO et faune ;
- filtres faune, CESBIO et ZH ;
- runner complet avec un `uf_profiling` simulé.

## 10. Mettre à jour les données (nouveau millésime ou département)

```bash
# 1. Ingestion RPG d'un département (contour exact, attributs dérivés)
python ingest_rpg.py --dir <dossier_gpkg> --dep 33 \
  [--ref-cultures REF_CULTURES_2025.csv --ref-groupes REF_CULTURES_GROUPES_CULTURES_2025.csv]

# 2. Composition îlots → parcelles cadastrales + indicateurs PM
python build_rpg_ilots_cadastre.py --dep 33          # environ 4 min pour la Gironde
python build_rpg_ilots_cadastre.py --dep 33 --skip-membres   # indicateurs seuls
```

Prérequis de performance : un index sur `ecocompensation.pm_ilot_membre (idu)`. Sans lui, le calcul des indicateurs passe de 25 s à environ 150 s.