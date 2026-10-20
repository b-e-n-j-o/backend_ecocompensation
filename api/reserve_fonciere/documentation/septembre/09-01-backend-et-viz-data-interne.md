# Affichage des couches carto — Web SIG données internes

Page : `/donnees-internes`.  
Catalogue (source de vérité) : `backend/data_interne/catalog.py`.  
Le frontend lit le catalogue via `GET /api/data-interne/layers` : **ajouter une couche = une entrée dans `LAYERS`**, pas une page React dédiée.

---

## 1. Principes

1. **Ne pas tout charger.** Le navigateur ne reçoit jamais 100k+ polygones d’un coup.
2. **2154 en base** (métier, intersections). **Pas de 4326 stocké.** Le GeoJSON MapLibre est du 4326 **à la volée**. Le web (tuiles, bbox) travaille en 3857.
3. **Catalogue, pas affichage forcé.** Couche lourde → `default_visible=False`. L’utilisateur coche ce dont il a besoin.
4. **Le front ne décide pas du mode de livraison.** `delivery` + `min_zoom` + couleurs vivent dans le catalogue.
5. **MapLibre affiche en 3857 ; le GeoJSON / les tuiles vectorielles parlent 4326 / MVT.** Ne pas confondre les deux.

---

## 2. Choisir le mode (`delivery`)

| Situation | `delivery` | Zoom | Exemple déjà en place |
|---|---|---|---|
| Peu d’objets, emprise locale (< ~20k) | `geojson` | tout | Parcelles syndicats (31) |
| Dense, évolutif, on n’affiche que la **vue** | `mvt` (live PostGIS) | plancher obligatoire | Végétation / CESBIO (424k, Gironde) |
| Dense, **quasi statique**, emprise métier figée | `mbtiles` (fichier + Storage) | plancher | Prospects SO (111k, bbox bureau) |

### Comment trancher

- **GeoJSON** si on peut tout envoyer et zoomer/clusterer côté client sans freeze.
- **MVT live** si la table va bouger, ou si le sol est *plein* (chaque tuile est saturée). MapLibre ne demande que les tuiles visibles (+ voisines).
- **MBTiles** si la couche ne change presque jamais **et** qu’on peut la **clipper** à une zone métier (Sud-Ouest, un département). Tippecanoe une fois, fichier dans Storage, cache local du backend.

### Ce qu’il ne faut pas faire

- Dump GeoJSON national / départemental de centaines de milliers d’objets.
- Faire lire un `.mbtiles` (SQLite) **directement** par MapLibre ou par une URL Storage : il faut un backend qui sert `{z}/{x}/{y}.mvt`.
- Un MBTiles « toute la France, tous les zooms, y compris tuiles vides ».
- Afficher une couverture de sol continue à z8–12 : illisible et trop lourd. Plancher **z ≥ 14** (végétation) ou **z ≥ 12** (parcelles éparses).

### Densité (ordre de grandeur mesuré)

- Parcelle éparse : z12 tient (tuile ~ quelques dizaines d’objets).
- Sol plein (~40 polygones / km²) : z12 ≈ 4 000 objets / tuile → **non**. z14 ≈ 200–300 → **oui**.
- MVT live sans `geom_3857` : ~1,4 s / tuile à froid sur la végétation, puis cache RAM ~3 ms. Si le pan est lent → ajouter `geom_3857` + GIST, pas changer de mode tout de suite.

---

## 3. Recette : ajouter une couche

### 3.1 Table PostGIS

- Schéma `ecocompensation`.
- Géométrie **EPSG:2154**, GIST.
- Idéal : colonnes `geom_2154` + `geom_3857` (filtre tuiles). Si une seule colonne (`geom` en 2154), le MVT transforme à la volée (plus lent).
- Un id stable (colonne `id` ou expression `feature_id_sql`).

### 3.2 Une entrée dans `catalog.py`

```python
InternalLayer(
    key="ma_couche",                 # slug URL
    label="Libellé catalogue",
    schema="ecocompensation",
    table="ma_table",
    geometry_type="polygon",         # polygon | line | point
    color="#2e7d32",                 # fallback / légende compacte
    geom_2154="geom_2154",           # ou "geom"
    default_visible=False,           # True seulement si léger
    delivery="geojson",              # geojson | mvt | mbtiles
    min_zoom=14,                     # None = tous zooms (geojson)
    compute_bounds=False,            # True seulement si la table est petite
    bounds_4326=(w, s, e, n),        # recadrer sans ST_Extent
    properties=("col_a", "col_b"),   # attributs identify / tuile
)
```

Champs utiles selon le mode :

