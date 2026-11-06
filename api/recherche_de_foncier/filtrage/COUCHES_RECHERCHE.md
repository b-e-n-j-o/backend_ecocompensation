# Couches interrogées par la recherche de foncier

Recueil des données lues par le pipeline de filtrage (`POST /api/projects/{id}/filter-pipeline`). La fiche structurée est `couches_recherche.json`, dans ce même dossier.

Ce n’est pas le catalogue carte des données internes (`api/reserve_fonciere/catalog.py`, route `GET /api/data-interne/layers`). Celui-là sert l’affichage Web SIG. Il ne contient ni la faune, ni l’hydrographie, ni les zones humides probables, ni les ENS, ni le cadastre utilisé comme candidats.

La colonne **Emprise** reprend seulement ce qui est déjà écrit dans le code ou dans ce catalogue carte. Le reste est à vérifier en base.

Deux schémas :

- `ecocompensation.*` — référentiels, sans `project_id`
- `ecocompensation_results.*` — extrait du projet, écrit pendant la recherche
- `geo.zones_humides_probables` — raster national de probabilité

## Périmètre du projet

Ces tables ne filtrent pas le thème. Elles définissent où l’on cherche.

| Rôle | Table |
|---|---|
| Zone de recherche | `ecocompensation.aoi` |
| Emprise du projet (foncier déposé) | `ecocompensation.foncier` |
| Parcelle(s) initiale(s) | `ecocompensation.project_parcelles` |
| Projet | `ecocompensation.projects` |

## Toujours exécutées

| Couche | Table source | Table écrite pour le projet | Rôle |
|---|---|---|---|
| Parcelles cadastrales candidates | `ecocompensation.parcelles` | `ecocompensation_results.parcelles` | Tuilage dans l’AOI, puis filtre surface minimale |
| Unités foncières | `public.parcelles_personnes_morales` (base PPM), repli `ecocompensation.parcelles_personnes_morales` | `ecocompensation_results.sous_ensembles`, `ecocompensation_results.unites_foncieres` | Regroupement propriétaire après le filtre |

Emprise du cadastre et du fichier personnes morales : à vérifier.

## Critères optionnels

Aucun de ces critères n’est obligatoire. S’il n’est pas activé, la table source n’est pas utilisée pour éliminer des parcelles.

| Critère dans le formulaire | Table source | Clip projet (si le critère est actif) | Lecture pendant le filtre |
|---|---|---|---|
| Occupation du sol (CESBIO) | `ecocompensation.vegetation_sur_cesbio` (`VEG_TABLE`) | non (requête directe) | `ST_Intersects` sur `libelle_prio` |
| Espèces | `ecocompensation.fauna` | non (requête directe) | `ST_DWithin` sur `nom_vernaculaire` |
| Zones humides établies | `ecocompensation.zone_humide` | `ecocompensation_results.zone_humide` | intersection, ou surface minimale en ha |
| Zones humides probables | `geo.zones_humides_probables` (raster) | `ecocompensation_results.zones_humides_probables` | intersection ou exclusion |
| Cours d’eau | `ecocompensation.troncons_hydros` | `ecocompensation_results.troncons_hydros` | intersection ou distance max |
| Plans d’eau | `ecocompensation.surfaces_hydros` | `ecocompensation_results.surfaces_hydros` | intersection ou distance max |

Emprises déjà notées dans le catalogue carte, à revérifier :

- `vegetation_sur_cesbio` : emprise commentée « Gironde » dans `catalog.py` (`BBOX_VEGETATION_CESBIO`). Partition BD TOPO puis CESBIO, géométries en dalles. Filtre, surfaces et libellés lisent `VEG_TABLE`. La table `vegetation_sur_cesbio_sub` n'existe plus. L'affichage carte est un remplissage sans contour, et la copie projet regroupe les dalles par classe (`ST_Union`).
- `zone_humide` : emprise commentée « Adour-Garonne / RPDZH » (`BBOX_ZONE_HUMIDE`). Le libellé catalogue est « Zones humides (RPDZH) ».

Faune, hydrographie, raster de zones humides probables : emprise à vérifier.

## Exclusions nationales

Activées par défaut (GEOMCE, préemption ENS, ENS, Natura 2000). Le filtre lit les tables nationales directement, sans clip préalable.

| Critère | Tables source |
|---|---|
| Mesures compensatoires (GEOMCE) | `ecocompensation.geomce_surf`, `ecocompensation.geomce_lin`, `ecocompensation.geomce_pct` |
| Préemption ENS | `ecocompensation.preemption_espaces_naturels_sensibles` |
| Espaces naturels sensibles | `ecocompensation.espaces_naturels_sensibles_ens` |
| Natura 2000 | `ecocompensation.natura_2000` |

Le catalogue carte indique GEOMCE comme France métropolitaine (hexagone et Corse), à revérifier. ENS, préemption ENS et Natura 2000 : emprise à vérifier.

Des clips existent pour l’affichage carte, pas pour l’exclusion :

- `ecocompensation_results.espaces_naturels_sensibles_ens`
- `ecocompensation_results.preemption_espaces_naturels_sensibles`

Ils ne sont chargés que pour une étude « dans le foncier » (`zones_humides_intra`). La recherche actuelle (`faune_buffer`) ne les charge pas.

## Après le filtre, sur les parcelles retenues

| Étape | Tables lues | Quand |
|---|---|---|
| Enrichissement végétation | `ecocompensation.vegetation_sur_cesbio` | toujours, sur les survivantes |
| Enrichissement espèces | `ecocompensation.fauna` | si des espèces ont été choisies |
| Enrichissement zone humide | `ecocompensation_results.zone_humide` | si le critère ZH établie est actif |
| Enrichissement hydro | `ecocompensation_results.troncons_hydros`, `ecocompensation_results.surfaces_hydros` | si le critère hydro correspondant est actif |
| Profilage personnes morales | base PPM `public.parcelles_personnes_morales` | recherche actuelle |
| Profilage occupation du sol | `ecocompensation.vegetation_sur_cesbio` | recherche actuelle |
| Clip `bd_topo_et_cesbio` | même source `ecocompensation.vegetation_sur_cesbio`, vers `ecocompensation_results.bd_topo_et_cesbio` | seulement l’ancienne étude zones humides |

## Absent du filtre

Présents dans le catalogue carte ou dans d’anciens modules, mais non lus par la recherche actuelle : parcelles syndicats, foncier privé, parcelles prospects (`reserve_fonciere`, `parcelles_prospects_filtered`), EBC, ZNIEFF, réserves naturelles, Carhab, CoSIA, remontée de nappes.
