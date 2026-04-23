# Identité Foncière V0 — France entière via WFS

Prototype simplifié : **référence cadastrale → rapport PDF** en s'appuyant
exclusivement sur les flux WFS publics (IGN + GPU), sans aucun stockage en base.

## Architecture

```
identite_fonciere_v0/
├── main.py                  # FastAPI app + endpoint POST /rapport
├── core/
│   ├── parcelle.py          # Récupération géométrie IGN Parcellaire Express
│   ├── gpu_wfs.py           # Client WFS Géoportail Urbanisme (PLU, SUP, prescriptions)
│   ├── intersections.py     # Orchestration : parcelle → intersections toutes couches
│   └── unites_foncieres.py  # Fusion de plusieurs parcelles contiguës en UF
├── visuels/
│   ├── carte_parcelle.py    # Carte matplotlib + contextily (fond satellite)
│   └── carte_plu.py         # Carte PLU avec couleurs typezone CNIG
├── pdf/
│   └── rapport.py           # Génération PDF ReportLab (calqué sur rapport_identite_fonciere.py)
└── utils/
    └── geo.py               # Helpers SRID, WKT, bbox, GeoDataFrame
```

## Flux de données

```
POST /rapport
  { "parcelles": [{"section":"AC","numero":"0042","insee":"33522","commune":"Bordeaux"}] }
        │
        ▼
  core/parcelle.py  →  IGN WFS Parcellaire Express  →  géométrie WKT EPSG:4326
        │
        ▼
  core/unites_foncieres.py  →  union des géométries si plusieurs parcelles
        │
        ▼
  core/gpu_wfs.py  →  GPU WFS (PLU, servitudes, prescriptions, préemption)
        │             appels parallèles, bbox ± 500m autour de l'UF
        ▼
  core/intersections.py  →  ST_Intersects simulé côté Python (Shapely)
        │
        ▼
  visuels/  →  PNG carte satellite + PLU
        │
        ▼
  pdf/rapport.py  →  PDF ReportLab  →  retourné en réponse HTTP
```

## Couches WFS utilisées

| Couche              | Endpoint WFS                        | Filtre       |
|---------------------|-------------------------------------|--------------|
| Parcelle cadastrale | IGN Parcellaire Express             | section+num  |
| Zonage PLU          | GPU `wfs_du:zone_urba`              | bbox + CQL   |
| Servitudes (surf.)  | GPU `wfs_sup:assiette_sup_s`        | bbox         |
| Prescriptions surf. | GPU `wfs_du:prescription_surf`      | bbox         |
| Prescriptions lin.  | GPU `wfs_du:prescription_lin`       | bbox         |
| Prescriptions pct.  | GPU `wfs_du:prescription_pct`       | bbox         |
| Droits préemption   | GPU `wfs_du:zone_pdc`               | bbox         |

## Installation

```bash
pip install fastapi uvicorn requests geopandas shapely \
            reportlab matplotlib contextily pyproj
```

## Lancement

```bash
uvicorn main:app --reload --port 8000
```

## Exemple de requête

```bash
curl -X POST http://localhost:8000/rapport \
  -H "Content-Type: application/json" \
  -d '{
    "parcelles": [
      {"section": "AC", "numero": "0042", "insee": "33522", "commune": "Bordeaux"}
    ]
  }' \
  --output rapport.pdf
```
