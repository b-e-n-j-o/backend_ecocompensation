# RPG 2025 — Analyse des données et pistes pour la recherche de foncier compensatoire

> Périmètre analysé : **Nouvelle-Aquitaine (R75)**, ingestion et analyse détaillée sur la **Gironde (33)**
> Rédigé : novembre 2026
> Document compagnon : `rpg_ilots_module.md` (implémentation dans KerFoncier)

---

## 1. Source et téléchargement

Le **Registre Parcellaire Graphique (RPG)** rassemble les parcelles agricoles déclarées à la PAC. Il est géré par l'ASP, produit et diffusé par l'IGN, et le ministère de l'Agriculture en fixe les règles de diffusion. La version diffusée est **anonymisée** et publiée une fois par an.

- **Millésime 2025** : annoncé par l'IGN le **23 octobre 2026**. Il correspond à la campagne de déclaration 2025 ; le décalage d'environ un an est normal.
- **Nommage** : `RPG_4-0__GPKG_LAMB93_R75_2025-01-01`. La date `AAAA-01-01` désigne le millésime. Dans le flux Atom, les fichiers v4.0 sont listés à la fin, c'est pourquoi on ne voyait d'abord que des liens 2023.
- **Liens directs** (Nouvelle-Aquitaine, GeoPackage, Lambert-93) :
  - 2025 : `https://data.geopf.fr/telechargement/download/RPG/RPG_4-0__GPKG_LAMB93_R75_2025-01-01/RPG_4-0__GPKG_LAMB93_R75_2025-01-01.7z` (environ 1,68 Go, MD5 `849eea023bad7e43bfb07ccb577abf1f`)
  - 2024 (rediffusé au modèle v4) : `https://data.geopf.fr/telechargement/download/RPG/RPG_4-0__GPKG_LAMB93_R75_2024-01-01/RPG_4-0__GPKG_LAMB93_R75_2024-01-01.7z` (environ 1,88 Go, MD5 `a82c1534b928a54394af181e54fbd2ec`)
- **Documentation** : *RPG v4.0, Descriptif de contenu et de livraison* (révision sept. 2026), `DC_DL_RPG_4.0.pdf`.
- **Nomenclatures** (non incluses dans l'archive, CSV séparés sur cartes.gouv.fr) : `REF_CULTURES_[année].csv`, `REF_CULTURES_GROUPES_CULTURES_[année].csv`, `REF_CULTURES_DEROBEES_[année].csv` (ce dernier n'est plus utilisé depuis 2025). Alternative : le jeu « Codes cultures PAC » sur data.gouv.
- **Flux WFS / WMTS** : `https://data.geopf.fr/annexes/ressources/wfs/agriculture.xml`
- **Licence** : Licence Ouverte Etalab 2.0.

### Évolution du modèle en 2025

Les couches GCP, PP, IAE et BIO ne sont plus fournies à part en téléchargement. Elles sont intégrées **sous forme d'attributs** dans la couche Parcelles et restent disponibles séparément en flux.

## 2. Contenu de l'archive

| Fichier / couche | Géométrie | Attributs | Usage pour nous |
|---|---|---|---|
| `RPG_Parcelles` | MultiPolygon | `id_parcel`, `surf_parc`, `cat_cult_p`, `code_cultu`, `code_group`, `precision`, `gcp`, `pp`, `bio`, `iae` | Culture déclarée |
| `RPG_Ilots` | MultiPolygon | `num_ilot` | **Regroupement en unités foncières** |
| `RPG_ZDH` | MultiPolygon | `id_zdh`, `prorata` | État des prairies (fermeture) |
| `RPG_SNA` – `SNA_VEGETATION_SURFACIQUE` | MultiPolygon | `numero_sna`, `categorie`, `type`, `camp_deb`, `camp_fin`, `surface`, `longueur`, `largeur`, `nb_arbres`, `mur_tradi` | Haies, bosquets, broussailles, forêt |
| `RPG_SNA` – `SNA_AUTRES_ELEMENTS` | MultiPolygon | idem | Mares, fossés, surfaces en eau, murs |
| `RPG_SNA` – `SNA_VEGETATION_PONCTUELLE` | Point | idem | Arbres isolés |
| `RPG_SNA` – `SNA_ELEMENT_ARTIFICIALISE` | MultiPolygon | idem | Bâtiments, routes : **non ingéré** (doublon BD TOPO) |

## 3. Ce que dit la documentation (et ce que les données montrent)

### 3.1 Points essentiels

