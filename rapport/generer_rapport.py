"""
Orchestrateur : Génération du rapport PDF complet d'éco-compensation
--------------------------------------------------------------------
Assemble dans l'ordre :
  Page 1  — Page de garde
  Page 2  — Table des matières
  Page 3  — I. Contexte de l'étude  (carte contexte foncier + AOI)
  Page 4  — II. Présentation de l'équipe
  Pages 5-6 — III. Méthodologie (tableaux scoring statiques)
  Pages 7-8 — IV. Résultats
               4.1 Tableau des parcelles + carte parcelles + scores
               4.2 Dureté foncière (placeholder carte)
               4.3 Synthèse
  Page 9  — Annexe sources

Input (RapportInput dataclass) :
  - shp_path          : chemin SHP parcelles (EPSG:2154)
  - foncier_id        : UUID ecocompensation.foncier
  - aoi_id            : UUID ecocompensation.aoi
  - meta              : dict projet (maitre_ouvrage, commune, etc.)
  - texte_contexte    : paragraphe libre rédigé par le bureau d'études
  - output_pdf        : chemin de sortie

Usage :
    python generer_rapport.py
Ou depuis FastAPI :
    from generer_rapport import generer_rapport_complet, RapportInput
    generer_rapport_complet(RapportInput(...))
"""

import os
import sys
import tempfile
import warnings
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import List, Optional

warnings.filterwarnings("ignore")

import geopandas as gpd
import matplotlib
matplotlib.use("Agg")


def pick_col(gdf: gpd.GeoDataFrame, new: str, legacy: str) -> str:
    """
    Colonnes GeoDataFrame classement : schéma d'export actuel (`new`)
    ou anciennes colonnes (`legacy`), pour PDF / cartes.
    """
    if new in gdf.columns:
        return new
    if legacy in gdf.columns:
        return legacy
    raise KeyError(
        f"Colonne attendue absente du GeoDataFrame : '{new}' ou '{legacy}' "
        f"(colonnes : {list(gdf.columns)})"
    )


from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable, Image as RLImage, KeepTogether, PageBreak,
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)
from reportlab.platypus.flowables import Flowable
from resultats_eco import append_section_resultats_eco, generer_image_carte_eco
from resultats_durete import append_sections_resultats_durete, generer_image_carte_durete

# ── Chemin racine pour imports locaux ───────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_LOGOS_DIR = _HERE / "logos"
_DEFAULT_LOGO_ECO = _LOGOS_DIR / "logo_eco_compensation.png"
_DEFAULT_LOGO_KERELIA = _LOGOS_DIR / "logo_kerelia.png"

# ─────────────────────────────────────────────────────────────────────────────
# PALETTE
# ─────────────────────────────────────────────────────────────────────────────
VERT_FONCE = "#1B4332"
VERT_MOYEN = "#2D6A4F"
VERT_CLAIR = "#95D5B2"
VERT_PALE  = "#D8F3DC"
GRIS_TEXTE = "#2D3436"
BLANC      = colors.white
NOIR       = colors.black
PAGE_SIZE  = landscape(A4)


# ─────────────────────────────────────────────────────────────────────────────
# INPUT DATACLASS
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class RapportInput:
    # ── Données pipeline ──────────────────────────────────────────────────
    shp_path: str                   # chemin SHP parcelles (EPSG:2154)
    foncier_id: str                 # UUID ecocompensation.foncier
    aoi_id: str                     # UUID ecocompensation.aoi

    # ── Métadonnées projet ────────────────────────────────────────────────
    maitre_ouvrage: str             = "—"
    commune: str                    = "—"
    type_projet: str                = "—"
    besoin_compensatoire_ha: float  = 0.0
    especes_cibles: List[str]       = field(default_factory=list)
    bureau_etudes: str              = "—"
    date_rapport: str               = ""    # libre ; si vide → date du jour JJ/MM/YYYY

    # ── Textes rédigés par le bureau d'études ─────────────────────────────
    texte_contexte: str             = ""    # paragraphe section I
    texte_complement_methode: str   = ""    # paragraphe complémentaire section III

    # ── Assets (page de garde) — si None : rapport/logos/*.jpg|png par défaut ─
    logo_eco_path: Optional[str]    = None  # ex. .jpg / .png
    logo_kerelia_path: Optional[str] = None

    # ── Sortie ────────────────────────────────────────────────────────────
    output_pdf: str                 = "/tmp/rapport_ecocompensation.pdf"

    # ── Options carte ─────────────────────────────────────────────────────
    buffer_carte_m: int             = 600   # marge autour des parcelles
    buffer_contexte_m: int          = 1500  # marge carte contexte


def _date_rapport_affichage(inp: RapportInput) -> str:
    """Texte date rapport : valeur saisie, ou date du jour au format JJ/MM/YYYY."""
    from datetime import date as _date
    return inp.date_rapport.strip() if inp.date_rapport.strip() else _date.today().strftime("%d/%m/%Y")


