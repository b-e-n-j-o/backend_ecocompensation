"""
reglement_qualite.py
Analyse qualité d'un PDF de règlement PLU.

entrée : bytes
sortie : ReglementQualite

Usage autonome :
    from reglement_qualite import analyser_qualite_reglement, ReglementQualite

Usage FastAPI :
    from reglement_qualite import router
    app.include_router(router)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import fitz  # PyMuPDF
import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# Seuils
# ---------------------------------------------------------------------------

SEUIL_CHARS_PAGE_SCANNEE = 80
SEUIL_CHARS_TOTAL_MIN    = 5_000
SEUIL_PCT_TEXTUEL        = 0.60
CHARS_PAR_TOKEN          = 4

MOTS_URBANISME = [
    "zone", "article", "destination", "construction", "hauteur",
    "emprise", "stationnement", "clôture", "recul", "implantation",
    "coefficient", "prospect", "alignement", "façade",
    "autorisation", "interdit", "applicable",
]

VERDICT_ICON = {
    "TEXTUEL":    "✅",
    "MIXTE":      "⚠️",
    "SCANNE":     "❌",
    "VIDE":       "❌",
    "TROP_COURT": "⚠️",
}


# ---------------------------------------------------------------------------
# Dataclass résultat (usage Python interne)
# ---------------------------------------------------------------------------

@dataclass
class ReglementQualite:
    verdict: str            # TEXTUEL | MIXTE | SCANNE | VIDE | TROP_COURT
    utilisable: bool
    detail: str
    # Métriques fichier
    poids_ko: int
    # Métriques extraction
    n_pages: int
    n_pages_textuelles: int
    n_pages_scannees: int
    chars_total: int
    chars_moy_par_page: int
    pct_pages_textuelles: float
    # Métriques LLM
    tokens_estimes: int
    # Structure interne PDF
    n_blocs_image: int
    n_blocs_texte: int
    mots_urbanisme_trouves: int

    @property
    def icon(self) -> str:
        return VERDICT_ICON.get(self.verdict, "?")

    def __str__(self) -> str:
        lines = [
            f"Verdict          : {self.icon} {self.verdict}",
            f"Utilisable       : {'oui' if self.utilisable else 'non'}",
            f"Pages            : {self.n_pages} ({self.n_pages_textuelles} textuelles, {self.n_pages_scannees} scannées)",
            f"Poids            : {self.poids_ko} Ko",
            f"Chars total      : {self.chars_total:,}",
            f"Chars moy/page   : {self.chars_moy_par_page:,}",
            f"% pages text     : {self.pct_pages_textuelles}%",
            f"Tokens estimés   : {self.tokens_estimes:,}",
            f"Blocs img/txt    : {self.n_blocs_image} / {self.n_blocs_texte}",
            f"Mots urbanisme   : {self.mots_urbanisme_trouves}/{len(MOTS_URBANISME)}",
        ]
        if self.detail:
            lines.append(f"Détail           : {self.detail}")
        return "\n".join(f"  {l}" for l in lines)


# ---------------------------------------------------------------------------
# Fonction principale — réutilisable partout
# ---------------------------------------------------------------------------

def analyser_qualite_reglement(pdf_bytes: bytes) -> ReglementQualite:
    """
    Analyse un PDF règlement PLU en mémoire.
    Retourne un ReglementQualite avec verdict et métriques complètes.

    Signaux combinés :
      1. chars/page      → détecte pages scannées (< 80 chars)
      2. pct pages text  → verdict global textuel/scanné/mixte
      3. chars moy/page  → détecte OCR partiel (headers seuls capturés)
      4. blocs img/txt   → signal structurel interne PDF
      5. mots urbanisme  → vérifie que le contenu est bien un règlement PLU
    """
    poids_ko = len(pdf_bytes) // 1024
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    n_pages = len(doc)

    if n_pages == 0:
        return ReglementQualite(
            verdict="VIDE", utilisable=False, detail="PDF sans pages",
            poids_ko=poids_ko, n_pages=0, n_pages_textuelles=0,
            n_pages_scannees=0, chars_total=0, chars_moy_par_page=0,
            pct_pages_textuelles=0.0, tokens_estimes=0,
            n_blocs_image=0, n_blocs_texte=0, mots_urbanisme_trouves=0,
        )

    # --- Signal 1 & 2 : extraction texte page par page ---
    chars_par_page: list[int] = []
    texte_complet = ""
    for page in doc:
        texte = page.get_text()
        chars_par_page.append(len(texte.strip()))
        texte_complet += texte

    chars_total  = sum(chars_par_page)
    chars_moy    = chars_total // n_pages
    n_scannees   = sum(1 for c in chars_par_page if c < SEUIL_CHARS_PAGE_SCANNEE)
    n_textuelles = n_pages - n_scannees
    pct_textuel  = n_textuelles / n_pages

    # --- Signal 3 : blocs image vs texte (échantillon 10 premières pages) ---
    n_blocs_image = n_blocs_texte = 0
    for page in list(doc)[:min(10, n_pages)]:
        for b in page.get_text("blocks"):
            if b[6] == 1:
                n_blocs_image += 1
            else:
                n_blocs_texte += 1

    # --- Signal 4 : mots urbanisme ---
    n_mots = sum(1 for m in MOTS_URBANISME if m in texte_complet.lower())

    # --- Verdict ---
    details: list[str] = []

    if chars_total < SEUIL_CHARS_TOTAL_MIN:
        verdict, utilisable = "VIDE", False
        details.append(f"seulement {chars_total} chars au total")

    elif pct_textuel < SEUIL_PCT_TEXTUEL:
        verdict, utilisable = "SCANNE", False
        details.append(f"{n_scannees}/{n_pages} pages sans texte extractible")

    elif pct_textuel < 0.85:
        verdict, utilisable = "MIXTE", False
        details.append(f"{n_scannees} pages scannées sur {n_pages}")

    elif chars_moy < 200:
        verdict, utilisable = "MIXTE", False
        details.append(f"moyenne {chars_moy} chars/page trop faible — possible scan avec OCR partiel")

    elif n_mots < 3:
        verdict, utilisable = "TROP_COURT", False
        details.append(f"seulement {n_mots}/{len(MOTS_URBANISME)} mots urbanisme trouvés")

    else:
        verdict, utilisable = "TEXTUEL", True

    # Signal complémentaire : dominante image (non bloquant seul, mais loggé)
    if n_blocs_image > n_blocs_texte and n_blocs_texte > 0:
        details.append(
            f"dominante image sur échantillon ({n_blocs_image} blocs img vs {n_blocs_texte} txt)"
        )
        # Si on était TEXTUEL mais dominante image → rétrograder en MIXTE
        if utilisable:
            verdict, utilisable = "MIXTE", False

    return ReglementQualite(
        verdict=verdict,
        utilisable=utilisable,
        detail=" | ".join(details),
        poids_ko=poids_ko,
        n_pages=n_pages,
        n_pages_textuelles=n_textuelles,
        n_pages_scannees=n_scannees,
        chars_total=chars_total,
        chars_moy_par_page=chars_moy,
        pct_pages_textuelles=round(pct_textuel * 100, 1),
        tokens_estimes=chars_total // CHARS_PAR_TOKEN,
        n_blocs_image=n_blocs_image,
        n_blocs_texte=n_blocs_texte,
        mots_urbanisme_trouves=n_mots,
    )


# ---------------------------------------------------------------------------
# Modèle Pydantic (usage FastAPI)
# ---------------------------------------------------------------------------

class ReglementQualiteResponse(BaseModel):
    insee: str
    reglement_url: str
    verdict: str
    utilisable: bool
    detail: str
    poids_ko: int
    n_pages: int
    n_pages_textuelles: int
    n_pages_scannees: int
    chars_total: int
    chars_moy_par_page: int
    pct_pages_textuelles: float
    tokens_estimes: int
    n_blocs_image: int
    n_blocs_texte: int
    mots_urbanisme_trouves: int


# ---------------------------------------------------------------------------
# Endpoint FastAPI
# ---------------------------------------------------------------------------

@router.post(
    "/urban-documents/{insee}/reglement-qualite",
    response_model=ReglementQualiteResponse,
    summary="Télécharge le règlement PLU et analyse sa qualité d'extraction texte",
)
async def get_reglement_qualite(insee: str, reglement_url: str):
    """
    Reçoit l'URL du règlement (fournie par GET /urban-documents/{insee}),
    télécharge le PDF côté serveur (Render) et retourne le verdict qualité.
    Aucun téléchargement côté client.
    """
    if not reglement_url:
        raise HTTPException(status_code=400, detail="reglement_url requis")

    try:
        resp = requests.get(reglement_url, timeout=60)
        resp.raise_for_status()
        pdf_bytes = resp.content
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else 502
        raise HTTPException(status_code=status, detail=f"Erreur téléchargement PDF ({status})")
    except Exception as e:
        logger.error("Erreur fetch règlement insee=%s: %s", insee, e)
        raise HTTPException(status_code=502, detail=f"Impossible de télécharger le règlement: {e}")

    if pdf_bytes[:4] != b"%PDF":
        raise HTTPException(status_code=422, detail="Fichier téléchargé non reconnu comme PDF")

    try:
        q = analyser_qualite_reglement(pdf_bytes)
    except Exception as e:
        logger.error("Erreur analyse qualité insee=%s: %s", insee, e)
        raise HTTPException(status_code=500, detail=f"Erreur analyse PDF: {e}")

    return ReglementQualiteResponse(
        insee=insee,
        reglement_url=reglement_url,
        verdict=q.verdict,
        utilisable=q.utilisable,
        detail=q.detail,
        poids_ko=q.poids_ko,
        n_pages=q.n_pages,
        n_pages_textuelles=q.n_pages_textuelles,
        n_pages_scannees=q.n_pages_scannees,
        chars_total=q.chars_total,
        chars_moy_par_page=q.chars_moy_par_page,
        pct_pages_textuelles=q.pct_pages_textuelles,
        tokens_estimes=q.tokens_estimes,
        n_blocs_image=q.n_blocs_image,
        n_blocs_texte=q.n_blocs_texte,
        mots_urbanisme_trouves=q.mots_urbanisme_trouves,
    )


# ---------------------------------------------------------------------------
# Test autonome
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import time

    GPU_API  = "https://www.geoportail-urbanisme.gouv.fr/api"
    WFS_BASE = "https://data.geopf.fr/wfs/ows"
    KEYWORDS_OK  = ["reglement", "règlement", "regl", "regt"]
    KEYWORDS_NOK = ["graphique", "plan", "zonage", "legende", "carte"]

    insee = sys.argv[1] if len(sys.argv) > 1 else "33234"
    print(f"\nTest qualité règlement PLU — INSEE {insee}")

    # Fetch pipeline
    resp = requests.get(WFS_BASE, params={
        "SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature",
        "typeNames": "wfs_du:doc_urba_com", "outputFormat": "application/json",
        "CQL_FILTER": f"insee='{insee}'", "count": "5",
    }, timeout=20)
    features = resp.json().get("features", [])
    prod = [f for f in features if f["properties"].get("gpu_status") == "production"]
    props = (prod[0] if prod else features[0])["properties"]
    details = requests.get(f"{GPU_API}/document/{props['gpu_doc_id']}/details", timeout=20).json()
    wm = details.get("writingMaterials", {})

    best_key, best_score = None, -999
    for nom in wm:
        s = sum(10 for kw in KEYWORDS_OK if kw in nom.lower())
        s -= sum(8 for kw in KEYWORDS_NOK if kw in nom.lower())
        s += 2 if nom.lower().endswith(".pdf") else 0
        if s > best_score:
            best_score, best_key = s, nom

    url = wm[best_key]
    print(f"Fichier : {best_key}")
    print(f"URL     : {url}\n")

    t0 = time.time()
    pdf_bytes = requests.get(url, timeout=60).content
    print(f"Téléchargé en {time.time()-t0:.2f}s — {len(pdf_bytes)//1024} Ko\n")

    q = analyser_qualite_reglement(pdf_bytes)
    print(q)