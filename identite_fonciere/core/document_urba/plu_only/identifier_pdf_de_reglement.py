"""
identifier_pdf_de_reglement.py
Identifier le PDF de règlement dans les fichiers disponibles sur le GPU
Batch test récupération règlement PLU — 50 communes
Lit les codes INSEE depuis le JSON local.
"""
import json
import time
from pathlib import Path

import requests
from tabulate import tabulate

JSON_PATH = Path("/Volumes/T7/Travaux_Freelance/KERELIA/CUAs/COMPENSATION_PARCELLE/COMPENSATION_ECO/backend/identite_fonciere/DATA/batch_de_codes_insee.json")

GPU_API  = "https://www.geoportail-urbanisme.gouv.fr/api"
WFS_BASE = "https://data.geopf.fr/wfs/ows"

KEYWORDS_OK  = ["reglement", "règlement", "regl", "regt"]
KEYWORDS_NOK = ["graphique", "plan", "zonage", "legende", "carte"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fetch_doc_urba_com(insee: str) -> dict | None:
    resp = requests.get(WFS_BASE, params={
        "SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature",
        "typeNames": "wfs_du:doc_urba_com",
        "outputFormat": "application/json",
        "CQL_FILTER": f"insee='{insee}'",
        "count": "5",
    }, timeout=20)
    resp.raise_for_status()
    features = resp.json().get("features", [])
    if not features:
        return None
    prod = [f for f in features
            if f.get("properties", {}).get("gpu_status") == "production"]
    return (prod[0] if prod else features[0])["properties"]


def fetch_doc_details(gpu_doc_id: str) -> dict:
    resp = requests.get(f"{GPU_API}/document/{gpu_doc_id}/details", timeout=20)
    resp.raise_for_status()
    return resp.json()


def find_reglement_key(writing_materials: dict) -> tuple[str | None, int]:
    """Retourne (meilleure_clé, score). Score <= 0 = pas trouvé."""
    best_key, best_score = None, -999
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
        if score > best_score:
            best_score = score
            best_key = nom
    return (best_key if best_score > 0 else None), best_score


# ---------------------------------------------------------------------------
# Test une commune (sans téléchargement PDF)
# ---------------------------------------------------------------------------

def test_commune(insee: str, commune: str) -> dict:
    t0 = time.time()
    result = {
        "insee": insee, "commune": commune,
        "status": "❌", "typedoc": "", "idurba": "",
        "nb_fichiers": 0, "reglement_trouve": "",
        "score": 0, "tous_fichiers": "", "erreur": "", "duree_s": 0.0,
    }
    try:
        props = fetch_doc_urba_com(insee)
        if not props:
            result["status"] = "⬜ pas de doc GPU"
            result["duree_s"] = round(time.time() - t0, 2)
            return result

        gpu_doc_id = props.get("gpu_doc_id", "")
        result["idurba"] = props.get("idurba", "")

        if not gpu_doc_id:
            result["status"] = "⬜ gpu_doc_id vide"
            result["duree_s"] = round(time.time() - t0, 2)
            return result

        details = fetch_doc_details(gpu_doc_id)
        result["typedoc"] = details.get("type", "")
        wm = details.get("writingMaterials", {})
        result["nb_fichiers"] = len(wm)
        result["tous_fichiers"] = " | ".join(wm.keys())

        key, score = find_reglement_key(wm)
        result["score"] = score
        if key:
            result["reglement_trouve"] = key
            result["status"] = "✅"
        else:
            result["status"] = "⚠️ pas identifié"

    except requests.HTTPError as e:
        result["erreur"] = f"HTTP {e.response.status_code}"
    except Exception as e:
        result["erreur"] = str(e)[:60]

    result["duree_s"] = round(time.time() - t0, 2)
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # Lecture JSON
    if not JSON_PATH.exists():
        print(f"✗ Fichier introuvable : {JSON_PATH}")
        return

    communes = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    # Support formats {"commune": ..., "code_insee": ...} ou {"nom": ..., "insee": ...}
    panel = []
    for c in communes[:50]:
        insee   = c.get("code_insee") or c.get("insee") or ""
        commune = c.get("commune")    or c.get("nom")   or insee
        if insee:
            panel.append((insee, commune))

    print(f"\nBatch test règlement PLU — {len(panel)} communes")
    print(f"Pas de téléchargement PDF — test API uniquement\n")

    results = []
    for i, (insee, commune) in enumerate(panel, 1):
        print(f"  [{i:02d}/{len(panel)}] {commune} ({insee})...", end=" ", flush=True)
        r = test_commune(insee, commune)
        results.append(r)
        print(f"{r['status']}  ({r['duree_s']}s)")
        if r["erreur"]:
            print(f"         ⚠ {r['erreur']}")
        time.sleep(0.3)

    # --- Rapport ---
    ok      = [r for r in results if r["status"] == "✅"]
    no_doc  = [r for r in results if "pas de doc" in r["status"] or "gpu_doc_id" in r["status"]]
    no_regl = [r for r in results if "pas identifié" in r["status"]]
    errors  = [r for r in results if r["status"] == "❌"]

    print(f"\n{'='*65}")
    print(f"RÉSULTATS — {len(panel)} communes testées")
    print(f"{'='*65}")
    print(f"  ✅ Règlement trouvé    : {len(ok):>3}  ({100*len(ok)//len(panel)}%)")
    print(f"  ⬜ Pas de doc GPU      : {len(no_doc):>3}  ({100*len(no_doc)//len(panel)}%)")
    print(f"  ⚠️  Pas identifié       : {len(no_regl):>3}  ({100*len(no_regl)//len(panel)}%)")
    print(f"  ❌ Erreur API          : {len(errors):>3}  ({100*len(errors)//len(panel)}%)")

    # Tableau complet
    print(f"\n--- TABLEAU COMPLET ---")
    rows = [{
        "INSEE": r["insee"],
        "Commune": r["commune"][:16],
        "Statut": r["status"],
        "Type": r["typedoc"],
        "Fichiers": r["nb_fichiers"],
        "Règlement trouvé": r["reglement_trouve"][:35] if r["reglement_trouve"] else "",
        "Score": r["score"] if r["score"] != 0 else "",
        "s": r["duree_s"],
    } for r in results]
    print(tabulate(rows, headers="keys", tablefmt="rounded_grid"))

    # Cas en échec — affiche tous les fichiers pour diagnostic
    if no_regl:
        print(f"\n--- ⚠️  RÈGLEMENT NON IDENTIFIÉ — fichiers disponibles ---")
        for r in no_regl:
            print(f"\n  [{r['insee']}] {r['commune']} ({r['typedoc']})")
            for f in r["tous_fichiers"].split(" | "):
                print(f"    • {f}")

    if errors:
        print(f"\n--- ❌ ERREURS ---")
        for r in errors:
            print(f"  [{r['insee']}] {r['commune']} → {r['erreur']}")

    # Stats nommage sur les succès
    print(f"\n--- PATTERNS DE NOMMAGE (succès) ---")
    from collections import Counter
    import re
    patterns = Counter()
    for r in ok:
        nom = r["reglement_trouve"].lower()
        if "reglement" in nom:   patterns["*_REGLEMENT_*.pdf"] += 1
        elif "regl" in nom:      patterns["*_REGL*.pdf"] += 1
        elif "règlement" in nom: patterns["*_règlement_*.pdf"] += 1
        elif "regt" in nom:      patterns["*_regt*.pdf"] += 1
        else:                    patterns[f"autre: {nom}"] += 1
    for pat, count in patterns.most_common():
        print(f"  {count:>3}x  {pat}")


if __name__ == "__main__":
    main()