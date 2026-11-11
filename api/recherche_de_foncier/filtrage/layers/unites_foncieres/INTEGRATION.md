# Intégration du module `unites_foncieres/`

Emplacement : `api/recherche_de_foncier/filtrage/layers/unites_foncieres/`
(à côté de `rpg/`). Les tables `uf_parcelles` / `uf_terrains` sont créées au premier lancement.

## 1. `filtrage/layers/filter_pipeline.py` — rendre les briques réutilisables

Le comportement du filtre parcelles ne change pas : on renomme et on extrait.

**a) Renommer** (définition + les 2 appels dans `_spatial_filter`) :
- `_build_filter_clauses` → `build_filter_clauses`
- `_filter_params` → `filter_params`

**b) Extraire la clause flore** — ajouter au-dessus de `build_filter_clauses` :

```python
def flora_exists_sql(geom_col: str, i: int) -> str:
    return f"""
        EXISTS (
            SELECT 1
            FROM ecocompensation.flore_observation o
            WHERE o.taxon_key = :flora_key_{i}
              AND ST_DWithin({geom_col}, o.geom, :flora_dist_{i})
        )
    """
```

et dans `build_filter_clauses`, remplacer la boucle `flora_parts` par :

```python
        flora_parts = [flora_exists_sql("p.geom_2154", i) for i, _fc in enumerate(config.flora_criteria)]
```

**c) Extraire le choix du mode spatial** — ajouter après `_has_source_geometry` :

```python
class NoSourceGeometry(RuntimeError):
    """search_mode=within_foncier mais le projet n'a pas de géométrie source."""


def resolve_spatial_mode(engine, project_id: str, config: FilterConfig,
                         log: Callable[[str], None]) -> str | None:
    has_source = _has_source_geometry(engine, project_id)
    if config.search_mode == "within_foncier":
        if not has_source:
            log("WITHIN_FONCIER:error_no_geom")
            raise NoSourceGeometry(project_id)
        log("WITHIN_FONCIER:enabled")
        return "within_foncier"
    if has_source:
        log("SOURCE_EXCLUSION:enabled")
        return "exclude_source"
    log("SOURCE_EXCLUSION:skipped_no_geom")
    return None
```

et au début de `_spatial_filter`, remplacer tout le bloc `has_source = … spatial_mode = None / log(...)` par :

```python
    try:
        spatial_mode = resolve_spatial_mode(engine, project_id, config, log)
    except NoSourceGeometry:
        return [], []
```

## 2. `filter_orchestrator.py` — UF puis RPG, l'un après l'autre

**Imports** — supprimer :
```python
from orchestrator import UF_PHASE_KEYS, run_orchestration
from api.recherche_de_foncier.filtrage.layers.uf_profiling import build_uf_pool_with_profiling
```
ajouter :
```python
from api.recherche_de_foncier.filtrage.layers.unites_foncieres.runner import run_uf_after_filter
```

**Phases** :
```python
UF_FILTER_PHASES = [
    {"key": "unites_foncieres", "label": "Unités foncières (personnes morales)"},
]
```

**Supprimer** `_run_uf_after_filter` et `_get_aoi_centre` (utilisée seulement par elle).

**Ajouter** :
```python
async def _run_post_filter_phases(engine, project_id, aoi_id, config, push) -> None:
    """Après le pool parcelles : UF puis îlots RPG, séquentiellement, en arrière-plan."""
    if config.include_unites_foncieres:
        await run_uf_after_filter(engine, project_id, aoi_id, config, push)
    if getattr(config, "include_ilots_rpg", False):
        await run_ilots_rpg_after_filter(engine, project_id, aoi_id, config, push)
```

**Remplacer** à la fin de `run_filter_orchestration` les deux `asyncio.create_task(...)` par :
```python
        asyncio.create_task(_run_post_filter_phases(engine, project_id, aoi_id, config, push))
```

Si l'engine PPM est distinct en prod (voir question Cursor), le passer :
`run_uf_after_filter(..., engine_ppm=get_engine_ppm())`.

## 3. Ancien chemin UF à retirer

| Où | Quoi |
|---|---|
| `layers/layer_runner.py` | entrées `sous_ensembles` et `enrich_uf` du `LAYER_REGISTRY` (+ docstring l. 11-12) |
| `orchestrator.py` | paramètres `uf_max_parcelles`, `uf_min_area_ha`, `uf_miller_thresh`, `uf_cesbio_libelles`, `uf_fauna_*` de `run_orchestration` ; branches `if key == "sous_ensembles"` / `elif key == "enrich_uf"` ; nettoyage dry-run `unites_foncieres` ; `UF_PHASE_KEYS` ; phase UF de `fetch_project_two_phases` |
| `couches_recherche.json` l. 66 | couche `sous_ensembles` |
| `COUCHES_RECHERCHE.md`, `FILTRAGE.md` | lignes UF à réécrire |
| fichiers supprimés | `layers/common/aoi_to_sub_uf.py`, `layers/enrich_uf.py`, `layers/uf_profiling.py`, `layers/uf_parcelles.py`, `enrichir/pool/ajout_uf.py` |

Remplacements d'imports :
- `ajout_uf.add_manual_uf / delete_uf / AjoutUfError` → `unites_foncieres.manuel` (mêmes signatures ; le retour contient en plus `verdict`, `motif_rejet`)
- `uf_parcelles.build_uf_parcelles_geojson(engine, pid, results)` → `unites_foncieres.pool.build_geojson(engine, pid, results)`
- `enrich_uf.attach_stored_veg_profiles` → plus nécessaire (le profil est déjà dans chaque terrain du JSON)

## 4. Données

Reprise des UF manuelles existantes (une fois, avant de supprimer les anciennes tables ;
elles sont réévaluées au prochain lancement) :

```sql
INSERT INTO ecocompensation_results.uf_parcelles
    (project_id, source, uf_id, idu, siren, denomination, surface_m2, geom_2154)
SELECT DISTINCT ON (project_id, uf_id, idu)
       project_id, 'manuel', uf_id, idu, 'manuel', 'Ajout manuel', ST_Area(geom_2154), geom_2154
FROM ecocompensation_results.unites_foncieres
WHERE siren = 'manuel' AND geom_2154 IS NOT NULL;
```

Puis, une fois le front migré :

```sql
DROP TABLE IF EXISTS ecocompensation_results.sous_ensembles;
DROP TABLE IF EXISTS ecocompensation_results.unites_foncieres;
```

## 5. Front / exports — format `last_results_uf` v2

Une ligne par terrain (voir docstring de `pool.py`). Disparaissent : `sous_ensembles`, `k`,
`nb_combinaisons`, `presence`, `noyau`, `best_subset_id`. Apparaissent : `verdict`,
`motif_rejet`, `criteres` (détail par critère avec les IDU porteurs/fautifs),
`parcelles[].criteres_portes`, `diagnostic` (entonnoir : PM dans l'AOI, parcelles retirées
par critère, terrains évalués / retenus / rejetés par motif).
Événements WS : `layer_key = "unites_foncieres"`, fin = `phase:uf_ready` (inchangé).
