# Fiche « Paramètres de filtrage » (PDF une page)

## Fichiers nécessaires (et suffisants)
```
filter_report/
├── __init__.py
├── fiche.py                         # point d'entrée : render_fiche_pdf_bytes(data) + CLI
├── builder.py                       # construit le contexte (critères, entonnoir) à partir des données
├── catalog.py                       # traduit filter_config en critères lisibles (faune, flore, …)
├── templates/fiche_parametres.html.j2
└── examples/example_reel.json       # entrée d'exemple
```
Dépendances : `pip install jinja2 weasyprint`
(Docker Debian : `apt-get install -y libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core`)

## Test en CLI (depuis le dossier filter_report/)
```bash
cd filter_report
python fiche.py --input examples/example_reel.json --out /tmp/fiche.pdf -v
```

## Les imports
Les modules s'importent entre eux en absolu (`from builder import …`, `from catalog import …`).
Pour les appeler depuis FastAPI, ajouter le dossier au chemin une fois, avant l'import :
```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "filter_report"))   # ← adapter au chemin réel
from fiche import render_fiche_pdf_bytes
```

## Route FastAPI (exemple)
```python
from fastapi import APIRouter, Response
from fastapi.concurrency import run_in_threadpool

router = APIRouter()

@router.get("/projects/{project_id}/filtrage/fiche.pdf")
async def fiche_filtrage(project_id: str):
    data = load_fiche_input(project_id)            # ← à écrire, voir ci-dessous
    pdf = await run_in_threadpool(render_fiche_pdf_bytes, data)   # WeasyPrint est synchrone
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="parametres_filtrage_{project_id}.pdf"'})
```

## Données d'entrée attendues (`data`)
```python
{
  "etude": {"nom": "...", "reference": "...", "projet": "...", "auteur": "...", "organisme": "Kerelia"},  # tout optionnel
  "computed_at": "2026-11-04T09:40:00+01:00",   # date du calcul (optionnel)
  "buffer_km": 5,                                # aoi.buffer_m / 1000
  "filter_config": {...},                        # SNAPSHOT du calcul : last_results.filter_config
  "funnel": [{"key": "...", "label": "...", "count": 668}, ...],   # log d'entonnoir (peut être [])
  "total": 36                                    # nb de parcelles retenues
}
```
Seuls `filter_config` et `total` sont réellement indispensables pour la fiche ; `funnel` peut être une liste vide.
