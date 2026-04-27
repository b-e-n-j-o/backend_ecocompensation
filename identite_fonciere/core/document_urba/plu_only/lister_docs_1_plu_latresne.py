"""
Récupération du règlement PLU — Latresne (33234)
Script de test minimal avec logs détaillés.

Objectif du script :
    - Récupérer le document d'urbanisme (doc_urba_com) pour la commune de Latresne (33234)
    - Récupérer les métadonnées et URLs des pièces écrites
    - Identifier le règlement
    - Télécharger le PDF du règlement
    - Vérifier la signature PDF
    - Résumer
"""
import io
import time
import requests

GPU_API = "https://www.geoportail-urbanisme.gouv.fr/api"
WFS_BASE = "https://data.geopf.fr/wfs/ows"

INSEE = "33234"
COMMUNE = "Latresne"


def log(msg: str) -> None:
    print(f"  {msg}")


def step(n: int, title: str) -> None:
    print(f"\n[{n}] {title}")
    print(f"    {'─' * (len(title) + 2)}")


# ---------------------------------------------------------------------------
# Étape 1 — WFS doc_urba_com → gpu_doc_id
# ---------------------------------------------------------------------------
step(1, f"Recherche document d'urbanisme — commune {COMMUNE} ({INSEE})")

t0 = time.time()
resp = requests.get(WFS_BASE, params={
    "SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature",
    "typeNames": "wfs_du:doc_urba_com",
    "outputFormat": "application/json",
    "CQL_FILTER": f"insee='{INSEE}'",
    "count": "5",
}, timeout=20)
resp.raise_for_status()
features = resp.json().get("features", [])
log(f"Requête WFS doc_urba_com    → {resp.elapsed.total_seconds():.2f}s  |  {len(resp.content)//1024} Ko reçus")

if not features:
    print("\n  ✗ Aucun document d'urbanisme trouvé pour cette commune.")
    exit(1)

props = features[0]["properties"]
idurba     = props.get("idurba", "")
gpu_doc_id = props.get("gpu_doc_id", "")
partition  = props.get("partition", "")
gpu_status = props.get("gpu_status", "")

log(f"idurba       : {idurba}")
log(f"gpu_doc_id   : {gpu_doc_id}")
log(f"partition    : {partition}")
log(f"gpu_status   : {gpu_status}")

if not gpu_doc_id:
    print("\n  ✗ gpu_doc_id vide — impossible de continuer.")
    exit(1)

# ---------------------------------------------------------------------------
# Étape 2 — API GPU /details → writingMaterials
# ---------------------------------------------------------------------------
step(2, "Récupération des métadonnées et URLs des pièces écrites")

resp2 = requests.get(f"{GPU_API}/document/{gpu_doc_id}/details", timeout=20)
resp2.raise_for_status()
details = resp2.json()
log(f"Requête API /details        → {resp2.elapsed.total_seconds():.2f}s  |  {len(resp2.content)//1024} Ko reçus")

log(f"Titre        : {details.get('title', '')}")
log(f"Type         : {details.get('type', '')}")
log(f"Statut légal : {details.get('legalStatus', '')}")
log(f"Publié le    : {details.get('publicationDate', '')}")

writing_materials = details.get("writingMaterials", {})
log(f"\n  {len(writing_materials)} fichier(s) disponible(s) :")
for nom in writing_materials:
    log(f"    • {nom}")

# ---------------------------------------------------------------------------
# Étape 3 — Identification du règlement
# ---------------------------------------------------------------------------
step(3, "Identification du fichier règlement")

KEYWORDS_OK  = ["reglement", "règlement", "regl", "regt"]
KEYWORDS_NOK = ["graphique", "plan", "zonage", "legende", "carte"]

scores: dict[str, int] = {}
for nom in writing_materials:
    nom_lower = nom.lower()
    score = 0
    for kw in KEYWORDS_OK:
        if kw in nom_lower:
            score += 10
    for kw in KEYWORDS_NOK:
        if kw in nom_lower:
            score -= 8
    if nom_lower.endswith(".pdf"):
        score += 2
    scores[nom] = score