- **Un îlot regroupe des parcelles contiguës exploitées par un même agriculteur.** Il est délimité par des éléments permanents (route, chemin, ruisseau, haie…) ou par d'autres exploitations. **Exploitant ≠ propriétaire** : une grande partie de la SAU est en fermage.
- **Une parcelle RPG n'est pas une parcelle cadastrale.** Elle est dessinée par l'agriculteur sur l'orthophoto (Telepac) et suit l'usage, pas la propriété. Une parcelle RPG peut couvrir plusieurs parcelles cadastrales, ou seulement une partie d'une.
- **Aucun lien attributaire parcelle → îlot** dans le RPG : il est reconstruit spatialement.
- **Aucun identifiant d'exploitant** : deux îlots non contigus d'un même agriculteur ne peuvent pas être reliés.
- **Les identifiants sont annuels et aléatoires** (`id_parcel`, `num_ilot`, `numero_sna`, `id_zdh`). Toute comparaison entre millésimes doit donc être **spatiale**.

### 3.2 Attributs : sémantique réelle

| Attribut | Ce qu'il faut savoir |
|---|---|
| `surf_parc` | Hectares. Le ratio avec la surface géométrique vaut environ 1,00 (vérifié) |
| `cat_cult_p` | `TA` terres arables, `CP` cultures permanentes, `PP` prairies permanentes, `SB` surfaces boisées, `NA` cultures diverses, **`NULL` = « Pas de cultures »** |
| `NULL` en Gironde | Ce sont les codes `BOR`, `BTA`, `BFS` (groupe 28) : bordures et bandes non cultivées |
| `gcp` | ⚠️ **Incohérent avec la doc** : `gcp = 0` correspond à `NA`, et les `NULL` ont `gcp = 1`. **À ne pas utiliser**, se baser sur `cat_cult_p` |
| `pp` | Rempli seulement si `cat_cult_p` est renseigné |
| `precision` | **Précision culturale** (ex. 001 = récolte en grain, 002 = ensilage), pas une précision géométrique. Inutile pour nous |
| `code_group` | Groupes déduits des effectifs (à confirmer avec `REF_CULTURES_GROUPES_CULTURES`) : 1 blé tendre (BTH), 2 maïs (MIS), 6 tournesol (TRN), **11 gel / jachères (JAC)**, **18 prairies permanentes (PPH)**, 19 prairies temporaires (PTR), **21 vigne (VRC)**, 28 divers (SNE, BOR, BTA, BFS…) |
| SNA `surface` | **En ares** (×100 pour avoir des m²) |
| SNA `longueur`, `largeur` | Mètres. Longueur obligatoire pour les haies |
| SNA `nb_arbres` | Seulement pour les « arbres alignés ». Présence de valeurs aberrantes (ex. 2018) |
| SNA `camp_deb` | Campagne d'**apparition dans la couche** (saisie), pas date de plantation |
| SNA `camp_fin` | Renseigné seulement si l'élément **a disparu au cours de cette campagne**. Pas d'historique des disparitions antérieures |
| ZDH `prorata` | Tranches `<10`, `10-30`, `30-50`, `50-80`, `>80` : part d'éléments **non admissibles** (broussailles, ligneux, rochers, sol nu) |

### 3.3 ZDH : ce que c'est vraiment

- Ce sont des zones de **densité homogène** sur les **prairies permanentes**, issues du contrôle de l'admissibilité aux aides PAC.
- Elles mesurent un **potentiel fourrager**, pas une valeur écologique.
- Elles ne sont saisies qu'à partir de **0,5 ha**.
- Elles sont entretenues seulement sur les prairies permanentes, mais **une ZDH peut perdurer quand la parcelle change de catégorie**.
- **Elles ne doivent pas servir à calculer des surfaces exploitées** (doc IGN).

## 4. Audit des données (emprise rectangulaire de la Gironde)

Script : `audit_rpg.py` (lecture directe des GeoPackage, sans ingestion en base).

