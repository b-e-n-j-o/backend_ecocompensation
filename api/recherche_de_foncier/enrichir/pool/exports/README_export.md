# Export SIG

Tous les scripts d’export vivent dans ce dossier. Le livrable à ouvrir dans QGIS est un GeoPackage EPSG:2154. Les shapefiles sont une copie des mêmes objets, avec les textes coupés à 254 caractères (limite DBF).

## Ce qu’on obtient

### Étude complète

`GET /api/projects/{id}/export/etude.gpkg?layers=parcelles,uf,ilots`

`write_etude_geopackage` (`export_etude_gpkg.py`) empile uniquement les couches demandées, puis ajoute toujours le contexte du projet s’il existe.

| Couche | Quand | Contenu |
|---|---|---|
| `parcelles` | `layers` contient `parcelles` | Une ligne = une parcelle du classement (hors indésirables) |
| `unites_foncieres` | `layers` contient `uf` | Une ligne = une unité foncière personne morale |
| `uf_parcelles` | idem | Une ligne = une parcelle membre d’une UF |
| `dictionnaire` | idem | Table sans géométrie : colonne, description, unité, règle d’agrégation |
| `ilots_rpg` | `layers` contient `ilots` | Une ligne = un îlot RPG retenu |
| `ilots_parcelles` | idem | Une ligne = une parcelle cadastrale membre d’un îlot |
| `zone_projet` | toujours si la géométrie existe | Emprise du foncier projet |
| `aire_etude` | toujours si l’AOI existe | Aire de recherche |

`uf_combinaisons` n’est plus écrite.

### Exports séparés

`GET /api/projects/{id}/export/shp?scope=parcelles|uf|indesirables` renvoie un zip.

- `parcelles` : shapefile tronqué + `parcelles.gpkg` (parcelles, zone projet, aire d’étude).
- `uf` : `unites_foncieres.shp`, `parcelles.shp` (les parcelles des UF, textes tronqués) et `uf.gpkg` (UF, parcelles, dictionnaire, zone projet, aire d’étude).
- `indesirables` : même schéma que les parcelles, limité aux parcelles marquées indésirables.

`GET /api/projects/{id}/export/csv?scope=parcelles|uf|indesirables` est le tableau, sans géométrie. Le CSV UF n’est pas le GeoPackage agrégé : c’est la liste des parcelles d’UF (`export_uf_classement_csv.py`).

## Parcelles du classement

`export_classement_shp.build_parcelles_geodataframes` lit le classement déjà calculé (`last_results` ou un `run_id`) et les métriques du run pool (`get_all_metrics_grouped_by_idu`).

Pour chaque parcelle :

1. `mmap_for_parcelle` regroupe les métriques (`filter_enrich`, `cesbio_profile`, `score_eco`, `durete_fonciere`, `parcelles_personnes_morales`, …).
2. `build_parcelle_export_row` (`classement_export_attrs.py`) produit la ligne d’attributs. Les noms font au plus 10 caractères. Les options du filtre (`filtre_options.FiltreOptions`) remplissent les colonnes de réglage : espèces, rayons hydro, modes EBC / ZNIEFF, végétation retenue.

La géométrie vient de `ecocompensation_results.parcelles.geom_2154`.

## Unités foncières personnes morales

Une UF est l’ensemble des parcelles cadastrales d’un même propriétaire personne morale, déjà constitué par le classement UF (`last_results_uf`). L’export ne relance pas ce classement. Il part de cette liste, puis qualifie chaque parcelle comme une parcelle du pool.

Fichiers : `export_uf_layers.py`, `uf_export_metrics.py`, `uf_export_aggregate.py`, `export_uf_classement_shp.py`.

### 1. Quelles parcelles

`iter_uf_summaries` lit `last_results_uf.unites_foncieres`. Pour chaque UF on garde les IDU retenus, le rang, la distance au centre du projet, le score écologique de l’UF, le SIREN et la dénomination.

`load_uf_parcels` relit la géométrie dans `ecocompensation_results.unites_foncieres` (une géométrie par couple UF / IDU), calcule la surface, l’indice de Miller et la distance au centroïde de la zone projet.

### 2. Métriques, les mêmes que les parcelles

`ensure_uf_parcel_metrics` crée un run pool de scope `uf_export` qui contient uniquement ces IDU, puis appelle `profiling_service.compute_metrics_for_run`. Les profilers (occupation du sol, hydro, personnes morales, score, etc.) tournent donc sur les parcelles d’UF comme sur le classement parcelles.

