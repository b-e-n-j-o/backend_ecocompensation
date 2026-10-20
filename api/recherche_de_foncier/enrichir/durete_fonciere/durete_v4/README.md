# durete_v4 — Dureté foncière Kerelia

Analyse d'une personne morale propriétaire à partir de son SIREN : score /100 déterministe et traçable,
indice de confiance, recommandations de maîtrise foncière, recherche web sourcée (option), synthèse courte.
Choix et audit de la V3 : **[DESIGN.md](DESIGN.md)**.

## Installation
```bash
pip install -r durete_v4/requirements.txt
export GEMINI_API_KEY=...            # ou MISTRAL_API_KEY + DURETE_LLM_PROVIDER=mistral
export SUPABASE_URL_PPM=... SUPABASE_KEY_PPM=...   # fichier des PM (sinon passer les IDU)
export DURETE_CACHE_DIR=~/.cache/durete            # optionnel, recommandé en batch
```

## CLI
```bash
python -m durete_v4 analyse --siren 952824076 --idus 33213000BP0018        # rapport lisible
python -m durete_v4 analyse --siren 952824076 --web --json                 # + web, IDU depuis le fichier PM
python -m durete_v4 analyse --siren 952824076 --no-llm                     # zéro appel LLM
python -m durete_v4 batch --input pool_parcelles.json --output res.jsonl --workers 2 --resume
python -m durete_v4 resume res.jsonl
```

## Backend (drop-in V3)
```python
from durete_v4.service import run_durete_for_siren
res = run_durete_for_siren("952824076", ["33213000BP0018"], avec_recherche_web=True, llm_provider="mistral")
res["score_final"], res["indice_confiance"], res["score_fiable"], res["recommandations"]
```

Brancher vos propres modules (urba GPU, table PM en Postgres local) :
```python
from durete_v4 import analyser_pm, Sources
from durete_v4.sources.parcelles import SqlProvider
res = analyser_pm(siren, sources=Sources(parcelles=SqlProvider(conn, schema="ecocompensation"),
                                         fetch_zonage=mon_module_urba))
```

## Tests (hors-ligne)
```bash
python -m pytest durete_v4/tests -q
```