| Couche | Objets | Remarques |
|---|---|---|
| Parcelles | 248 247 | 4 géométries invalides. TA 40,7 %, CP 31,6 %, PP 13,9 %, NULL 9,2 %, NA 4,6 % |
| Îlots | 141 879 | 1 géométrie invalide, pas de doublon de `num_ilot` |
| Lien parcelle → îlot | | 7 parcelles orphelines, 4 dans plusieurs îlots, 3 îlots vides : **lien spatial fiable** |
| Parcelles RPG par îlot | médiane 1, moyenne 1,75, max 72 | **67,6 % des îlots ne contiennent qu'une parcelle RPG** |
| ZDH | 54 382 (121 000 ha) | 92,5 % en `<10`. Environ 4 000 zones (7 400 ha) en 10-80 / >80 |
| ZDH sans aucune parcelle 2025 | 11 790 (environ 18 000 ha) | Ancienne prairie hors déclaration, **ou** donnée non mise à jour |
| SNA végétation surfacique | 246 739 | 97 k haies, 49 k bosquets, 38 k forêts, 33 k arbres alignés, 9,6 k broussailles |
| SNA autres éléments | 56 567 | 33 k fossés non maçonnés, 11,2 k mares, 10,5 k surfaces en eau |
| SNA arbres isolés | 302 928 | |
| SNA disparus en 2025 | quelques centaines | `camp_fin` ne contient que 2025 |

## 5. Ingestion en base (département 33 exact)

Script : `ingest_rpg.py`. Contour du département via ADMIN EXPRESS. Un objet est rattaché au département qui contient son **point intérieur** (pas de doublon aux frontières). Durée : environ 2 minutes.

| Table | Lignes | Détail |
|---|---|---|
| `rpg2025_parcelles` | 89 679 | CP 39 958, TA 26 559, PP 13 268, NULL 6 289, NA 3 605. 5 sans îlot |
| `rpg2025_ilots` | 54 635 | 14 698 avec au moins 2 parcelles RPG |
| `rpg2025_zdh` | 23 189 | ouvert 21 019, en fermeture 1 324, très fermé 846, **hors parcelle 2025 : 6 273 (27 %)** |
| `rpg2025_sna` | 132 156 | haies 25 967, fossés non maçonnés 25 656, bosquets 21 411, forêts 16 391, arbres alignés 15 921, broussailles 6 333, **mares 4 695**, surfaces en eau 2 670. 197 disparus en 2025 |
| `rpg2025_sna_arbres` | 145 319 | |

### Attributs dérivés à l'ingestion

| Table | Colonnes ajoutées |
|---|---|
| Toutes | `code_dep` ; géométries corrigées (`make_valid`) et converties en MultiPolygon |
| `rpg2025_parcelles` | `num_ilot` (lien spatial reconstruit), `non_cultivee` (`cat_cult_p` NULL), `jachere` (groupe 11), `vigne` (groupe 21), `prairie_permanente` (`cat_cult_p` = PP), `surf_geom_ha`, libellés si les CSV de référence sont fournis |
| `rpg2025_ilots` | `nb_parcelles`, `surf_ha`, `surf_parcelles_ha`, `part_pp`, `part_jachere`, `part_vigne`, `part_non_cultivee`, `cat_dominante`, `code_group_dominant`, `code_cultu_dominant` |
| `rpg2025_zdh` | `prorata_ordre` (1 à 5), `etat_milieu` (**lecture métier, non officielle** : 1 ouvert, 2-3 en fermeture, 4-5 très fermé), `part_sous_parcelles`, `hors_parcelle_millesime` (**indice à vérifier, pas une preuve**), `surf_ha` |
| `rpg2025_sna` | `couche` (végétation ou autres, fusionnées), champs numériques convertis, `surface_m2`, `nb_arbres` > 500 mis à NULL, `disparu` |

## 6. Passage au cadastre : îlots → unités foncières

Script : `build_rpg_ilots_cadastre.py`. Calcul commune par commune, environ 4 minutes pour le 33.

- `rpg2025_ilot_membre` : une parcelle cadastrale est membre d'un îlot si **au moins 50 %** de sa surface est dans l'îlot.
- Indicateurs ajoutés à `rpg2025_ilots` : `nb_parcelles_cad`, `surf_cad_m2`, `part_ilot_couverte`, `nb_pm`, `part_surf_pm`, `pm_dominant`, `part_pm_dominant`, `pm_unique` (une seule personne morale détient au moins 95 % de la surface membre).

### Résultats (Gironde)

| Indicateur | Valeur | Lecture |
|---|---|---|
| Liens îlot → parcelle cadastrale | 230 648 | |
| Îlots avec au moins 2 parcelles cadastrales | **33 126 (61 %)** | La majorité des îlots regroupe plusieurs parcelles cadastrales, alors que 2/3 n'ont qu'une parcelle RPG |
| Parcelles cadastrales par îlot | moyenne 4,2, max 364 | |
| Couverture moyenne de l'îlot par ses membres | 86 % | Le reste correspond aux parcelles de bordure (moins de 50 % dans l'îlot) |
| Îlots sans membre | 3 832 | Petits îlots, non convertibles en UF |
| Îlots avec au moins une personne morale | 18 101 | |
| **`pm_unique`** | **12 372 îlots, 46 035 ha** | 1 exploitant + 1 propriétaire personne morale |

