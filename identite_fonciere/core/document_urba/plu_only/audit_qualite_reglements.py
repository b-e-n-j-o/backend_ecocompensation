"""
audit_qualite_reglements.py
Audit qualité des PDFs de règlement PLU sur le batch de communes.

Pour chaque commune avec un règlement identifié :
  - Télécharge le PDF (avec cache T7)
  - Analyse la qualité via analyser_qualite_pdf()
  - Métriques : pages, poids, chars, verdict textuel/scanné
  - Estimation tokens LLM
  - Rapport final avec tableau + stats
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import fitz
import requests
from tabulate import tabulate

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

JSON_PATH = Path("/Volumes/T7/Travaux_Freelance/KERELIA/CUAs/COMPENSATION_PARCELLE/COMPENSATION_ECO/backend/identite_fonciere/DATA/batch_de_codes_insee.json")

CACHE_DIR = Path("/Volumes/T7/.cache_reglements_plu")

GPU_API  = "https://www.geoportail-urbanisme.gouv.fr/api"
WFS_BASE = "https://data.geopf.fr/wfs/ows"

KEYWORDS_OK  = ["reglement", "règlement", "regl", "regt"]
KEYWORDS_NOK = ["graphique", "plan", "zonage", "legende", "carte"]

# Seuils qualité
SEUIL_CHARS_PAGE_SCANNEE  = 80    # chars/page en dessous = scanné
SEUIL_CHARS_TOTAL_MIN     = 5_000 # total en dessous = inutilisable
SEUIL_PCT_TEXTUEL         = 0.60  # % pages textuelles minimum
CHARS_PAR_TOKEN           = 4     # estimation tokens GPT/Gemini

MOTS_URBANISME = [
    "zone", "article", "destination", "construction", "hauteur",
    "emprise", "stationnement", "clôture", "recul", "implantation",
    "coefficient", "prospect", "alignement", "façade",
    "autorisation", "interdit", "applicable",
]


# ---------------------------------------------------------------------------
# Fetch avec cache
# ---------------------------------------------------------------------------

def fetch_doc_urba_com(insee: str) -> Optional[dict]:
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
            if f["properties"].get("gpu_status") == "production"]
    return (prod[0] if prod else features[0])["properties"]


def find_reglement_url(writing_materials: dict) -> Optional[tuple[str, str]]:
    """Retourne (nom_fichier, url) du règlement, ou None."""
    best_key, best_score = None, -999
    for nom in writing_materials:
        nom_lower = nom.lower()
        score = sum(10 for kw in KEYWORDS_OK if kw in nom_lower)
        score -= sum(8 for kw in KEYWORDS_NOK if kw in nom_lower)
        score += 2 if nom_lower.endswith(".pdf") else 0
        if score > best_score:
            best_score, best_key = score, nom
    if best_key and best_score > 0:
        return best_key, writing_materials[best_key]
    return None


def fetch_pdf_with_cache(insee: str, nom_fichier: str, url: str) -> tuple[bytes, bool]:
    """
    Retourne (pdf_bytes, depuis_cache).
    Écrit dans le cache si pas encore présent.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"{insee}_{nom_fichier}"

    if cache_path.exists():
        return cache_path.read_bytes(), True

    # Téléchargement avec progression
    resp = requests.get(url, stream=True, timeout=60)
    resp.raise_for_status()

    chunks = []
    downloaded = 0
    for chunk in resp.iter_content(chunk_size=64 * 1024):
        if chunk:
            chunks.append(chunk)
            downloaded += len(chunk)

    pdf_bytes = b"".join(chunks)
    cache_path.write_bytes(pdf_bytes)
    return pdf_bytes, False


# ---------------------------------------------------------------------------
# Analyse qualité PDF
# ---------------------------------------------------------------------------

