# Mise à jour v0.2 — score urbanistique : notes de fusion

À appliquer sur la version déjà intégrée du module. Les fichiers **nouveaux** se copient tels quels ;
les fichiers **modifiés** sont à fusionner (diff ciblé ci-dessous).

## Nouveaux fichiers
- `urba_profiler/scoring/` (tout le dossier : `__init__.py`, `regles_compensation.yaml`, `loader.py`,
  `engine.py`, `schemas.py`, `doc.py`)
- `tests/test_scoring.py`
- `SCORING.md`, `docs/REGLES_SCORING.md`, `docs/revue_regles_scoring.xlsx`

## Fichiers modifiés
| Fichier | Changement |
|---|---|
| `config.py` | `ProfilerSettings` : + `anneau_zonage_m: float = 200.0`, + `scoring_profil: Optional[str] = "generique"` |
| `schemas.py` | import `ScoreUrba` ; `ProfilOptions` : + `anneau_zonage_m`, + `scoring_profil` ; nouvelle classe `AnneauZonage` ; `Synthese.anneau_zonage` ; `Cible.score` |
| `nomenclature.py` | + `typezone_simple()` (statistiques d'anneau) |
| `service.py` | `_merge_settings` : `scoring_profil: null` explicite désactive le score, et le profil est validé ; + `_anneau_zonage()` ; marge de la bbox WFS = max(voisinage, tolérance, anneau) ; anneau calculé pour les parcelles et les UF ; étape 6 « scoring » à la fin de `run_urba_profile` (une erreur de scoring n'interrompt jamais le profil, elle va dans `meta.errors`) |
| `router.py` | + `POST /urba/score`, + `GET /urba/scoring/regles` |
| `requirements.txt` | + `pyyaml`, + `openpyxl` (optionnel) |

## Points d'attention
- **Fichiers de données** : `scoring/regles_compensation.yaml` et `cnig_codes.json` doivent être
  embarqués au déploiement (package data si vous packagez le module).
- **Bbox WFS** : l'anneau de 200 m agrandit un peu la bbox des requêtes (marge de 210 m au lieu
  de 10 m). C'est négligeable avec des clusters de 2 km. Mettre `anneau_zonage_m: 0` le désactive
  (le malus de pression urbaine disparaît alors).
- **Persistance** : si vous stockez les résultats, gardez `score.version_regles` et le profil. On
  pourra tout recalculer via `/urba/score` quand l'urbaniste aura modifié les règles.
