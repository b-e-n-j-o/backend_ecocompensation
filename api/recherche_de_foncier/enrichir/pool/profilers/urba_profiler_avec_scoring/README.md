# urba_profiler — onglet « Urba » de l'enrichissement KerFoncier

Croise des parcelles (et unités foncières) avec les flux WFS du Géoportail de
l'Urbanisme, **à la volée** (rien n'est écrit en base), et renvoie, couche par
couche, les éléments intersectés avec leurs mesures, le libellé officiel CNIG
du code saisi, les liens vers les documents et des drapeaux qualité.

Fonctionne comme le pipeline dureté foncière : lancé à la demande depuis le
front sur une sélection de parcelles du pool (ou tout le pool).

---

## 1. Qu'est-ce qui est quoi

```
urba_profiler/
├── config.py         Catalogue des couches WFS + paramètres (seuils, tolérances, réseau)
├── cnig_codes.json   Libellés officiels CNIG PLU v2024-01 (165 codes prescriptions, 45 informations, typezone, typesect)
├── nomenclature.py   Lookup code -> libellé CNIG + flags qualité ; parse de l'idurba
├── geometry.py       GeoJSON <-> shapely, reprojection vers Lambert-93 (toutes les mesures en m / m²)
├── wfs_client.py     Requêtes WFS data.geopf.fr (subdivision > 5000, dédup, retries, cache mémoire/disque)
├── intersect.py      LOGIQUE MÉTIER : mesure d'une entité vs la cible + agrégation en éléments
├── service.py        Orchestration : clusters de parcelles -> fetch parallèle -> profils parcelles puis UF
├── schemas.py        Contrats pydantic entrée / sortie (ce que consomme le front)
├── router.py         Endpoints FastAPI (profil, recalcul du score, référentiel de règles)
├── smoke.py          Test réel en CLI sur une parcelle
└── scoring/          SCORE URBANISTIQUE pour la compensation (voir SCORING.md)
    ├── regles_compensation.yaml   toute la logique métier, éditable par un urbaniste
    ├── loader.py / engine.py      chargement validé / moteur sans valeur métier en dur
    ├── schemas.py                 sortie ScoreUrba
    └── doc.py                     génère docs/REGLES_SCORING.md + grille Excel de relecture
docs/                 REGLES_SCORING.md, revue_regles_scoring.xlsx (générés)
SCORING.md            note de méthode du score (pour l'urbaniste)
tests/                63 tests hors ligne (profiler + scoring)
```

Vocabulaire :

| Terme | Sens |
|---|---|
| **cible** | ce qu'on analyse : une `parcelle` ou une `unite_fonciere` (union de parcelles) |
| **couche** | un flux WFS GPU (`wfs_du:zone_urba`…), identifié côté Kerelia par une `layer_key` |
| **entité** | un objet GPU brut (un polygone de zone, une prescription…), identifié par son `gml_id` |
| **élément** | une ligne du rapport : une ou plusieurs entités **fusionnées** car identiques (même valeurs des champs `group_by`). Ex : la zone `Nzh` découpée en 2 polygones → 1 élément, `nb_entites=2` |
| **cluster** | groupe de parcelles proches (grille de 2 km) qui partagent les mêmes requêtes WFS |
| **écarté** | entité qui touche la cible mais n'est pas retenue (contact de limite, sous le seuil). Toujours visible dans `ecartes` avec `raison_ecart` |
| **voisinage** | entité à moins de `voisinage_m` de la cible sans l'intersecter (désactivé par défaut) |

## 2. Couches interrogées (`config.LAYERS`)

| layer_key | typeName WFS | famille | géométrie | par défaut |
|---|---|---|---|---|
| `zonage_plu` | `wfs_du:zone_urba` | zonage | surf | oui |
| `secteur_cc` | `wfs_du:secteur_cc` | zonage (cartes communales) | surf | oui |
| `prescriptions_surf` / `_lin` / `_pct` | `wfs_du:prescription_*` | prescription | surf/lin/pct | oui |
| `infos_surf` / `_lin` / `_pct` | `wfs_du:info_*` | information | surf/lin/pct | oui |
| `sup_assiette_s` / `_l` / `_p` | `wfs_sup:assiette_sup_*` | servitude | surf/lin/pct | oui |
| `sup_generateur_s` / `_l` / `_p` | `wfs_sup:generateur_sup_*` | servitude | surf/lin/pct | non |

