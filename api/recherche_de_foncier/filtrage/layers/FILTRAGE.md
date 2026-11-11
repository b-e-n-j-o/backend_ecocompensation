# Référence filtrage

Source : panneau `CreateAoiPage`, `filter_pipeline.FilterConfig`, `national_exclusions.py`, `layer_runner.LAYER_REGISTRY`.
Les critères se cumulent (ET). La faune est un OU entre espèces. Ignorer un bloc = ne pas l’appliquer.

Ordre ci-dessous = ordre métier du panneau (et du compte rendu `FiltrageParam`), pas l’ordre SQL de l’entonnoir.

## 0. Zone de recherche (étape AOI, avant le panneau)

Géométrie initiale + tampon autour. Stocké sur `ecocompensation.aoi.buffer_m` (exposé en `buffer_km`).
Étude intra-périmètre (`zones_humides_intra` / `search_mode = within_foncier`) : tampon = 0.

| Champ | Défaut | Effet |
|---|---|---|
| `buffer_km` | saisi à la création | Rayon de recherche autour du foncier déposé |
| `search_mode` | `exclude_source_buffer` | `exclude_source` = hors emprise projet ; `within_foncier` = dans l’AOI |

## 1. Présence d’espèce(s)

Recherche `nom_vernaculaire`. Distance max observation ↔ parcelle réglable **par espèce**.

| Champ | Défaut | Effet |
|---|---|---|
| `fauna_criteria` | `[]` | `{species, dist_m}` ; vide = pas de filtre |
| `fauna_combine` | `or` | `or` = au moins une espèce ; `and` = toutes les espèces |

## 1b. Présence de flore

Jumeau de la faune, juste après dans l’entonnoir. Observations dans `ecocompensation.flore_observation` (`taxon_key`).

| Champ | Défaut | Effet |
|---|---|---|
| `flora_criteria` | `[]` | `{taxon_key, label, dist_m}` ; vide = pas de filtre ; `0` = intersection stricte |
| `flora_combine` | `or` | `or` = au moins un taxon ; `and` = tous les taxons |

## 2. Filtre géométrique

| Champ | Défaut | Effet |
|---|---|---|
| `min_area_ha` | 7.0 | Surface minimale du foncier (ha) |
| `miller_thresh` | `None` | Compacité ; UI souvent 0,39 si activé |

Miller : `(4 × π × aire) / périmètre² ≥ seuil`.

## 3. Occupation du sol

CESBIO : au moins un libellé intersecté. Vide = pas de filtre.

| Champ | Défaut |
|---|---|
| `cesbio_libelles` | `[]` |

Libellés : Autres, Bâtis denses, Bâtis diffus, Bois, Céréales à pailles, Colza, Eau, Forêt fermée de conifères, Forêt fermée de feuillus, Forêt fermée mixte, Forêt ouverte, Forêts de conifères, Forêts de feuillus, Haie, Lande herbacée, Lande ligneuse, Landes ligneuses, Maïs, Pelouses, Peupleraie, Plages et dunes, Prairies, Protéagineux, Soja, Surfaces minérales, Surfaces routes, Tournesol, Tubercules/racines, Verger, Vergers, Vigne, Vignes, Zones industrielles et commerciales.

## 4. Zones humides

Optionnel. Laisser sur Ignorer pour ne pas filtrer. Modes : `ignore` \| `intersect` \| `exclude`.

| Champ | Défaut | Effet |
|---|---|---|
| `zone_humide_mode` | `ignore` | ZH établies (`ecocompensation_results.zone_humide`) |
| `min_zone_humide_ha` | 0 | Seulement si établies = intersect ; 0 = toute intersection |
| `zones_humides_probables_mode` | `ignore` | ZH probables (`zones_humides_probables`, geom `geom`) |

## 5. Bassin versant

Optionnel. Uniquement les bassins versants qui touchent le foncier déposé.

| Champ | Défaut | Effet |
|---|---|---|
| `same_bassin_versant` | `False` | Parcelle dans un BV qui touche le foncier projet (`bv_spe_masse_d_eau`) |

## 6. Hydrographie

Optionnel. Cours d’eau et plans d’eau, seuls ou combinés. `None` = ignoré ; ≤ 0 = intersection stricte.

| Champ | Défaut | Table projet |
|---|---|---|
| `troncons_hydros_max_dist_m` | `None` | `troncons_hydros` |
| `surfaces_hydros_max_dist_m` | `None` | `surfaces_hydros` |

## 7. Arrachage de vignes

Optionnel. Sans type coché, toute intersection compte. Types : diversification, renaturation (boisements), renaturation (jachères). Colonne `c_div_re_1`.

| Champ | Défaut |
|---|---|
| `arrachage_vignes` | `False` |
| `arrachage_vignes_types` | `[]` |

## 8. Couches d’exclusion

Toute intersection écarte la parcelle. Clé API → défaut coché.

| Clé | Défaut | Source |
|---|---|---|
| `geomce` | oui | `geomce_surf` / `geomce_lin` / `geomce_pct` (`deleted_at` nul) |
| `preemption_ens` | oui | `preemption_espaces_naturels_sensibles` |
| `ens` | oui | `espaces_naturels_sensibles_ens` |
| `natura_2000` | oui | `natura_2000` |
| `znieff_1` | oui | `znieff` où `type_znieff = 1` |
| `znieff_2` | non | `znieff` où `type_znieff = 2` |
| `ebc` | non | `v_ebc`, exclusion seulement si couverture 100 % (hors EBC à créer) |

`excluded_layers` : `string[]` de ces clés. Lecture sur tables nationales `ecocompensation.*`.

## Autre champ config

| Champ | Défaut | Effet |
|---|---|---|
| `include_unites_foncieres` | `True` | `False` = pool parcelles sans UF. Le calcul est `unites_foncieres/`, après le filtre, pas une couche de `layer_runner` |

## Couches projet (`layer_runner`)

Découpe AOI → tables `ecocompensation_results.*`, avant ou pour le filtre.

| Clé | Rapide | Table | Rôle |
|---|---|---|---|
| `parcelles` | oui | `parcelles` | Candidats cadastraux |
| `zone_humide` | oui | `zone_humide` | ZH établies |
| `espaces_naturels_sensibles_ens` | oui | `espaces_naturels_sensibles_ens` | ENS (copie projet) |
| `preemption_ens` | oui | `preemption_espaces_naturels_sensibles` | Préemption ENS (copie projet) |
| `troncons_hydros` | oui | `troncons_hydros` | Cours d’eau BD TOPO |
| `surfaces_hydros` | oui | `surfaces_hydros` | Plans d’eau BD TOPO |
| `zones_humides_probables` | non | `zones_humides_probables` | ZH probables |
| `bd_topo_et_cesbio` | non | `bd_topo_et_cesbio` | Occupation du sol |

## Champs enrichis sur les survivantes

`parcelles` : `veg_libelles`, `fauna_distances`, `flora_distances`, `zone_humide_ha`, `dist_hydro_m`, `troncons_hydro_info`, `dist_surface_hydro_m`, `surface_hydro_ha`, `surfaces_hydro_info`.
Config enregistrée : `ecocompensation.projects.filter_config` (jsonb). Snapshot du calcul : `last_results.filter_config` / `result_summary.filter_config`.