@dataclass
class AuditPDF:
    insee: str
    commune: str
    typedoc: str
    nom_fichier: str
    # Métriques fichier
    poids_ko: int
    depuis_cache: bool
    # Métriques extraction
    n_pages: int
    n_pages_textuelles: int
    n_pages_scannees: int
    chars_total: int
    chars_moy_par_page: int
    pct_pages_textuelles: float
    # Métriques LLM
    tokens_estimes: int
    # Structure
    n_blocs_image: int
    n_blocs_texte: int
    mots_urbanisme_trouves: int
    # Verdict
    verdict: str        # TEXTUEL / MIXTE / SCANNE / VIDE
    utilisable: bool
    detail: str
    # Divers
    duree_s: float
    erreur: str = ""


def analyser_pdf(
    pdf_bytes: bytes,
    insee: str,
    commune: str,
    typedoc: str,
    nom_fichier: str,
    poids_ko: int,
    depuis_cache: bool,
    duree_s: float,
) -> AuditPDF:

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    n_pages = len(doc)

    if n_pages == 0:
        return AuditPDF(
            insee=insee, commune=commune, typedoc=typedoc,
            nom_fichier=nom_fichier, poids_ko=poids_ko,
            depuis_cache=depuis_cache, n_pages=0,
            n_pages_textuelles=0, n_pages_scannees=0,
            chars_total=0, chars_moy_par_page=0,
            pct_pages_textuelles=0.0, tokens_estimes=0,
            n_blocs_image=0, n_blocs_texte=0,
            mots_urbanisme_trouves=0,
            verdict="VIDE", utilisable=False,
            detail="PDF sans pages", duree_s=duree_s,
        )

    # --- Extraction page par page ---
    chars_par_page: list[int] = []
    texte_complet = ""
    for page in doc:
        texte = page.get_text()
        chars_par_page.append(len(texte.strip()))
        texte_complet += texte

    chars_total = sum(chars_par_page)
    chars_moy   = chars_total // n_pages
    n_scannees  = sum(1 for c in chars_par_page if c < SEUIL_CHARS_PAGE_SCANNEE)
    n_textuelles = n_pages - n_scannees
    pct_textuel = n_textuelles / n_pages

    # --- Blocs image vs texte (échantillon 10 pages) ---
    n_blocs_image = n_blocs_texte = 0
    for page in list(doc)[:min(10, n_pages)]:
        for b in page.get_text("blocks"):
            if b[6] == 1:
                n_blocs_image += 1
            else:
                n_blocs_texte += 1

    # --- Mots urbanisme ---
    texte_lower = texte_complet.lower()
    n_mots = sum(1 for m in MOTS_URBANISME if m in texte_lower)

    # --- Verdict ---
    details = []
    if chars_total < SEUIL_CHARS_TOTAL_MIN:
        verdict, utilisable = "VIDE", False
        details.append(f"seulement {chars_total} chars")
    elif pct_textuel < SEUIL_PCT_TEXTUEL:
        verdict, utilisable = "SCANNE", False
        details.append(f"{n_scannees}/{n_pages} pages scannées")
    elif pct_textuel < 0.85:
        verdict, utilisable = "MIXTE", False
        details.append(f"{n_scannees} pages scannées sur {n_pages}")
    elif chars_moy < 200:
        verdict, utilisable = "MIXTE", False
        details.append(f"moy {chars_moy} chars/page très faible")
    elif n_mots < 3:
        verdict, utilisable = "TROP_COURT", False
        details.append(f"seulement {n_mots} mots urbanisme trouvés")
    else:
        verdict, utilisable = "TEXTUEL", True

    if n_blocs_image > n_blocs_texte and n_blocs_texte > 0:
        details.append(f"dominante image ({n_blocs_image} img vs {n_blocs_texte} txt blocs)")

    return AuditPDF(
        insee=insee, commune=commune, typedoc=typedoc,
        nom_fichier=nom_fichier, poids_ko=poids_ko,
        depuis_cache=depuis_cache, n_pages=n_pages,
        n_pages_textuelles=n_textuelles, n_pages_scannees=n_scannees,
        chars_total=chars_total, chars_moy_par_page=chars_moy,
        pct_pages_textuelles=round(pct_textuel * 100, 1),
        tokens_estimes=chars_total // CHARS_PAR_TOKEN,
        n_blocs_image=n_blocs_image, n_blocs_texte=n_blocs_texte,
        mots_urbanisme_trouves=n_mots,
        verdict=verdict, utilisable=utilisable,
        detail=" | ".join(details), duree_s=duree_s,
    )


