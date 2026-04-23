import requests, json
from pathlib import Path

gpu_doc_id = "f26c159f49168100834810a8d7834216"

# Réponse complète /details
r = requests.get(f"https://www.geoportail-urbanisme.gouv.fr/api/document/{gpu_doc_id}/details")
data = r.json()
print(json.dumps(data, indent=2))

# ZIP → liste des fichiers sans extraire
import zipfile, io
r2 = requests.get("https://www.geoportail-urbanisme.gouv.fr/api/document/download-by-partition/DU_33234")
if r2.status_code == 200:
    with zipfile.ZipFile(io.BytesIO(r2.content)) as z:
        for name in z.namelist():
            print(name)