# ─────────────────────────────────────────────────────────────────────────────
# STYLES
# ─────────────────────────────────────────────────────────────────────────────
def build_styles() -> dict:
    base = getSampleStyleSheet()
    custom = {
        "h1": ParagraphStyle("h1", fontSize=10, fontName="Helvetica-Bold",
            textColor=BLANC, leading=15),
        "h2": ParagraphStyle("h2", fontSize=9.5, fontName="Helvetica-Bold",
            textColor=HexColor(VERT_FONCE), leading=14,
            spaceBefore=10, spaceAfter=5),
        "body": ParagraphStyle("body", fontSize=8.5, fontName="Helvetica",
            textColor=HexColor(GRIS_TEXTE), alignment=TA_JUSTIFY,
            leading=13, spaceBefore=3, spaceAfter=3),
        "bullet": ParagraphStyle("bullet", fontSize=8.5, fontName="Helvetica",
            textColor=HexColor(GRIS_TEXTE), alignment=TA_JUSTIFY,
            leading=12, leftIndent=12, spaceBefore=2, spaceAfter=2),
        "th": ParagraphStyle("th", fontSize=7.5, fontName="Helvetica-Bold",
            textColor=BLANC, alignment=TA_CENTER, leading=10),
        "td": ParagraphStyle("td", fontSize=7, fontName="Helvetica",
            textColor=HexColor(GRIS_TEXTE), alignment=TA_CENTER, leading=9),
        "td_left": ParagraphStyle("td_left", fontSize=7, fontName="Helvetica",
            textColor=HexColor(GRIS_TEXTE), alignment=TA_LEFT, leading=9),
        "legende": ParagraphStyle("legende", fontSize=7.5,
            fontName="Helvetica-Oblique", textColor=HexColor("#666666"),
            alignment=TA_CENTER, spaceBefore=3, spaceAfter=8),
        "note": ParagraphStyle("note", fontSize=7.5,
            fontName="Helvetica-Oblique", textColor=HexColor("#555555"),
            alignment=TA_JUSTIFY, leading=11, leftIndent=8, spaceBefore=3),
        "toc": ParagraphStyle("toc", fontSize=9, fontName="Helvetica",
            textColor=HexColor(GRIS_TEXTE), leading=14),
        "toc_bold": ParagraphStyle("toc_bold", fontSize=9,
            fontName="Helvetica-Bold", textColor=HexColor(VERT_FONCE), leading=14),
    }
    return {**{k: base[k] for k in base.byName}, **custom}


# ─────────────────────────────────────────────────────────────────────────────
# FLOWABLES UTILITAIRES
# ─────────────────────────────────────────────────────────────────────────────
class BandeTitre(Flowable):
    """Bande pleine largeur colorée pour les titres de section."""
    def __init__(self, text: str, width: float,
                 couleur=VERT_FONCE, height: float = 22):
        super().__init__()
        self._text   = text
        self._width  = width
        self._couleur = couleur
        self._height = height

    def wrap(self, aW, aH):
        return self._width, self._height

    def draw(self):
        c = self.canv
        c.setFillColor(HexColor(self._couleur))
        c.rect(0, 0, self._width, self._height, fill=1, stroke=0)
        c.setFillColor(BLANC)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(10, 7, self._text)


def placeholder_carte(W: float, H: float, label: str) -> Table:
    """Zone grise placeholder pour une carte non encore générée."""
    tbl = Table([[Paragraph(
        f"[ {label} ]",
        ParagraphStyle("ph", fontSize=10, fontName="Helvetica",
                       textColor=HexColor("#AAAAAA"), alignment=TA_CENTER)
    )]], colWidths=[W], rowHeights=[H])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), HexColor("#F0F0F0")),
        ("BOX",        (0,0), (-1,-1), 1, HexColor("#CCCCCC")),
        ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
    ]))
    return tbl


def _rl_logo_from_path(path: str, max_w: float, max_h: float) -> Optional[RLImage]:
    """Image ReportLab (PNG, JPEG, etc.) redimensionnée pour tenir dans max_w × max_h (points)."""
    if not path or not os.path.isfile(path):
        return None
    try:
        from PIL import Image as PILImage
        with PILImage.open(path) as im:
            pw, ph = im.size
        if pw <= 0 or ph <= 0:
            return None
        ratio = pw / ph
        w = max_w
        h = w / ratio
        if h > max_h:
            h = max_h
            w = h * ratio
        return RLImage(path, width=w, height=h)
    except Exception:
        return None