scored = sorted(scores.items(), key=lambda x: x[1], reverse=True)
log("Scores de pertinence :")
for nom, score in scored:
    marker = " ← choisi" if nom == scored[0][0] and scored[0][1] > 0 else ""
    log(f"    {score:+3d}  {nom}{marker}")

best_nom, best_score = scored[0]
if best_score <= 0:
    print("\n  ✗ Aucun fichier identifié comme règlement.")
    print("    Fichiers disponibles :", list(writing_materials.keys()))
    exit(1)

reglement_nom = best_nom
reglement_url = writing_materials[reglement_nom]
log(f"\n  → Règlement identifié : {reglement_nom}")
log(f"  → URL               : {reglement_url}")

# ---------------------------------------------------------------------------
# Étape 4 — Téléchargement du PDF avec suivi de progression
# ---------------------------------------------------------------------------
step(4, "Téléchargement du règlement PDF")

t_dl = time.time()
resp3 = requests.get(reglement_url, stream=True, timeout=60)
resp3.raise_for_status()

total_size = int(resp3.headers.get("Content-Length", 0))
if total_size:
    log(f"Taille annoncée  : {total_size / 1024 / 1024:.2f} Mo")
else:
    log("Taille annoncée  : inconnue (pas de Content-Length)")

chunks = []
downloaded = 0
last_log = 0
CHUNK = 64 * 1024  # 64 Ko

for chunk in resp3.iter_content(chunk_size=CHUNK):
    if chunk:
        chunks.append(chunk)
        downloaded += len(chunk)
        # Log tous les 512 Ko
        if downloaded - last_log >= 512 * 1024:
            elapsed = time.time() - t_dl
            speed = downloaded / elapsed / 1024  # Ko/s
            if total_size:
                pct = 100 * downloaded / total_size
                bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
                print(f"    [{bar}] {pct:5.1f}%  {downloaded//1024:>5} Ko  {speed:.0f} Ko/s")
            else:
                print(f"    {downloaded//1024:>5} Ko téléchargés  {speed:.0f} Ko/s")
            last_log = downloaded

pdf_bytes = b"".join(chunks)
dl_time = time.time() - t_dl

log(f"\n  Téléchargement terminé")
log(f"  Taille réelle    : {len(pdf_bytes) / 1024 / 1024:.2f} Mo ({len(pdf_bytes):,} octets)")
log(f"  Durée            : {dl_time:.2f}s")
log(f"  Débit moyen      : {len(pdf_bytes) / dl_time / 1024:.0f} Ko/s")
log(f"  Stockage disque  : 0 octet (PDF en RAM uniquement)")

# Vérification signature PDF
is_valid_pdf = pdf_bytes[:4] == b"%PDF"
log(f"  Signature %PDF   : {'✓ valide' if is_valid_pdf else '✗ invalide'}")

if not is_valid_pdf:
    print("\n  ✗ Le fichier téléchargé n'est pas un PDF valide.")
    exit(1)

# ---------------------------------------------------------------------------
# Résumé
# ---------------------------------------------------------------------------
total_time = time.time() - t0
print(f"\n{'='*55}")
print(f"  SUCCÈS — Règlement PLU disponible en mémoire")
print(f"{'='*55}")
print(f"  Commune       : {COMMUNE} ({INSEE})")
print(f"  Document      : {idurba}")
print(f"  Fichier       : {reglement_nom}")
print(f"  Taille PDF    : {len(pdf_bytes) / 1024 / 1024:.2f} Mo")
print(f"  Durée totale  : {total_time:.2f}s (dont {dl_time:.2f}s download)")
print(f"  Data consommée: {(len(resp.content) + len(resp2.content) + len(pdf_bytes)) / 1024 / 1024:.2f} Mo")
print(f"  En mémoire    : pdf_bytes ({len(pdf_bytes):,} octets) prêt pour PyMuPDF / Gemini")
print(f"{'='*55}\n")