**`pm_unique` par catégorie dominante :**

| Catégorie | Îlots | ha |
|---|---|---|
| CP (essentiellement vigne) | 8 465 | 23 876 |
| TA | 2 500 | 15 585 |
| PP | 1 159 | 6 153 |
| NA | 248 | 420 |

➡️ **Hors vigne : environ 3 900 îlots et 22 000 ha** avec un seul exploitant et un seul propriétaire personne morale. C'est le chiffre pertinent pour la compensation. La vigne `pm_unique` correspond surtout à des châteaux (SCEA, GFA) : foncier cher et peu mobilisable.

**Îlots très morcelés** (plus de 100 parcelles cadastrales, ex. 364 parcelles pour 86 ha, 267 parcelles de vigne pour 30 ha) : ce ne sont pas des anomalies, c'est du **morcellement cadastral**. Un îlot comme le `1005380` (138 parcelles, 9 ha, aucune personne morale) appartient probablement à des dizaines de propriétaires personnes physiques.

## 7. Hypothèses métier pour la recherche de foncier compensatoire

> Les grilles ci-dessous sont des **propositions**. Elles doivent être validées par l'écologue et l'urbaniste avant d'entrer dans un scoring.

### 7.1 Filtrage du pool

| Critère | Logique | Justification |
|---|---|---|
| Exclure la vigne | `part_vigne > 0,5` (option activée par défaut) | Foncier cher, AOC, très peu mobilisable, **sauf vignes arrachées** (voir le diff 2024/2025) |
| Cibler jachère et non cultivé | `part_jachere + part_non_cultivee > seuil` | Faible valeur agricole, couvert souvent déjà permanent |
| Prairies permanentes | `cat_dominante = 'PP'` | Support classique de gestion extensive |
| Hors RPG | Parcelle sans recouvrement RPG | **Catégorie à part, pas une exclusion** : friche, boisement, délaissé… À qualifier avec OCS GE / CoSIA |

### 7.2 Potentiel écologique : type de mesure plausible

| Situation | Mesure typique | Gain attendu |
|---|---|---|
| Terres arables, grandes cultures | Conversion en prairie, plantation de haies | **Fort** (gain démontrable) |
| ZDH en fermeture (classes 2 à 4) | Réouverture de milieux, débroussaillage, pâturage extensif | Fort pour les espèces de milieux ouverts |
| Prairie permanente ouverte | Gestion extensive, retard de fauche | Moyen (additionnalité à justifier) |
| Jachère, non cultivé | Gestion ou restauration selon l'état | Variable |
| Faible linéaire de haies sur une grande surface | Plantation de haies | Fort, mesure très courante |
| Présence de mares ou de fossés | Restauration de mares, de zones humides | Fort pour les amphibiens |

**Point de vigilance sur l'additionnalité** : une prairie déjà très riche et stable offre peu de gain. L'intérêt est maximal sur les milieux **en cours de fermeture**, pas sur ceux déjà boisés.

**ZDH, lecture à rappeler** : « dégradé » au sens PAC (faible valeur fourragère) n'est pas « dégradé » au sens écologique. Une prairie avec 30 à 50 % de broussailles peut être une lande ou une pelouse sèche d'intérêt, et c'est souvent un **milieu en cours de fermeture** propice à une mesure de réouverture. Ce n'est pas de la « renaturation » au sens ZAN (désartificialisation).

### 7.3 Dureté foncière : proxys tirés du RPG