def _logos_page_garde(inp: RapportInput, W: float):
    """Une ligne ECO + KERELIA sous les encarts contact, ou None si aucun fichier valide."""
    eco_p = inp.logo_eco_path or str(_DEFAULT_LOGO_ECO)
    ker_p = inp.logo_kerelia_path or str(_DEFAULT_LOGO_KERELIA)
    max_h = 2.2 * cm
    max_w = W * 0.42
    img_e = _rl_logo_from_path(eco_p, max_w, max_h)
    img_k = _rl_logo_from_path(ker_p, max_w, max_h)
    if img_e is None and img_k is None:
        return None
    cell_e = img_e if img_e is not None else Spacer(1, max_h * 0.3)
    cell_k = img_k if img_k is not None else Spacer(1, max_h * 0.3)
    tbl = Table([[cell_e, cell_k]], colWidths=[W * 0.5, W * 0.5], hAlign="CENTER")
    tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return tbl


def _buf_to_rl_image(buf: BytesIO, width: float, max_height: float) -> RLImage:
    from PIL import Image as PILImage
    buf.seek(0)
    pil = PILImage.open(buf)
    pw, ph = pil.size
    ratio = ph / pw
    h = min(width * ratio, max_height)
    w = h / ratio
    buf.seek(0)
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.write(buf.read())
    tmp.flush()
    return RLImage(tmp.name, width=w, height=h)


# ─────────────────────────────────────────────────────────────────────────────
# EN-TÊTE + PIED DE PAGE
# ─────────────────────────────────────────────────────────────────────────────
class HeaderFooter:
    def __init__(self, inp: RapportInput):
        self.inp = inp

    def __call__(self, canvas, doc):
        if doc.page == 1:
            return   # pas de header/footer sur la page de garde
        canvas.saveState()
        W, H = doc.pagesize

        # Header
        canvas.setFillColor(HexColor(VERT_FONCE))
        canvas.rect(0, H - 1.1*cm, W, 1.1*cm, fill=1, stroke=0)
        canvas.setFillColor(BLANC)
        canvas.setFont("Helvetica-Bold", 7.5)
        canvas.drawString(1.5*cm, H - 0.72*cm,
            "Etude foncière de compensation écologique")
        canvas.setFont("Helvetica", 7)
        canvas.drawRightString(W - 1.5*cm, H - 0.72*cm,
            "KERELIA × ECO-COMPENSATION")

        # Footer
        canvas.setStrokeColor(HexColor(VERT_CLAIR))
        canvas.setLineWidth(0.5)
        canvas.line(1.5*cm, 1.4*cm, W - 1.5*cm, 1.4*cm)
        canvas.setFillColor(HexColor("#888888"))
        canvas.setFont("Helvetica", 7)
        canvas.drawCentredString(W/2, 0.8*cm, f"Page {doc.page}")
        canvas.drawString(1.5*cm, 0.8*cm, _date_rapport_affichage(self.inp))
        canvas.drawRightString(W - 1.5*cm, 0.8*cm, "Rapport pré-identification éco-compensation")
        canvas.restoreState()


# ─────────────────────────────────────────────────────────────────────────────
# SECTIONS DU RAPPORT
# ─────────────────────────────────────────────────────────────────────────────