Les **assiettes** SUP sont l'emprise d'effet de la servitude (ce qui s'applique à la parcelle).
Les **générateurs** sont l'objet à l'origine (le monument, la ligne HT) : désactivés par défaut.

Ajouter une couche = ajouter un `LayerDef` dans `config.py` (typename, champs à garder,
`group_by`). Aucun autre fichier à toucher.

## 3. Règles métier (intersect.py)

Toutes les mesures sont faites en Lambert-93 (EPSG:2154).

**Surfacique** (zonage, prescriptions/infos surf, assiettes S)
1. L'entité intersecte la cible ?
2. **Contact de limite** : on rétrécit la cible de `edge_tolerance_m` (0,5 m). Si l'entité
   ne touche pas cette cible rétrécie → écartée `contact_limite` (décalage cadastre / PLU,
   l'ancien problème des zonages voisins qui « mordent » de quelques cm).
3. **Seuil** : écartée `sous_seuil_…` si `pct < min_pct_surface` (0,5 %) **et**
   `surface < min_area_keep_m2` (200 m²). Le second critère protège les grandes UF
   (0,3 % de 10 ha = 300 m², on garde).
4. Sinon retenue : `surface_m2`, `pct_cible` (% de la surface de la cible).

**Linéaire** (prescriptions/infos lin, assiettes L)
- `longueur_m` = longueur à l'intérieur de la cible.
- `position="interieur"` si la ligne entre dans la cible rétrécie, `"limite"` si elle longe
  la bordure ou passe à moins de 0,5 m (ex. haie protégée en limite de parcelle).
  Ces lignes sont **retenues** (`keep_linear_on_boundary=True`), car pertinentes.

**Ponctuel** : `nb_points` dans la cible, `position` interieur / limite (même tolérance).

**Agrégation** : les entités sont fusionnées par `group_by` (ex. zonage : `idurba + libelle + typezone`,
prescriptions : `idurba + typepsc + stypepsc + libelle + txt`, SUP : `suptype + idass + nomass`),
les mesures sont sommées, les éléments triés par % puis longueur puis nombre de points.

**Unité foncière** : la cible est l'union des parcelles ; mêmes règles. Le détail parcelle par
parcelle est dans `parcelles[<id>]` (chaque parcelle de l'UF y est profilée individuellement).

## 4. Qualité des données GPU : ce que fait (et ne fait pas) le module

Le code saisi par la collectivité n'est **pas** considéré comme fiable. On affiche côte à côte :
- `attributs.libelle` = ce que la collectivité a saisi,
- `code` + `libelle_cnig` = ce que dit le code selon le standard CNIG v2024-01,

et on lève des `flags` par élément :

| flag | sens |
|---|---|
| `code_absent` | type vide |
| `code_hors_nomenclature` | code inexistant dans le standard (ex. `77`) |
| `sous_type_hors_nomenclature` | sous-type inconnu, on retombe sur le libellé générique `TT-00` |
| `code_generique_99` | « Autre » : seul le libellé saisi renseigne |
| `libelle_vide` | pas de libellé saisi |
| `idurba_absent` | entité non rattachée à un document |

Flags par cible : `aucun_zonage_gpu` (RNU, document non versé ou parcelle hors document),
`zonage_multiple`, `couches_en_erreur`, `uf_non_contigue`, `uf_incomplete`.

Le module **ne corrige pas** les incohérences entre code et libellé (cas des EBC mal codés).
C'est l'étape suivante (normalisation / LLM), qui consommera cette sortie telle quelle.

## 5. Contrat d'API

### Entrée — `UrbaProfilRequest`

```json
{
  "parcelles": [
    {"id": "33234000AB0123", "geometry": {"type": "Polygon", "coordinates": [...]}, "srid": 2154,
     "meta": {"commune": "Latresne"}}
  ],
  "unites_foncieres": [{"id": "UF_12", "parcelle_ids": ["33234000AB0123", "33234000AB0124"]}],
  "options": {"layers": null, "voisinage_m": 0, "include_geometries": false}
}
```

- `srid` : 2154 (recommandé, comme en base) ou 4326.
- `meta` : renvoyé tel quel (pratique pour réafficher commune / section / numéro).
- `options` (toutes facultatives) : `layers`, `voisinage_m`, `min_pct_surface`, `min_area_keep_m2`,
  `edge_tolerance_m`, `include_ecartes`, `include_raw` (propriétés WFS brutes),
  `include_geometries` (géométrie intersectée en GeoJSON 4326 par élément, pour la carte).

### Sortie — `UrbaProfilResponse`

```
{
  "parcelles":        { "<id>": Cible },
  "unites_foncieres": { "<id>": Cible },
  "meta": RunMeta   // durée, nb requêtes WFS, cache hits, erreurs, paramètres utilisés
}

Cible = {
  id, type: "parcelle"|"unite_fonciere", surface_m2, parcelle_ids (UF), meta,
  documents: [ {idurba, partition, typedoc, date, datappro, insee, urls_reglement[]} ],
  synthese:  { zonages: [{libelle, type, pct_cible, surface_m2}], nb_elements_par_famille,
               couches_intersectees[], couches_en_erreur[] },
  flags: [],
  couches: [ Couche ]          // une par couche demandée, toujours présente, même vide
}

Couche = {
  layer_key, display_name, famille, geom_kind, typename,
  status: "intersected" | "not_intersected" | "error",
  elements_count, elements: [Element], ecartes: [Element], voisinage: [Element],
  colonnes: { champ: "Libellé d'affichage" },   // en-têtes de tableau, dans l'ordre
  error
}

Element = {
  element_id ("zonage_plu:1"), source_ids (gml_id GPU), nb_entites,
  attributs: { champ: valeur },   // clés = celles de `colonnes`
  code ("05-01" | "AUs"), libelle_cnig,
  mesure: { position, surface_m2, pct_cible, longueur_m, nb_points, distance_m },
  liens: [ {type: "reglement"|"acte_sup", url, nom} ],
  flags: [], raison_ecart (dans ecartes), raw?, geometry?
}
```

Un exemple complet de cible est dans `example_output.json`.

**Important pour le front** : `status="error"` ne veut pas dire « rien » mais « couche
indisponible » (timeout GPU…). À afficher distinctement de `not_intersected`.

## 6. Brancher sur notre système (consignes pour l'assistant de code)

1. Copier le dossier `urba_profiler/` dans le backend FastAPI (à côté du module dureté foncière).
   Dépendances : `requests shapely>=2 pyproj pydantic>=2 numpy pyyaml` (+ `fastapi`, déjà présent ; `openpyxl` pour la grille Excel).
2. Endpoint : soit `app.include_router(urba_router, prefix=...)` depuis `router.py`, soit,
   pour coller au pattern dureté foncière, un endpoint maison qui :
   - reçoit les **ids** des parcelles sélectionnées (ou l'id du run de pool),
   - charge leurs géométries en **2154** depuis la table du pool (`ST_AsGeoJSON(geom)`),
   - construit `UrbaProfilRequest` (+ UF si le pool les connaît) et appelle `run_urba_profile`,
   - renvoie le résultat (et, si on veut le garder, le stocke en `jsonb` par parcelle et par run,
     comme les autres profileurs).
3. Le traitement est **bloquant** (réseau + géométrie) : endpoint en `def` (pas `async def`),
   ou tâche de fond si on lance tout le pool. Ordre de grandeur attendu : 11 couches × nombre
   de clusters de 2 km requêtes ; un pool de 200 parcelles réparties sur 20 km représente
   quelques dizaines de clusters, donc quelques centaines de requêtes, à 4 en parallèle.
4. Onglet « Urba » : boucler sur `couches` ; en-têtes = `colonnes` ; lignes = `elements` ;
   mesure affichée selon `geom_kind` (`pct_cible` / `longueur_m` / `nb_points`) ;
   badge `position` ; `libelle_cnig` en sous-titre du libellé saisi ; `flags` en pastilles ;
   `ecartes` et `voisinage` repliés. Bloc en tête : `synthese.zonages` + `documents`.

## 7. Paramètres réseau et cache

- `cluster_cell_m` (2000) : taille de la grille de regroupement des parcelles.
- `max_workers` (4) : requêtes WFS parallèles.
- `fetch_srs` (`EPSG:4326`) : comme nos scripts ETL. Un garde-fou détecte et corrige un ordre
  des axes inversé. `EPSG:2154` est possible si le flux le supporte (à valider).
- Cache : en mémoire dans le process (TTL `URBA_WFS_CACHE_TTL`, 3600 s par défaut). En dev,
  `URBA_WFS_CACHE_DIR=.cache_wfs` ajoute un cache disque : relancer sur les mêmes parcelles
  ne refait aucune requête.

## 8. À valider en réel (je n'ai pas pu joindre data.geopf.fr depuis mon environnement)

```bash
python -m urba_profiler.smoke --insee 33234 --section AB --numero 0123 --voisinage 50 \
       --cache-dir .cache_wfs --json out.json
```

Points à vérifier :
- **Noms des champs** : repris de nos scripts ETL. Si une colonne reste toujours `null`
  (ex. `nature`, `partition`), regarder les propriétés réelles avec `include_raw: true` et
  corriger `fields` dans `config.py`.
- `wfs_du:secteur_cc` : typeName supposé par analogie avec `zone_urba`. À retirer de
  `LAYERS` s'il renvoie une erreur.
- `pct_cible` des zonages ≈ 100 % au total sur une parcelle entièrement couverte.

## 9. Score urbanistique (compensation)

Calculé automatiquement à la fin de chaque profil (`options.scoring_profil`, défaut `generique` ;
`null` pour le désactiver). Profils : `generique`, `zones_humides`, `boisement`, `milieux_ouverts`.
Méthode complète et hypothèses à valider : **`SCORING.md`**. Règles : **`docs/REGLES_SCORING.md`**.

Ajouts à la sortie :
- `Cible.score` (`ScoreUrba`) : `score_global` (0-100, plafonné par les vetos), `classe` (A à E),
  `sous_scores` (pérennité, compatibilité, synergie, maîtrise), `confiance` (valeur, niveau, motifs),
  `vetos`, `contributions` (règle, points, éléments, fiabilité, justification), `points_a_verifier`,
  `interpretations` (comment chaque élément GPU a été lu : règle, fiabilité, conflit code / libellé),
  `resume` (3 à 6 lignes prêtes à afficher).
- `Cible.synthese.anneau_zonage` : % U/AU/A/N dans un anneau de `anneau_zonage_m` (200 m par défaut).

Endpoints :
- `POST /urba/score` `{cibles: [Cible...], profil}` : recalcul sans WFS (autre profil, règles modifiées).
  Les résultats stockés portent `version_regles`.
- `GET /urba/scoring/regles` : référentiel (pour des infobulles côté front).

Front, suggestion d'affichage dans l'onglet Urba : carte de score (score + classe + badge de
confiance), 4 jauges de sous-scores, vetos en bandeau rouge ou orange, `resume`, puis les
contributions triées (les éléments cités renvoient aux lignes du tableau des couches) et la liste
des points à vérifier. Les éléments en conflit (`interpretations[].conflit`) méritent un badge
« donnée GPU incohérente ».

Modifier les règles : éditer le YAML, incrémenter `version`, puis
`python -m urba_profiler.scoring.doc --out-dir docs` et `pytest -q tests/`.

## 10. Tests

```bash
pip install pytest httpx openpyxl
pytest -q tests/
```