# ---------------------------------------------------------------------------
# Icônes verdict
# ---------------------------------------------------------------------------

VERDICT_ICON = {
    "TEXTUEL":    "✅",
    "MIXTE":      "⚠️",
    "SCANNE":     "❌",
    "VIDE":       "❌",
    "TROP_COURT": "⚠️",
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50,
                        help="Nombre de communes à tester (défaut: 50)")
    parser.add_argument("--no-cache", action="store_true",
                        help="Ignore le cache, retélécharge tout")
    args = parser.parse_args()

    if not JSON_PATH.exists():
        print(f"✗ JSON introuvable : {JSON_PATH}")
        return

    communes = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    panel = []
    for c in communes[:args.limit]:
        insee   = c.get("code_insee") or c.get("insee") or ""
        commune = c.get("commune")    or c.get("nom")   or insee
        if insee:
            panel.append((insee, commune))

    print(f"\nAudit qualité PDFs règlement PLU — {len(panel)} communes")
    print(f"Cache : {CACHE_DIR}")
    print(f"{'─'*55}\n")

    audits: list[AuditPDF] = []
    total_downloaded_ko = 0

    for i, (insee, commune) in enumerate(panel, 1):
        print(f"  [{i:02d}/{len(panel)}] {commune} ({insee})", end=" ", flush=True)
        t0 = time.time()

        try:
            # 1. doc_urba_com
            props = fetch_doc_urba_com(insee)
            if not props:
                print("⬜ pas de doc GPU")
                time.sleep(0.3)
                continue

            gpu_doc_id = props.get("gpu_doc_id", "")
            if not gpu_doc_id:
                print("⬜ gpu_doc_id vide")
                time.sleep(0.3)
                continue

            # 2. details
            details = requests.get(
                f"{GPU_API}/document/{gpu_doc_id}/details", timeout=20
            ).json()
            typedoc = details.get("type", "")
            wm = details.get("writingMaterials", {})

            # 3. règlement
            result = find_reglement_url(wm)
            if not result:
                print("⬜ règlement non identifié")
                time.sleep(0.3)
                continue
            nom_fichier, url = result

            # 4. téléchargement avec cache
            if args.no_cache:
                cache_path = CACHE_DIR / f"{insee}_{nom_fichier}"
                if cache_path.exists():
                    cache_path.unlink()

            pdf_bytes, depuis_cache = fetch_pdf_with_cache(insee, nom_fichier, url)
            poids_ko = len(pdf_bytes) // 1024
            if not depuis_cache:
                total_downloaded_ko += poids_ko

            duree = round(time.time() - t0, 2)

            # 5. analyse qualité
            audit = analyser_pdf(
                pdf_bytes=pdf_bytes,
                insee=insee, commune=commune, typedoc=typedoc,
                nom_fichier=nom_fichier, poids_ko=poids_ko,
                depuis_cache=depuis_cache, duree_s=duree,
            )
            audits.append(audit)

            cache_tag = "💾cache" if depuis_cache else f"⬇️ {poids_ko} Ko"
            print(
                f"{VERDICT_ICON.get(audit.verdict, '?')} {audit.verdict:10}"
                f"  {audit.n_pages:>3}p  {poids_ko:>5} Ko"
                f"  {audit.chars_total:>8,} chars"
                f"  ~{audit.tokens_estimes:>6,} tok"
                f"  {cache_tag}  ({duree}s)"
            )
            if audit.detail:
                print(f"           ↳ {audit.detail}")

        except Exception as e:
            print(f"❌ ERREUR : {e}")

        time.sleep(0.2)

    # -----------------------------------------------------------------------
    # Rapport final
    # -----------------------------------------------------------------------
    if not audits:
        print("\nAucun audit réalisé.")
        return

    utilisables = [a for a in audits if a.utilisable]
    non_util    = [a for a in audits if not a.utilisable]

    print(f"\n{'='*70}")
    print(f"RAPPORT D'AUDIT — {len(audits)} PDFs analysés")
    print(f"{'='*70}")
    print(f"  ✅ Textuels (utilisables)  : {len(utilisables):>3}  ({100*len(utilisables)//len(audits)}%)")
    print(f"  ❌ Non utilisables         : {len(non_util):>3}  ({100*len(non_util)//len(audits)}%)")
    print(f"  ⬇️  Data téléchargée        : {total_downloaded_ko/1024:.1f} Mo")
    print(f"  💾 Cache utilisé           : {sum(1 for a in audits if a.depuis_cache)} fois")

    if utilisables:
        total_chars  = sum(a.chars_total for a in utilisables)
        total_tokens = sum(a.tokens_estimes for a in utilisables)
        moy_pages    = sum(a.n_pages for a in utilisables) // len(utilisables)
        moy_tokens   = total_tokens // len(utilisables)
        print(f"\n  Stats sur les utilisables :")
        print(f"    Pages moy/règlement    : {moy_pages}")
        print(f"    Chars total corpus     : {total_chars:,}")
        print(f"    Tokens total corpus    : {total_tokens:,}")
        print(f"    Tokens moy/règlement   : {moy_tokens:,}")
        max_tok = max(utilisables, key=lambda a: a.tokens_estimes)
        min_tok = min(utilisables, key=lambda a: a.tokens_estimes)
        print(f"    Plus volumineux        : {max_tok.commune} ({max_tok.tokens_estimes:,} tok, {max_tok.n_pages}p)")
        print(f"    Plus léger             : {min_tok.commune} ({min_tok.tokens_estimes:,} tok, {min_tok.n_pages}p)")

    # Tableau complet
    print(f"\n{'─'*70}")
    print("TABLEAU COMPLET")
    rows = []
    for a in audits:
        rows.append({
            "INSEE":    a.insee,
            "Commune":  a.commune[:14],
            "Type":     a.typedoc,
            "Verdict":  f"{VERDICT_ICON.get(a.verdict,'?')} {a.verdict}",
            "Pages":    a.n_pages,
            "Ko":       a.poids_ko,
            "Chars":    f"{a.chars_total:,}",
            "~Tokens":  f"{a.tokens_estimes:,}",
            "% txt":    f"{a.pct_pages_textuelles}%",
            "Mots URB": a.mots_urbanisme_trouves,
            "Cache":    "💾" if a.depuis_cache else "⬇️",
        })
    print(tabulate(rows, headers="keys", tablefmt="rounded_grid"))

    # Cas non utilisables
    if non_util:
        print(f"\n{'─'*70}")
        print("CAS NON UTILISABLES — détail")
        for a in non_util:
            print(f"  [{a.insee}] {a.commune} ({a.typedoc})")
            print(f"    verdict : {a.verdict}")
            print(f"    pages   : {a.n_pages}  |  chars: {a.chars_total:,}  |  moy: {a.chars_moy_par_page} chars/p")
            if a.detail:
                print(f"    détail  : {a.detail}")

    # Distribution taille tokens (pour décider stratégie LLM)
    if utilisables:
        print(f"\n{'─'*70}")
        print("DISTRIBUTION TOKENS — stratégie LLM recommandée")
        buckets = {
            "< 10k tok  (full context sans souci)":       [a for a in utilisables if a.tokens_estimes < 10_000],
            "10k-50k tok (full context Gemini)":           [a for a in utilisables if 10_000 <= a.tokens_estimes < 50_000],
            "50k-100k tok (full context Flash/Pro)":       [a for a in utilisables if 50_000 <= a.tokens_estimes < 100_000],
            "> 100k tok  (chunking recommandé)":           [a for a in utilisables if a.tokens_estimes >= 100_000],
        }
        for label, group in buckets.items():
            communes_str = ", ".join(a.commune for a in group[:5])
            if len(group) > 5:
                communes_str += f"… +{len(group)-5}"
            print(f"  {len(group):>3}x  {label}")
            if communes_str:
                print(f"         ex: {communes_str}")


if __name__ == "__main__":
    main()