| Indicateur | Effet sur la dureté |
|---|---|
| Valeur d'usage : vigne/vergers > grandes cultures > prairie permanente > jachère / non cultivé > hors RPG | Plus l'usage a de la valeur, plus la parcelle est dure à obtenir |
| Parcelle déclarée PAC | **Exploitant en place**, probablement avec bail : un accord supplémentaire est nécessaire, dureté en hausse |
| Flag `bio` | Engagement de l'exploitant, conversion lourde difficile, mais souvent plus réceptif à des mesures de gestion |
| ZDH à prorata faible (beaucoup d'éléments non admissibles) | Faible valeur PAC, donc moins à perdre pour l'exploitant : dureté en baisse |
| Grand îlot | Grosse exploitation, perdre de la surface est moins critique |
| **Densité parcellaire** (`nb_parcelles_cad / surf_ha`) | Morcellement élevé, beaucoup de propriétaires probables : dureté forte, **sauf si `pm_unique`** |
| `pm_unique` / `part_pm_dominant` | Un seul interlocuteur propriétaire : dureté en baisse |
| SIREN déjà hôte de mesures compensatoires (GEOMCE) | Dureté en baisse (déjà en place via `pm_prospect`) |

### 7.4 Regroupements possibles

1. **Par îlot RPG** (implémenté) : parcelles cadastrales d'un même exploitant.
2. **Îlot ∩ unité foncière PM** : un exploitant et un propriétaire, c'est la cible idéale. Déjà approché via `pm_unique`.
3. **Massifs** : îlots contigus de même catégorie, fusionnés quel que soit l'exploitant (`ST_ClusterDBSCAN`). Ça donne de grands ensembles de prairies ou de terres arables (quatrième type de candidat).

## 8. Travaux possibles pour aller plus loin

### Court terme
- [ ] **Front « Îlots agricoles »** : section dédiée, couche carte, fiche îlot.
- [ ] **Nomenclatures** : récupérer `REF_CULTURES_2025.csv` et `REF_CULTURES_GROUPES_CULTURES_2025.csv`, relancer l'ingestion avec `--ref-*` et confirmer la correspondance `code_group` → libellé.
- [ ] **Dénomination** de la personne morale dominante (table SIREN → dénomination).
- [ ] **Harmoniser** l'application de `min_zone_humide_ha` entre UF PM et îlots.
- [ ] **Filtres agricoles optionnels** sur les îlots : exclusion de la vigne, cible prairies / jachères.

### Moyen terme
- [ ] **Diff RPG 2024 → 2025** (le RPG 2024 est disponible au même modèle v4), par croisement **spatial** puisque les identifiants sont annuels :
  - vignes arrachées (VRC → jachère / SNE / disparition), qui créent du foncier potentiellement mobilisable (en lien avec `arrachage_vignes` dans `FilterConfig`) ;
  - prairies permanentes retournées ;
  - parcelles sorties de la PAC ;
  - haies et mares disparues (`camp_fin`), qui demandent d'accumuler les millésimes.
- [ ] **Qualifier les 6 273 ZDH hors parcelle 2025** avec le 2024 et OCS GE : déprise réelle ou donnée non mise à jour ?
- [ ] **Enrichissement agricole des candidats** (à la volée, sur le pool, comme le module urba) : culture majoritaire, % sous RPG, linéaire de haies, nombre de mares, nombre d'arbres, prorata ZDH pondéré, **type de mesure plausible**.
- [ ] **Scoring potentiel écologique et dureté** à partir des grilles du §7, validé par l'écologue.
- [ ] **Massifs agricoles** (`ST_ClusterDBSCAN`), quatrième type de candidat.

### Long terme
- [ ] **Autres départements** de Nouvelle-Aquitaine : `ingest_rpg.py --dep XX` puis `build_rpg_ilots_cadastre.py --dep XX`. Surveiller l'espace disque Supabase (90 Go provisionnés, environ 36 Go utilisés après nettoyage de CoSIA et CarHab).
- [ ] **Zonage ICHN** (annoncé par l'IGN « courant 2026 », pas encore diffusé) : indicateur de handicap naturel et de faible rentabilité agricole.
- [ ] **Historique multi-millésimes** des SNA pour détecter les arrachages de haies.
- [ ] **Croisement RPG × filiation cadastrale** : stabilité de l'usage et de la propriété dans le temps.
- [ ] **Hors RPG × OCS GE / CoSIA** pour qualifier les friches et délaissés.

## 9. Scripts de la chaîne de données

| Script | Rôle | Sortie |
|---|---|---|
| `audit_rpg.py` | Audit des GeoPackage avant ingestion | `report.md`, CSV, `summary.json` |
| `ingest_rpg.py` | Ingestion d'un département et attributs dérivés | `rpg2025_parcelles`, `_ilots`, `_zdh`, `_sna`, `_sna_arbres` |
| `build_rpg_ilots_cadastre.py` | Composition îlots → cadastre et indicateurs PM | `rpg2025_ilot_membre`, colonnes ajoutées à `rpg2025_ilots` |

Tous ces scripts se lancent en CLI, ont des logs détaillés et des tests pytest.