| Champ | Rôle |
|---|---|
| `style=LayerStyle(cluster=True, …)` | Halos centroïdes (GeoJSON, zooms larges) |
| `min_zoom` | Le backend renvoie une tuile vide en dessous ; MapLibre ne demande rien |
| `max_zoom` | Zoom max des tuiles MBTiles / source MapLibre (défaut 14 dump, 16 affichage) |
| `where_sql` | Filtre dump / comptage / MVT live (`deleted_at IS NULL AND classe LIKE 'C -%'`) |
| `color_property` + `class_colors` | Remplissage par classe (`libelle_prio`, `source`, etc.) |
| `class_labels` | Libellés légende si les clés de `class_colors` sont des codes (`zhe` → « Zones humides effectives ») |
| `mvt_order_sql` | Ordre de dessin dans la tuile (ex. végétation au-dessus du fond) |
| `clip_bbox` | Filtre dump MBTiles (zone métier) |
| `mbtiles_file` + `storage_bucket` + `storage_object` | Fichier + chemin Storage |
| `feature_id_sql` | Id numérique unique si pas de colonne `id` |

Le frontend n’a **rien à coder** pour une couche standard (toggle, recadrer, popup, zoom plancher, légende de classes).

### 3.3 Selon `delivery`

**GeoJSON** — rien d’autre. Endpoint : `/layers/{key}/geojson`.

**MVT live** — rien d’autre. Endpoint : `/layers/{key}/tiles/{z}/{x}/{y}.mvt` (clip `geom && tuile`).  
Vérifier un zoom : une tuile à froid < ~2 s, sinon `geom_3857` ou monter `min_zoom`.

**MBTiles**

```bash
cd COMPENSATION_ECO/backend
python3 -m data_interne.build_mbtiles ma_couche --upload
# ou, fichier déjà généré :
python3 -m data_interne.storage_mbtiles upload ma_couche
```

- Dump **filtré** par `clip_bbox` (ne pas tippecanoe la France entière « au cas où »).
- Zooms typiques : `-Z12 -z14` (MapLibre survole au-delà).
- Storage : bucket `ecocompensation`, objet `couches/<slug>/<fichier>.mbtiles`.
- Le backend **télécharge une fois** en cache local (`data_interne/mbtiles/`, gitignoré), puis sert les tuiles. Le navigateur ne parle jamais au bucket.

---

## 4. Couches déjà en catalogue (référence)

| Couche | Objets | Mode | Pourquoi |
|---|---|---|---|
| Parcelles syndicats | 31, locale | GeoJSON + clusters jusqu’au z14 | Trop petit pour des tuiles |
| Parcelles prospects | 181k France → **111k clip SO** | MBTiles z12–14 + Storage | Statique, épars, zone bureau |
| Végétation / CESBIO | 424k, Gironde, sol plein | MVT live, z ≥ 14, 1 couche | Empilement volontaire végétation **sur** CESBIO (végétation prioritaire). Pas deux couches catalogue. |
| Zones humides (RPDZH) | 202k, Adour-Garonne, patches denses | MVT live, z ≥ 14 | Deux classes `source` : ZHE au-dessus de l’inventaire total. Pas de `geom_3857` (transform à la volée). |
| GEOMCE surf / lin / pct | ~10k mesures, France métro | MBTiles z12–16 + Storage + halos | Statique (~3 ans). Clip hexagone+Corse. Légende par `classe` (E/R/C/A/Z). Compensation dessinée au-dessus. |

Seuils ressentis (prospects, MVT live **avant** MBTiles) : 1 tuile isolée ~300 ms = correct ; 6 tuiles en parallèle ~1,5 s = **lent** (pool Postgres 4 connexions). MBTiles : ~0,02 ms disque, ~5–10 ms HTTP.

---

## 5. UX carte

- Catalogue à gauche : cocher / masquer / recadrer.
- Couches lourdes **off** par défaut.
- Sous z plancher : bandeau « zoomez jusqu’à z ≥ N ».
- Identify : clic → attributs `properties` (labels dans le front, `PROP_LABELS`).
- Légende de classes : `class_colors` exposé par l’API, affiché une fois la couche cochée.

Ne pas `fitBounds` automatique sur une couche nationale / départementale (ça dézoome sous le plancher). Recadrer = action utilisateur.

---

## 6. Fichiers à connaître

| Fichier | Rôle |
|---|---|
| `catalog.py` | Registre + styles |
| `router.py` | GeoJSON, MVT live, lecture MBTiles |
| `build_mbtiles.py` | Dump filtré + tippecanoe |
| `storage_mbtiles.py` | Upload / pull bucket `ecocompensation` |
| `frontend/.../DonneesInternesPage.tsx` | Carte, catalogue, clusters, MVT |
| `frontend/.../api.ts` | Types catalogue |

Géométries rappel : intersections SQL → 2154 ; tuiles / bbox → 3857 ; GeoJSON MapLibre → 4326 transformé.

---

## 7. Checklist avant de brancher une nouvelle table

- [ ] Combien d’objets ? Emprise (locale / département / France) ? Sol plein ou épars ?
- [ ] La couche bouge souvent ? → MVT live. Figée + zone métier ? → MBTiles clipé.
- [ ] `default_visible=False` si > quelques milliers d’objets ou si nationale.
- [ ] `min_zoom` : 12 (parcelles), 14 (occupation du sol continue).
- [ ] GIST sur la géométrie 2154 ; `geom_3857` si les tuiles live dépassent ~1–2 s.
- [ ] `properties` limitées (tuiles = petit payload).
- [ ] Tester : catalogue, coche, zoom plancher, une tuile, clic identify, masquage (stoppe les requêtes MVT).
