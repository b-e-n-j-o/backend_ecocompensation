# test_uf_layers.py
from db import get_engine
from layers.layer_runner import LAYER_REGISTRY

engine = get_engine()

# Récupérer le dernier projet
from sqlalchemy import text
with engine.begin() as conn:
    row = conn.execute(text(
        "SELECT id, aoi_id FROM ecocompensation.projects "
        "WHERE aoi_id IS NOT NULL ORDER BY created_at DESC LIMIT 1"
    )).mappings().one()
    project_id = str(row["id"])
    aoi_id     = str(row["aoi_id"])

print(f"Projet : {project_id} | AOI : {aoi_id}\n")

# Filtrer uniquement les deux couches UF
uf_keys = {"unites_foncieres", "sous_ensembles"}
uf_layers = [l for l in LAYER_REGISTRY if l["key"] in uf_keys]

for layer in uf_layers:
    print(f"▶ {layer['label']}...")
    result = layer["fn"](engine, project_id, aoi_id, cb=print)
    if result.success:
        print(f"✅ {result.n_inserted:,} lignes en {result.duration_s:.1f}s\n")
    else:
        print(f"❌ Erreur : {result.error}\n")