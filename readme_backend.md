# KERELIA Ecocompensation — Backend

## Structure

```
backend/
├── main.py                    # FastAPI app (points d'entrée API + WebSocket)
├── orchestrator.py            # Enchaîne tous les fetches de couches (async)
├── vrai_filtre.py             # ← TON SCRIPT (copier ici, adapter les imports)
├── vrai_filtre_puis_scoring.py # ← TON SCRIPT (copier ici)
├── export_classement_shp.py   # ← TON SCRIPT (copier ici si besoin)
├── carroyage_utils.py         # ← TON MODULE (copier ici)
├── layers/
│   ├── __init__.py
│   └── layer_runner.py        # Wrappeurs importables pour chaque couche
├── requirements.txt
└── .env                       # Variables de connexion (ne pas committer)
```

## Variables d'environnement (.env)

```
SUPABASE_HOST=xxxxx.supabase.com
SUPABASE_PORT=6543
SUPABASE_DB=postgres
SUPABASE_USER=postgres.xxxxx
SUPABASE_PASSWORD=xxxxxxxx
```

## Lancement local

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

## Adaptations nécessaires avant de lancer

### 1. Copier tes scripts existants dans backend/

```bash
cp vrai_filtre.py backend/
cp vrai_filtre_puis_scoring.py backend/
cp carroyage_utils.py backend/
```

### 2. Adapter les imports dans vrai_filtre.py

Remplacer :
```python
from main import get_engine
```
Par :
```python
from db import get_engine
```

Note : `get_engine()` est maintenant dans le module `db.py` pour éviter les imports circulaires.

La fonction `run()` de `vrai_filtre.py` reçoit déjà `engine` en paramètre,
donc il suffit de supprimer le bloc `if __name__ == "__main__":` ou de le
laisser tel quel (il ne sera pas exécuté à l'import).

### 3. Vérifier que `vrai_filtre.py` expose bien

```python
TARGET_COUNT   = 50       # ← modifiable par l'API
RADIUS_START_KM = 10.0   # ← modifiable par l'API
RADIUS_MIN_KM  = 1.0     # ← modifiable par l'API

@dataclass
class FiltreOptions:
    zdv_natures: list[str]
    troncon_hydro_mode: HydroMode
    troncon_hydro_radius_m: float
    surface_hydro_mode: HydroMode
    surface_hydro_radius_m: float

def run(engine, aoi_id, cx, cy, options, *, return_parcelles=False): ...
```

## API Reference

| Méthode | Endpoint | Description |
|---------|----------|-------------|
| GET | `/api/projects` | Liste des projets |
| POST | `/api/projects` | Créer un projet (form: name, buffer_m, gpkg_file OU code_insee) |
| GET | `/api/projects/{id}` | Détail d'un projet |
| DELETE | `/api/projects/{id}` | Supprimer projet + données AOI |
| POST | `/api/projects/{id}/fetch` | Lancer l'orchestration des couches |
| POST | `/api/projects/{id}/filter` | Appliquer filtre + scoring |
| GET | `/api/projects/{id}/results` | Derniers résultats |
| WS | `/ws/projects/{id}/fetch-progress` | Suivi temps réel |
| GET | `/api/layers` | Liste des couches disponibles |

## Événements WebSocket (fetch-progress)

```json
{ "event": "connected",  "status": "fetching", "layers_status": {} }
{ "event": "start",      "total_layers": 9,    "message": "Démarrage..." }
{ "event": "running",    "layer_key": "ebc",   "message": "[3/9] EBC en cours…" }
{ "event": "progress",   "layer_key": "ebc",   "message": "Fetch WFS page 2…" }
{ "event": "done",       "layer_key": "ebc",   "n_inserted": 42, "duration_s": 8.3 }
{ "event": "skipped",    "layer_key": "zone_humide", "n_inserted": 0 }
{ "event": "error",      "layer_key": "patrimoine_naturel", "message": "HTTP 504" }
{ "event": "complete",   "n_ok": 8, "n_skip": 0, "n_err": 1, "total_s": 187 }
```

## Déploiement Render

1. Connecter le repo GitHub à Render
2. Créer un **Web Service** avec `render.yaml`
3. Ajouter les variables d'environnement dans le dashboard Render
4. Utiliser le tier **Starter ($7/mois)** pour éviter le spin-down
   (les fetches WFS durent plusieurs minutes)