# Réserve foncière — ajouter un site

Table unique : `ecocompensation.reserve_fonciere` (noyau commun + `attributs_source` JSONB).  
Une table par apporteur = dette (UNION, plusieurs GIST, légendes éclatées). **Ne jamais créer une nouvelle table pour un nouveau foncier.**

Catalogue Web SIG : `catalog.py` (famille **Réserves foncières**).  
Scripts d’ingestion : `DATA_ECOCOMPENSATION/DATA_INTERNE/RESERVE_FONCIERE/`.

---

## 1. Ce qui est déjà en place

| `source_type` | Légende catalogue | Couleur | Exemple |
|---|---|---|---|
| `PARCELLE_SYNDICALE` | Parcelles syndicats | bleu `#2563eb` | shapefile syndicats |
| `PROPRIETAIRE_PRIVE` | Foncier privé | ambre `#b45309` | Margariti, Saint-Aubin-de-Médoc |

Le bureau d’études distingue les types **dans le catalogue** (deux lignes, deux pastilles), pas par sous-classes d’une seule couche.

**Même `source_type` → rien à changer au front.** Un nouveau privé apparaît sous **Foncier privé**, avec le compteur à jour.

**Nouveau type** (`COMMUNE`, `ASA`, …) → une entrée `InternalLayer` dans `catalog.py` (`label`, `color`, `where_sql`).

---

## 2. Recette d’un nouveau site (cas courant : privé)

### 2.1 QGIS — shapefile cadastral

1. Extraire les parcelles PCI du site (pas les morceaux voisins / subdivisions hors cible).
2. Vérifier le **nombre d’entités** avant export (ex. 4 parcelles, pas 30).
3. Attributs minimum : `idu`, `nom_com`, `code_insee`, `contenance`, `section`, `numero`.
4. L’**IDU du cadastre fait foi** (14 caractères). Ne pas reconstruire un INSEE « de tête » (Saint-Aubin-de-Médoc = `33376`, pas `33390`).
5. CRS indifférent (souvent 3857 depuis QGIS) : l’ingestion reprojette en **EPSG:2154**, puis calcule `geom_3857`.

### 2.2 Dossier

Un dossier **par site**, pas par type :

```
DATA_ECOCOMPENSATION/DATA_INTERNE/RESERVE_FONCIERE/
  parcelles_syndicates_ecocompensation/
  baudoin_margariti/
  nom_proprietaire_commune/     ← nouveau
    le_site.shp (+ .dbf .shx .prj)
    ingestion.py
```

### 2.3 Script

Copier `baudoin_margariti/ingestion.py`. Ne modifier que le **bloc site** en tête de fichier :

| Constante | Rôle |
|---|---|
| `SHP_FILE` | Nom du shapefile dans le même dossier |
| `SOURCE_TYPE` | En général `PROPRIETAIRE_PRIVE` |
| `SITE_CODE` | Identifiant stable ASCII, ex. `dupont_lacanau` |
| `NOM_SITE` | Intitulé lisible |
| `CONTACT_NOM` / `CONTACT_EMAIL` | Propriétaire / interlocuteur |
| `DESCRIPTION_SITE` | Texte opportunité (noyau) |
| `SITE_JSONB` | Enjeux, montages, origine de la demande |

`SITE_CODE` est la clé de **re-run** : `DELETE WHERE attributs_source->>'site_code' = …` puis insert. Ça n’efface ni les syndicats ni les autres privés.

```bash
cd DATA_ECOCOMPENSATION/DATA_INTERNE/RESERVE_FONCIERE/nom_proprietaire_commune
python ingestion.py
```

Connexion : `.env` à côté du script, sinon `COMPENSATION_ECO/backend/.env` (`SUPABASE_HOST`, `SUPABASE_PASSWORD`, …).

---

## 3. Noyau vs JSONB

**Noyau** (colonnes SQL, filtres / carte / identify) :

- `idu`, `nomcommune`, `codeinsee`
- `surface_cadastrale` (contenance), `surface_sig` (`ST_Area` 2154)
- `source_type`, `statut_prospection` (`A_ETUDIER` par défaut)
- `contact_nom`, `contact_email`, `description_site`
- `geom_2154`, `geom_3857`

**JSONB `attributs_source`** (ne pas élargir le schéma) :

```json
{
  "site_code": "margariti_st_aubin",
  "nom_site": "Foncier Margariti - Saint-Aubin-de-Médoc",
  "origine_demande": "…",
  "etat_ecologique": ["Friche", "Ancienne pinède coupée"],
  "potentiels_retenus": ["ERC", "Zones humides", "Reboisement"],
  "montages_envisages": ["Cession", "Conventionnement long terme"],
  "cadastre": { "section": "0A", "numero": "0281", "contenance": 64080 }
}
```

Tout attribut exotique (FAUNA, zonage, mail brut) va dans ce JSONB. Pas d’`ALTER TABLE`.

---

## 4. Catalogue (`catalog.py`) — seulement si nouveau type

Le front ne code **rien** : `GET /api/data-interne/layers` lit `LAYERS`.

```python
InternalLayer(
    key="foncier_commune",
    label="Foncier communal",
    schema="ecocompensation",
    table="reserve_fonciere",
    geometry_type="polygon",
    color="#0f766e",
    family=FAM_FONCIER,
    default_visible=True,
    where_sql="t.source_type = 'COMMUNE'",
    style=LayerStyle(cluster=True, cluster_max_zoom=13, cluster_radius=70, geom_min_zoom=14),
    properties=(
        "idu", "nomcommune", "codeinsee",
        "surface_cadastrale", "surface_sig",
        "source_type", "statut_prospection",
        "contact_nom", "contact_email", "description_site",
    ),
)
```

Livraison : **GeoJSON** tant que la réserve reste locale (quelques dizaines / centaines d’objets). Passer MVT / MBTiles seulement si ça explose — voir `README.md` (affichage).

Libellés identify manquants → `PROP_LABELS` / `PROP_VALUE_LABELS` dans `DonneesInternesPage.tsx`.

---

## 5. Interdits

- `DROP TABLE reserve_fonciere` (détruit toutes les sources).
- `DELETE` sur tout un `source_type` s’il y a **plusieurs sites** (privés) : filtrer sur `site_code`.
- Nouvelle table `parcelles_*` par propriétaire.
- Forcer un INSEE / un IDU qui contredit le shapefile cadastral.

---

## 6. Checklist

- [ ] SHP : bon nombre d’entités, `idu` 14 car., commune / INSEE cohérents.
- [ ] Dossier `RESERVE_FONCIERE/<site>/` + script copié de Margariti, constantes renseignées.
- [ ] `SITE_CODE` unique, jamais réutilisé pour un autre site.
- [ ] `python ingestion.py` : N lignes = N entités SHP, SRID 2154 + 3857 OK.
- [ ] Catalogue : inchangé si `PROPRIETAIRE_PRIVE` / `PARCELLE_SYNDICALE` ; sinon nouvelle `InternalLayer`.
- [ ] Page `/donnees-internes` : pastille, compteur, clic identify (contact, description, source).