def _page_de_garde(story, styles, W, inp: RapportInput):
    date_str = _date_rapport_affichage(inp)

    # Header vert
    hd = Table([[
        Paragraph("Etude foncière de compensation écologique",
            ParagraphStyle("hdr", fontSize=9, fontName="Helvetica-Bold",
                           textColor=HexColor("#A8D5BA"), alignment=TA_CENTER, leading=12)),
    ]], colWidths=[W])
    hd.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), HexColor(VERT_FONCE)),
        ("TOPPADDING",    (0,0), (-1,-1), 10),
        ("BOTTOMPADDING", (0,0), (-1,-1), 10),
        ("LEFTPADDING",   (0,0), (0,-1), 14),
        ("RIGHTPADDING",  (-1,0),(-1,-1),14),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
    ]))
    story.append(hd)
    story.append(Spacer(1, 1.5 * cm))

    # Zone photo retirée pour laisser la place aux encarts entreprises.
    story.append(Spacer(1, 0.2*cm))

    # Titre
    story.append(Paragraph(
        "Pré-identification du foncier mobilisable pour l'animation foncière",
        ParagraphStyle("tg", fontSize=16, fontName="Helvetica-Bold",
                       textColor=HexColor(VERT_FONCE), alignment=TA_CENTER, leading=22)))
    story.append(Paragraph(
        "au titre de la compensation « espèce protégée »",
        ParagraphStyle("tg2", fontSize=13, fontName="Helvetica",
                       textColor=HexColor(VERT_MOYEN),
                       alignment=TA_CENTER, leading=18, spaceBefore=4)))
    story.append(Spacer(1, 0.8*cm))

    # Encart KPIs
    # Ajustement manuel demandé pour ce rapport.
    especes_str = "Tarier pâtre, Cisticole des joncs"
    kpi = Table([
        [Paragraph("Besoin compensatoire", ParagraphStyle("k_l", fontSize=8,
             fontName="Helvetica", textColor=HexColor("#555"), alignment=TA_CENTER)),
         Paragraph("Espèces cibles", ParagraphStyle("k_l2", fontSize=8,
             fontName="Helvetica", textColor=HexColor("#555"), alignment=TA_CENTER)),
         Paragraph("Rapport établi le", ParagraphStyle("k_l3", fontSize=8,
             fontName="Helvetica", textColor=HexColor("#555"), alignment=TA_CENTER))],
        [Paragraph("<b>6,8 ha</b>",
             ParagraphStyle("k_v", fontSize=14, fontName="Helvetica-Bold",
                            textColor=HexColor(VERT_FONCE), alignment=TA_CENTER)),
         Paragraph(f"<b>{especes_str}</b>",
             ParagraphStyle("k_v2", fontSize=9, fontName="Helvetica-Bold",
                            textColor=HexColor(VERT_FONCE),
                            alignment=TA_CENTER, leading=12)),
         Paragraph(f"<b>{date_str}</b>",
             ParagraphStyle("k_v3", fontSize=11, fontName="Helvetica-Bold",
                            textColor=HexColor(VERT_FONCE), alignment=TA_CENTER))],
    ], colWidths=[W/3]*3)
    kpi.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), HexColor(VERT_PALE)),
        ("BOX",           (0,0), (-1,-1), 1, HexColor(VERT_CLAIR)),
        ("LINEAFTER",     (0,0), (1,-1), 1, HexColor(VERT_CLAIR)),
        ("TOPPADDING",    (0,0), (-1,-1), 8),
        ("BOTTOMPADDING", (0,0), (-1,-1), 8),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
    ]))
    story.append(kpi)
    story.append(Spacer(1, 0.8*cm))

    # Contacts
    def col_contact(nom, adresse, cp_ville, tel, email, web, logo_path=None):
        logo = _rl_logo_from_path(logo_path, max_w=(W/2 - 2.0*cm), max_h=1.1*cm)
        rows = [[Paragraph(t, ParagraphStyle("ci", fontSize=8,
                fontName="Helvetica", textColor=HexColor(GRIS_TEXTE), leading=11))]
                for t in [adresse, cp_ville, f"Tél. : {tel}", email, web]]
        if logo is not None:
            rows.insert(0, [logo])
        rows.insert(0, [Paragraph(nom, ParagraphStyle("cn", fontSize=10,
                fontName="Helvetica-Bold", textColor=HexColor(VERT_FONCE), leading=14))])
        t = Table(rows, colWidths=[W/2 - 1.2*cm])
        t.setStyle(TableStyle([
            ("BOX",           (0,0), (-1,-1), 1, HexColor(VERT_CLAIR)),
            ("BACKGROUND",    (0,0), (-1,-1), HexColor(VERT_PALE)),
            ("TOPPADDING",    (0,0), (-1,-1), 3),
            ("BOTTOMPADDING", (0,0), (-1,-1), 3),
            ("LEFTPADDING",   (0,0), (-1,-1), 10),
            ("RIGHTPADDING",  (0,0), (-1,-1), 8),
        ]))
        return t

    eco_logo_path = inp.logo_eco_path or str(_DEFAULT_LOGO_ECO)
    kerelia_logo_path = inp.logo_kerelia_path or str(_DEFAULT_LOGO_KERELIA)

    contacts = Table([[
        col_contact("ECO-COMPENSATION", "5 C Rue de Vivey", "33380 MIOS",
                    "07 68 88 14 19", "contact@eco-compensation.fr",
                    "eco-compensation.fr/", eco_logo_path),
        col_contact("KERELIA",
                    "60 rue Augustinot",
                    "33360 LATRESNE", "06 XX XX XX XX",
                    "contact@kerelia.fr", "kerelia.fr", kerelia_logo_path),
    ]], colWidths=[W/2 - 0.3*cm, W/2 - 0.3*cm], hAlign="CENTER")
    contacts.setStyle(TableStyle([
        ("VALIGN",       (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING",  (0,0), (-1,-1), 4),
        ("RIGHTPADDING", (0,0), (-1,-1), 4),
    ]))
    story.append(contacts)
    story.append(PageBreak())


def _table_des_matieres(story, styles, W):
    story.append(BandeTitre("Table des matières", W))
    story.append(Spacer(1, 0.5*cm))
    toc = [
        ("I.",   "Contexte de l'étude",                           "3"),
        ("II.",  "Présentation de l'équipe projet",               "4"),
        ("III.", "Méthodologie",                                   "5"),
        ("IV.",  "Résultats",                                      "7"),
        ("",     "4.1  Analyse territoriale et fonctionnelle",     "7"),
        ("",     "4.2  Analyse de la dureté foncière",             "8"),
        ("",     "4.3  Synthèse et scoring",                       "9"),
    ]
    for num, titre, page in toc:
        indent = 0 if num else 16
        bold = bool(num)
        fn = "Helvetica-Bold" if bold else "Helvetica"
        data = [[
            Paragraph(num,   ParagraphStyle("tn", fontSize=9, fontName=fn,
                             textColor=HexColor(VERT_FONCE), leading=13)),
            Paragraph(titre, ParagraphStyle("tt", fontSize=9, fontName=fn,
                             textColor=HexColor(GRIS_TEXTE),
                             leading=13, leftIndent=indent)),
            Paragraph(page,  ParagraphStyle("tp", fontSize=9, fontName="Helvetica",
                             textColor=HexColor(GRIS_TEXTE),
                             alignment=TA_RIGHT, leading=13)),
        ]]
        t = Table(data, colWidths=[0.8*cm, W - 2*cm, 1.2*cm])
        t.setStyle(TableStyle([
            ("VALIGN",     (0,0), (-1,-1), "BOTTOM"),
            ("BOTTOMPADDING",(0,0),(-1,-1), 5),
            ("LINEBELOW",  (1,0), (2,0), 0.3, HexColor("#DDDDDD")),
        ]))
        story.append(t)
    story.append(PageBreak())


def _section_contexte(story, styles, W, inp: RapportInput,
                      img_contexte_buf: Optional[BytesIO]):
    story.append(BandeTitre("I.  Contexte de l'étude", W))
    story.append(Spacer(1, 0.4*cm))

    # Texte rédigé par le bureau d'études (ou texte par défaut)
    texte = inp.texte_contexte or (
        "Sur la commune de <b>La Brède</b>, la démarche de compensation écologique "
        "nécessite l'identification de foncier mobilisable en cohérence avec les enjeux "
        "faunistiques et les contraintes territoriales. Le besoin compensatoire est fixé à "
        "<b>6,8 ha</b> et l'étude est réalisée par le bureau d'études <b>Simethis</b>."
    )
    story.append(Paragraph(texte, styles["body"]))
    story.append(Spacer(1, 0.3*cm))

    # Carte contexte (foncier + AOI)
    if img_contexte_buf is not None:
        carte_h = PAGE_SIZE[1] - 2.4*cm - 1.5*cm - 2*cm - 3*cm
        story.append(_buf_to_rl_image(img_contexte_buf, width=W, max_height=carte_h))
    else:
        story.append(placeholder_carte(W, 7*cm,
            "CARTE DE LOCALISATION DU SITE PROJET"))

    story.append(Paragraph(
        "Fig. 1. Localisation du site projet et zone d'étude",
        styles["legende"]))
    story.append(PageBreak())


def _section_equipe(story, styles, W):
    story.append(BandeTitre("II.  Présentation de l'équipe projet", W))
    story.append(Spacer(1, 0.4*cm))
    story.append(Paragraph(
        "La mission a été conduite conjointement par <b>ECO-COMPENSATION</b> et "
        "<b>KERELIA</b>, deux équipes complémentaires mobilisant des expertises "
        "pluridisciplinaires pour assurer la réussite des mesures compensatoires.",
        styles["body"]))
    story.append(Spacer(1, 0.4*cm))

    def bloc(nom, desc, bullets, couleur):
        titre_t = Table([[Paragraph(nom,
            ParagraphStyle("bt", fontSize=10, fontName="Helvetica-Bold",
                           textColor=BLANC, leading=14))]],
            colWidths=[W])
        titre_t.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (-1,-1), HexColor(couleur)),
            ("TOPPADDING",    (0,0), (-1,-1), 7),
            ("BOTTOMPADDING", (0,0), (-1,-1), 7),
            ("LEFTPADDING",   (0,0), (-1,-1), 12),
        ]))
        body_rows = [[Paragraph(desc, styles["body"])]] + \
                    [[Paragraph(f"• {b}", styles["bullet"])] for b in bullets]
        body_t = Table(body_rows, colWidths=[W])
        body_t.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (-1,-1), HexColor("#F7F7F7")),
            ("TOPPADDING",    (0,0), (-1,-1), 4),
            ("BOTTOMPADDING", (0,0), (-1,-1), 3),
            ("LEFTPADDING",   (0,0), (-1,-1), 12),
            ("RIGHTPADDING",  (0,0), (-1,-1), 12),
            ("LINEBELOW",     (0,-1), (-1,-1), 1, HexColor(couleur)),
        ]))
        return [titre_t, body_t, Spacer(1, 0.4*cm)]

    for el in bloc("ECO-COMPENSATION",
        "ECO-COMPENSATION apporte une expertise <b>globale en écologie et gestion "
        "environnementale</b>, avec une expérience reconnue dans :",
        ["Conception et mise en œuvre de mesures compensatoires",
         "Réalisation d'inventaires faunistiques et floristiques",
         "Veille réglementaire en lien avec les mesures compensatoires"],
        VERT_FONCE):
        story.append(el)

    for el in bloc("KERELIA",
        "KERELIA est spécialisée dans la <b>gestion et l'animation foncière</b> "
        "des projets environnementaux, avec des compétences dans :",
        ["Traitement et automatisation des données environnementales",
         "Accompagnement juridique et administratif des démarches d'acquisition",
         "Coordination avec les acteurs locaux et les propriétaires fonciers"],
        VERT_MOYEN):
        story.append(el)

    story.append(PageBreak())