Le run est réutilisé tant que la liste d’IDU et le filtre UF (`last_filter_uf`) ne changent pas. Il est invisible dans les sélecteurs de runs et n’est pas supprimé quand un filtrage purge les anciens runs de recherche.

`attach_filter_fields` complète, quand les colonnes existent sur `ecocompensation_results.parcelles`, la zone humide, l’hydrographie et les distances faune. Une espèce demandée dans le filtre et absente de ces colonnes est recalculée : distance à l’observation la plus proche, sans rayon.

### 3. Deux couches

Chaque parcelle passe dans `build_parcelle_export_row`. On obtient donc exactement les mêmes colonnes que la couche `parcelles`, plus `uf_id`, `uf_rang`, `uf_type` (`PM`).

La couche `unites_foncieres` est la géométrie union des parcelles de l’UF. Les attributs ne sont pas une moyenne brute : `uf_export_aggregate.COLUMNS` fixe une règle par colonne, recopiée dans la table `dictionnaire`.

| Règle | Exemples | Sens |
|---|---|---|
| `sum` | `surf_ha`, `zh_ha`, `zh_m2`, `surf_hyha` | Somme des parcelles |
| `min_dist` | `dist_hyd`, `dist_shy`, `rayon_esp` | Distance la plus petite. `-1` (non calculé) est ignoré s’il existe une vraie distance |
| `any` | `zh_int`, `p_morale`, `pm_prosp` | Oui si au moins une parcelle |
| `max` | `score_dur`, axes de dureté | Parcelle la plus dure |
| `min` | `attr_fonc` | Attractivité la plus faible |
| `wmean` | `score_comp`, `cmp_attr` | Moyenne pondérée par la surface |
| `recalc` | `zh_pct`, `occ_sol`, `occ_json`, `faune_txt` | Recalculé sur l’UF |
| `geom` | `miller` | Recalculé sur le polygone union |
| `uf` | `dist_km`, `score_eco`, `eco_max` | Valeur déjà portée par le classement UF, pas par les parcelles |
| `same` | `ebc`, `znieff`, `troncon`, `zdv` | Réglage du filtre, identique partout |
| `concat` | `txt_scor`, `txt_dure`, … | Détail de chaque parcelle, préfixé par son IDU |

Occupation du sol : les parts de chaque parcelle (`occ_sol_ratios`, CESBIO / végétation) sont pondérées par la surface, puis ramenées à la surface de l’UF.

Faune : pour chaque espèce, on garde la distance minimale parmi les parcelles. `espece_esp` est l’espèce la plus proche. `faune_txt` liste ces minima.

Personne morale : si le classement UF connaît le SIREN, il remplace la liste des SIREN des parcelles. `p_morale` passe à oui.

## Îlots RPG

Les îlots ne passent pas par `build_parcelle_export_row` et n’ont pas le dictionnaire d’agrégation des UF. Ce sont des objets agricoles déjà calculés par le pipeline RPG.

`export_ilots_rpg_layers.build_ilots_geodataframes` lit les îlots du projet (`get_results`), puis deux géométries :

- `ilots_rpg` : polygone stocké dans la table de résultats RPG (`geom_2154`), un îlot = une entité. Attributs du classement : `num_ilot`, `rang`, `surface_ha`, `nb_parcelles`, culture dominante, personne morale dominante, `pm_unique`, distance au centre, score écologique.
- `ilots_parcelles` : chaque IDU de l’îlot, joint au cadastre. Attributs : identifiant cadastral, surface, part de l’îlot (`part_ilot`), propriétaire principal, nombre de personnes morales, `in_pool` (la parcelle est aussi dans le classement parcelles du projet).

Le lien entre les deux couches est `num_ilot`. `uf_type` n’est pas posé ici : un îlot RPG n’est pas une UF personne morale, même si une personne morale peut en être le propriétaire dominant.

## Contexte projet

`project_context_layers.load_project_context_layers` :

- `zone_projet` : géométrie de `ecocompensation.foncier`. À défaut, union des parcelles du projet (`project_parcelles`).
- `aire_etude` : géométrie de `ecocompensation.aoi`, avec le buffer en kilomètres.

## Ligne de commande

Depuis `backend` :

```bash
python -m api.recherche_de_foncier.enrichir.pool.exports.export_etude_cli \
  --project-id <uuid> --out etude.gpkg --layers parcelles,uf,ilots --check
```

`--force-metrics` recalcule le run `uf_export` avant l’écriture. `--check` relit le GeoPackage et signale les colonnes vides.
