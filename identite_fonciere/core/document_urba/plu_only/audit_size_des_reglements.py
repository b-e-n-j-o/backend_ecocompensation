"""
Taille des PDFs de règlement PLU sans téléchargement.
Utilise HEAD request pour récupérer Content-Length uniquement.
"""
import json, time
from pathlib import Path
import requests
from tabulate import tabulate

JSON_PATH = Path("/Volumes/T7/Travaux_Freelance/KERELIA/CUAs/COMPENSATION_PARCELLE/COMPENSATION_ECO/backend/identite_fonciere/DATA/batch_de_codes_insee.json")
GPU_API  = "https://www.geoportail-urbanisme.gouv.fr/api"
WFS_BASE = "https://data.geopf.fr/wfs/ows"
KEYWORDS_OK  = ["reglement", "règlement", "regl", "regt"]
KEYWORDS_NOK = ["graphique", "plan", "zonage", "legende", "carte"]


def fetch_doc_urba_com(insee):
    resp = requests.get(WFS_BASE, params={
        "SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature",
        "typeNames": "wfs_du:doc_urba_com", "outputFormat": "application/json",
        "CQL_FILTER": f"insee='{insee}'", "count": "5",
    }, timeout=20)
    features = resp.json().get("features", [])
    if not features: return None
    prod = [f for f in features if f["properties"].get("gpu_status") == "production"]
    return (prod[0] if prod else features[0])["properties"]


def find_reglement_url(wm):
    best_key, best_score = None, -999
    for nom in wm:
        s = sum(10 for kw in KEYWORDS_OK if kw in nom.lower())
        s -= sum(8 for kw in KEYWORDS_NOK if kw in nom.lower())
        s += 2 if nom.lower().endswith(".pdf") else 0
        if s > best_score: best_score, best_key = s, nom
    return (best_key, wm[best_key]) if best_key and best_score > 0 else (None, None)


def get_file_size_ko(url):
    """HEAD request — aucun contenu téléchargé."""
    try:
        r = requests.head(url, timeout=10, allow_redirects=True)
        cl = r.headers.get("Content-Length")
        if cl: return int(cl) // 1024
        # Certains serveurs ne répondent pas au HEAD → GET avec stream + arrêt immédiat
        r2 = requests.get(url, stream=True, timeout=10)
        cl2 = r2.headers.get("Content-Length")
        r2.close()
        return int(cl2) // 1024 if cl2 else None
    except Exception as e:
        return None


communes = json.loads(JSON_PATH.read_text())
panel = [(c.get("code_insee") or c.get("insee"), c.get("commune") or c.get("nom"))
         for c in communes[:50] if c.get("code_insee") or c.get("insee")]

print(f"\nEstimation taille PDFs règlement — {len(panel)} communes (HEAD only)\n")

rows, total_ko, inconnu = [], 0, 0
for i, (insee, commune) in enumerate(panel, 1):
    print(f"  [{i:02d}/{len(panel)}] {commune} ({insee})...", end=" ", flush=True)
    try:
        props = fetch_doc_urba_com(insee)
        if not props: print("⬜ pas de doc"); continue
        details = requests.get(f"{GPU_API}/document/{props['gpu_doc_id']}/details", timeout=15).json()
        nom, url = find_reglement_url(details.get("writingMaterials", {}))
        if not url: print("⬜ non identifié"); continue
        taille = get_file_size_ko(url)
        if taille:
            total_ko += taille
            flag = "🔴" if taille > 20_000 else "🟡" if taille > 5_000 else "🟢"
            print(f"{flag} {taille:>7,} Ko  ({taille/1024:.1f} Mo)")
        else:
            inconnu += 1
            print("❓ taille inconnue")
        rows.append({"INSEE": insee, "Commune": commune[:18],
                     "Type": details.get("type",""), "Fichier": nom[:35] if nom else "",
                     "Ko": f"{taille:,}" if taille else "?",
                     "Mo": f"{taille/1024:.1f}" if taille else "?"})
    except Exception as e:
        print(f"❌ {e}")
    time.sleep(0.2)

print(f"\n{'='*55}")
print(tabulate(rows, headers="keys", tablefmt="rounded_grid"))
print(f"\n  Total estimé   : {total_ko:,} Ko  ({total_ko/1024:.0f} Mo)")
print(f"  Taille inconnue: {inconnu} commune(s)")
print(f"  🟢 < 5 Mo  🟡 5-20 Mo  🔴 > 20 Mo")