def _section_methodologie(story, styles, W):
    story.append(BandeTitre("III.  Méthodologie", W))
    story.append(Spacer(1, 0.4*cm))
    story.append(Paragraph(
        "La méthodologie repose sur une <b>analyse territoriale et fonctionnelle "
        "multicritères</b>, fondée sur l'exploitation de données géographiques de "
        "référence et l'application d'un processus de sélection spatiale adapté aux "
        "enjeux écologiques et aux exigences réglementaires de la compensation. "
        "Après une phase d'analyse géomatique et d'occupation du sol, les parcelles "
        "résultantes sont soumises à un score de pertinence écologique en vue des besoins "
        "de compensation d'espèces animales du projet.",
        styles["body"]))
    story.append(Spacer(1, 0.3*cm))

    def tableau_critere(titre, data_rows, col_w):
        story.append(Paragraph(titre, styles["h2"]))
        header = [Paragraph(h, styles["th"]) for h in data_rows[0]]
        rows_rl = [header]
        COULEURS = [HexColor("#2D6A4F"), HexColor("#F4A261"), HexColor("#B7B7B7")]
        for i, row in enumerate(data_rows[1:]):
            rows_rl.append([Paragraph(c, styles["td"] if j > 0 else
                ParagraphStyle("niv", fontSize=8, fontName="Helvetica-Bold",
                    textColor=BLANC, alignment=TA_CENTER))
                for j, c in enumerate(row)])
        t = Table(rows_rl, colWidths=col_w)
        bg_rows = []
        for i in range(len(data_rows) - 1):
            bg_rows.append(("BACKGROUND", (0, i+1), (0, i+1), COULEURS[min(i, 2)]))
            bg = HexColor(VERT_PALE) if i % 2 == 0 else BLANC
            bg_rows.append(("BACKGROUND", (1, i+1), (-1, i+1), bg))
        t.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (-1,0), HexColor(VERT_FONCE)),
            ("GRID",          (0,0), (-1,-1), 0.4, HexColor("#CCCCCC")),
            ("TOPPADDING",    (0,0), (-1,-1), 5),
            ("BOTTOMPADDING", (0,0), (-1,-1), 5),
            ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
            *bg_rows,
        ]))
        story.append(t)
        story.append(Spacer(1, 0.4*cm))

    cw3 = [W*0.18, W*0.52, W*0.30]
    tableau_critere("Critère 1 — Proximité géographique", [
        ["Niveau", "Critère", "Valeur"],
        ["Fort",   "Distance < 3 km",                  "3"],
        ["Moyen",  "Distance comprise entre 3 et 7 km", "2"],
        ["Faible", "Distance > 7 km",                  "1"],
    ], cw3)

    tableau_critere("Critère 2 — Données de présence de l'espèce cible", [
        ["Niveau", "Critère", "Valeur"],
        ["Fort",   "Observation directement sur la parcelle",                         "+3"],
        ["Moyen",  "Observation dans l'environnement proche (max. 500 m)",            "+2"],
        ["Faible", "Observation dans le périmètre de dispersion de l'espèce (1 000 m)", "+1"],
        ["Très faible", "Aucune observation dans le périmètre considéré",             "0"],
    ], cw3)

    tableau_critere("Interprétation du score global", [
        ["Score", "Niveau de priorité", "Interprétation"],
        ["5 à 6", "Priorité forte",       "Pertinence écologique et spatiale élevée"],
        ["3 à 4", "Priorité intermédiaire", "Potentiellement mobilisable — à confirmer"],
        ["2",     "Priorité faible",       "Peu prioritaire au regard des critères"],
    ], [W*0.12, W*0.28, W*0.60])

    story.append(Paragraph("Critère complémentaire — Dureté foncière "
        "(à titre expérimental, hors propriété privée)", styles["h2"]))
    story.append(Paragraph(
        "Ce paramètre évalue la faisabilité juridique et opérationnelle de "
        "mobilisation des parcelles par les personnes morales. L'analyse est "
        "réalisée à titre expérimental et ne s'applique pas aux propriétaires "
        "privés en raison des limitations des données disponibles. Le score "
        "repose sur une analyse de croisement de données issues notamment de "
        "l'Annuaire des Entreprises, du BODACC, du RPG, de data.gouv.fr et de la DVF.",
        styles["body"]))
    story.append(PageBreak())


def _section_resultats(story, styles, W,
                       img_eco_buf: Optional[BytesIO],
                       img_durete_buf: Optional[BytesIO],
                       img_parcelles_buf: Optional[BytesIO],
                       gdf: Optional[gpd.GeoDataFrame]):
    story.append(BandeTitre("IV.  Résultats", W))
    story.append(Spacer(1, 0.3*cm))
    append_section_resultats_eco(
        story=story,
        styles=styles,
        width_pt=W,
        gdf=gdf,
        img_eco_buf=img_eco_buf,
        buf_to_rl_image=_buf_to_rl_image,
        placeholder_carte=placeholder_carte,
    )
    story.append(PageBreak())
    append_sections_resultats_durete(
        story=story,
        styles=styles,
        width_pt=W,
        gdf=gdf,
        img_durete_buf=img_durete_buf,
        img_parcelles_buf=img_parcelles_buf,
        buf_to_rl_image=_buf_to_rl_image,
        placeholder_carte=placeholder_carte,
    )


def _section_annexe(story, styles, W):
    story.append(BandeTitre("Annexe — Sources de données", W))
    story.append(Spacer(1, 0.4*cm))
    story.append(Paragraph(
        "L'ensemble des données sources mobilisées pour l'analyse territoriale "
        "et fonctionnelle multicritère est listé ci-dessous :",
        styles["body"]))
    story.append(Spacer(1, 0.3*cm))

    sources = [
        ("FAUNA",                     "Données d'observations des espèces animales", "FAUNA"),
        ("BD TOPO® IGN",              "Base topographique nationale",                "IGN — open data"),
        ("CESBIO — Occupation du sol","Cartographie haute résolution",               "CESBIO / Theia"),
        ("RPG",                       "Registre Parcellaire Graphique (PAC)",        "ASP / IGN"),
        ("BRGM",                      "Données zones humides",                        "BRGM"),
        ("Géoportail",                "Données zones protégées par la réglementation d'urbanisme (ex. Natura 2000)", "IGN / Géoportail"),
        ("DVF",                       "Demandes de Valeurs Foncières",               "DGFiP — open data"),
        ("BODACC",                    "Bulletin officiel annonces civiles",          "DILA"),
        ("SIRENE",                    "Répertoire national des entreprises",         "INSEE — open data"),
        ("data.gouv.fr",              "Données de parcelles personnes morales",      "data.gouv.fr"),
        ("Annuaire des Entreprises",  "Informations légales et financières",         "API Entreprises"),
        ("Cadastre — Parcelles",      "Plan cadastral informatisé",                  "DGFiP / IGN"),
    ]
    header = [Paragraph(h, styles["th"]) for h in ["Source", "Description", "Producteur"]]
    rows   = [header] + [
        [Paragraph(f"<b>{n}</b>", styles["td_left"]),
         Paragraph(d, styles["td_left"]),
         Paragraph(p, styles["td_left"])]
        for n, d, p in sources
    ]
    t = Table(rows, colWidths=[W*0.28, W*0.42, W*0.30])
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0), HexColor(VERT_FONCE)),
        ("GRID",          (0,0), (-1,-1), 0.3, HexColor("#CCCCCC")),
        ("TOPPADDING",    (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ("VALIGN",        (0,0), (-1,-1), "TOP"),
        ("ROWBACKGROUNDS",(0,1), (-1,-1), [HexColor(VERT_PALE), BLANC]),
    ]))
    story.append(t)


# ─────────────────────────────────────────────────────────────────────────────
# ORCHESTRATEUR PRINCIPAL
# ─────────────────────────────────────────────────────────────────────────────
def generer_rapport_complet(inp: RapportInput) -> str:
    """
    Point d'entrée principal — génère le PDF complet.
    Retourne le chemin du PDF généré.
    """
    print(f"\n{'='*60}")
    print(f"  GÉNÉRATION RAPPORT ÉCOCOMPENSATION")
    print(f"  Projet : {inp.commune} — {inp.maitre_ouvrage}")
    print(f"{'='*60}\n")

    # ── 1. Chargement SHP parcelles ─────────────────────────────────────────
    print("📂 Chargement parcelles (GPKG attributs complets ou SHP)...")
    gdf = None
    if inp.shp_path and os.path.exists(inp.shp_path):
        gpkg = Path(inp.shp_path).with_suffix(".gpkg")
        if gpkg.is_file():
            gdf = gpd.read_file(gpkg)
            print(f"   → {len(gdf)} parcelles (GeoPackage, dont txt_dure complet)")
        else:
            gdf = gpd.read_file(inp.shp_path)
            print(f"   → {len(gdf)} parcelles (Shapefile)")
    else:
        print(f"   ⚠ SHP introuvable : {inp.shp_path}")

    # ── 2. Chargement foncier + AOI ─────────────────────────────────────────
    print("📂 Chargement foncier + AOI...")
    sys.path.insert(0, str(_HERE))
    from carte_contexte.carte_contexte import charger_foncier_et_aoi
    foncier_gdf, aoi_gdf = charger_foncier_et_aoi(inp.foncier_id, inp.aoi_id)

    # ── 3. Génération image carte contexte ──────────────────────────────────
    img_contexte_buf = None
    if foncier_gdf is not None or aoi_gdf is not None:
        print("🗺️  Génération carte contexte...")
        from carte_contexte.carte_contexte import generer_image_contexte
        try:
            img_contexte_buf = generer_image_contexte(
                foncier_gdf, aoi_gdf, inp.buffer_contexte_m)
        except Exception as e:
            print(f"   ⚠ Carte contexte impossible : {e}")

    # ── 4. Génération image carte parcelles ─────────────────────────────────
    img_parcelles_buf = None
    img_eco_buf = None
    img_durete_buf = None
    if gdf is not None:
        print("🗺️  Génération carte parcelles + scores...")
        from carte_parcelles.carte_parcelles import generer_image_carte, generer_image_legende
        try:
            img_parcelles_buf = generer_image_carte(
                gdf, inp.type_projet, site_geom=None,
                buffer_m=inp.buffer_carte_m, foncier_projet=foncier_gdf)
            img_eco_buf = generer_image_carte_eco(
                gdf_2154=gdf, foncier_projet=foncier_gdf, buffer_m=inp.buffer_carte_m)
            img_durete_buf = generer_image_carte_durete(
                gdf_2154=gdf, foncier_projet=foncier_gdf, buffer_m=inp.buffer_carte_m)
        except Exception as e:
            print(f"   ⚠ Carte parcelles impossible : {e}")

    # ── 5. Assemblage PDF ───────────────────────────────────────────────────
    print(f"📄 Assemblage PDF → {inp.output_pdf}")
    os.makedirs(os.path.dirname(os.path.abspath(inp.output_pdf)), exist_ok=True)

    W = PAGE_SIZE[0] - 3*cm
    styles = build_styles()
    story  = []

    meta = {
        "maitre_ouvrage":          inp.maitre_ouvrage,
        "commune":                 inp.commune,
        "type_projet":             inp.type_projet,
        "besoin_compensatoire_ha": inp.besoin_compensatoire_ha,
        "especes_cibles":          inp.especes_cibles,
        "bureau_etudes":           inp.bureau_etudes,
    }

    _page_de_garde(story, styles, W, inp)
    _table_des_matieres(story, styles, W)
    _section_contexte(story, styles, W, inp, img_contexte_buf)
    _section_equipe(story, styles, W)
    _section_methodologie(story, styles, W)
    _section_resultats(story, styles, W, img_eco_buf, img_durete_buf, img_parcelles_buf, gdf)
    _section_annexe(story, styles, W)

    hf = HeaderFooter(inp)
    doc = SimpleDocTemplate(
        inp.output_pdf, pagesize=PAGE_SIZE,
        rightMargin=1.5*cm, leftMargin=1.5*cm,
        topMargin=1.5*cm, bottomMargin=1.8*cm,
        title="Rapport Pré-identification foncière — Éco-compensation",
        author="KERELIA × ECO-COMPENSATION",
    )
    doc.build(story, onFirstPage=hf, onLaterPages=hf)

    print(f"\n✅ Rapport généré : {inp.output_pdf}")
    return inp.output_pdf


# ─────────────────────────────────────────────────────────────────────────────
# MAIN standalone
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(_HERE.parent.parent.parent / ".env")

    # SHP parcelles (EPSG:2154) — dossier à la racine de backend/rapport/
    SHP_PATH = str(_HERE / "parcelles_6643c835" / "parcelles.shp")

    OUTPUT_PDF = str(_HERE / "rapport_ecocompensation.pdf")

    inp = RapportInput(
        shp_path               = SHP_PATH,
        foncier_id             = "4cb71955-82c7-4a3c-a3dc-8c61a29080e5",
        aoi_id                 = "7ba1e7b4-078b-4e47-99ae-6c97a4754d07",
        maitre_ouvrage         = "Groupe QENERGY",
        commune                = "La Brède (33)",
        type_projet            = "Centrale agrivoltaïque",
        besoin_compensatoire_ha= 6.4,
        especes_cibles         = ["Cisticole des joncs", "Tarier pâtre"],
        bureau_etudes          = "SIMETHIS",
        texte_contexte         = "",   # laissé vide → texte auto
        output_pdf             = OUTPUT_PDF,
        buffer_carte_m         = 600,
        buffer_contexte_m      = 1500,
    )

    generer_rapport_complet(inp)