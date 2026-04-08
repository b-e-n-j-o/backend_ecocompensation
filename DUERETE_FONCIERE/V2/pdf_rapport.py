"""
Génération PDF — Rapport Dureté Foncière Kerelia
=================================================
Convertit le markdown Gemini en PDF professionnel via ReportLab.

Usage :
    python pdf_rapport.py --input rapport.md --output rapport.pdf
    python pdf_rapport.py --siren 892632365 --idus 86275000D0319 --output rapport.pdf
    python pdf_rapport.py --siren 892632365 --idus 86275000D0319 --raw
    (mode direct : collecte + Gemini + PDF en une seule commande)
"""

import argparse
import re
import sys
import json
import subprocess
import tempfile
from datetime import date
from pathlib import Path
from io import BytesIO

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak, KeepTogether
)
from reportlab.platypus.flowables import Flowable
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# ---------------------------------------------------------------------------
# Palette Kerelia (slate + vert écologique)
# ---------------------------------------------------------------------------

C_DARK    = colors.HexColor("#1e293b")   # slate-900
C_MID     = colors.HexColor("#475569")   # slate-600
C_LIGHT   = colors.HexColor("#94a3b8")   # slate-400
C_BG      = colors.HexColor("#f8fafc")   # slate-50
C_GREEN   = colors.HexColor("#16a34a")   # green-600
C_GREEN_L = colors.HexColor("#dcfce7")   # green-100
C_ORANGE  = colors.HexColor("#ea580c")   # orange-600
C_ORANGE_L= colors.HexColor("#ffedd5")   # orange-100
C_RED     = colors.HexColor("#dc2626")   # red-600
C_RED_L   = colors.HexColor("#fee2e2")   # red-100
C_RED_D   = colors.HexColor("#7f1d1d")   # red-950
C_WHITE   = colors.white
C_RULE    = colors.HexColor("#e2e8f0")   # slate-200

W, H = A4
MARGIN_L = 20*mm
MARGIN_R = 20*mm
MARGIN_T = 18*mm
MARGIN_B = 18*mm

# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------

def build_styles():
    base = getSampleStyleSheet()
    styles = {}

    common = dict(fontName="Helvetica", textColor=C_DARK, leading=14)

    styles["cover_title"] = ParagraphStyle("cover_title",
        fontName="Helvetica-Bold", fontSize=22, textColor=C_WHITE,
        leading=28, spaceAfter=6, alignment=TA_LEFT)

    styles["cover_sub"] = ParagraphStyle("cover_sub",
        fontName="Helvetica", fontSize=13, textColor=colors.HexColor("#cbd5e1"),
        leading=18, spaceAfter=4, alignment=TA_LEFT)

    styles["cover_meta"] = ParagraphStyle("cover_meta",
        fontName="Helvetica", fontSize=10, textColor=colors.HexColor("#94a3b8"),
        leading=14, alignment=TA_LEFT)

    styles["h1"] = ParagraphStyle("h1",
        fontName="Helvetica-Bold", fontSize=14, textColor=C_DARK,
        spaceBefore=14, spaceAfter=6, leading=18, borderPad=0)

    styles["h2"] = ParagraphStyle("h2",
        fontName="Helvetica-Bold", fontSize=11, textColor=C_MID,
        spaceBefore=10, spaceAfter=4, leading=15)

    styles["body"] = ParagraphStyle("body",
        fontName="Helvetica", fontSize=9.5, textColor=C_DARK,
        leading=14, spaceAfter=5, alignment=TA_JUSTIFY)

    styles["bullet"] = ParagraphStyle("bullet",
        fontName="Helvetica", fontSize=9.5, textColor=C_DARK,
        leading=14, spaceAfter=3, leftIndent=12, firstLineIndent=-10)

    styles["score_label"] = ParagraphStyle("score_label",
        fontName="Helvetica", fontSize=9, textColor=C_MID, leading=12)

    styles["score_value"] = ParagraphStyle("score_value",
        fontName="Helvetica-Bold", fontSize=11, textColor=C_DARK, leading=14)

    styles["table_header"] = ParagraphStyle("table_header",
        fontName="Helvetica-Bold", fontSize=9, textColor=C_WHITE, leading=12)

    styles["table_cell"] = ParagraphStyle("table_cell",
        fontName="Helvetica", fontSize=9, textColor=C_DARK, leading=12)

    styles["callout"] = ParagraphStyle("callout",
        fontName="Helvetica", fontSize=9.5, textColor=C_DARK,
        leading=14, leftIndent=10, rightIndent=10, spaceAfter=6)

    styles["footer"] = ParagraphStyle("footer",
        fontName="Helvetica", fontSize=8, textColor=C_LIGHT,
        alignment=TA_CENTER, leading=10)

    return styles


# ---------------------------------------------------------------------------
# Couleur niveau dureté
# ---------------------------------------------------------------------------

def niveau_colors(niveau_str: str):
    s = niveau_str.lower()
    if "exceptionnelle" in s:
        return C_GREEN, C_GREEN_L
    elif "faible" in s:
        return C_GREEN, C_GREEN_L
    elif "modérée" in s or "moderee" in s:
        return C_ORANGE, C_ORANGE_L
    elif "rédhibitoire" in s or "redhibitoire" in s:
        return C_RED_D, colors.HexColor("#fecaca")
    elif "forte" in s:
        return C_RED, C_RED_L
    return C_DARK, C_BG


# ---------------------------------------------------------------------------
# Parseur markdown → éléments ReportLab
# ---------------------------------------------------------------------------

def md_inline(text: str) -> str:
    """Convertit le markdown inline en XML ReportLab."""
    # Bold
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)
    # Italic
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    # Code inline
    text = re.sub(r'`(.+?)`', r'<font name="Courier" size="9">\1</font>', text)
    # Échapper les ampersands non XML (sauf ceux déjà escapés)
    text = re.sub(r'&(?!amp;|lt;|gt;|quot;|apos;)', '&amp;', text)
    return text


def parse_markdown(md_text: str, styles: dict) -> list:
    """
    Parse le markdown Gemini et retourne une liste de Flowables ReportLab.
    Gère : titres H1/H2/H3, paragraphes, listes à puces, tableaux markdown, HR.
    """
    flowables = []
    lines = md_text.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Ligne vide
        if not stripped:
            i += 1
            continue

        # HR (--- ou ***)
        if re.match(r'^[-*]{3,}$', stripped):
            flowables.append(HRFlowable(width="100%", thickness=0.5,
                                         color=C_RULE, spaceAfter=6, spaceBefore=6))
            i += 1
            continue

        # H1 (#)
        if stripped.startswith("# ") and not stripped.startswith("## "):
            text = md_inline(stripped[2:])
            flowables.append(Spacer(1, 4*mm))
            flowables.append(Paragraph(text, styles["h1"]))
            flowables.append(HRFlowable(width="100%", thickness=1.5,
                                         color=C_GREEN, spaceAfter=4))
            i += 1
            continue

        # H2 (##)
        if stripped.startswith("## ") and not stripped.startswith("### "):
            text = md_inline(stripped[3:])
            flowables.append(Spacer(1, 2*mm))
            flowables.append(Paragraph(text, styles["h2"]))
            i += 1
            continue

        # H3 (###)
        if stripped.startswith("### "):
            text = md_inline(stripped[4:])
            flowables.append(Paragraph(f"<b>{text}</b>", styles["body"]))
            i += 1
            continue

        # Tableau markdown (| col | col |)
        if stripped.startswith("|"):
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1
            flowables.extend(_parse_md_table(table_lines, styles))
            continue

        # Liste à puces (* ou - ou •)
        if re.match(r'^[\*\-•]\s+', stripped):
            bullet_items = []
            while i < len(lines) and re.match(r'^[\*\-•]\s+', lines[i].strip()):
                item = re.sub(r'^[\*\-•]\s+', '', lines[i].strip())
                bullet_items.append(md_inline(item))
                i += 1
            for item in bullet_items:
                flowables.append(Paragraph(f"• {item}", styles["bullet"]))
            flowables.append(Spacer(1, 2))
            continue

        # Paragraphe normal
        flowables.append(Paragraph(md_inline(stripped), styles["body"]))
        i += 1

    return flowables


def _parse_md_table(lines: list, styles: dict) -> list:
    """Parse un tableau markdown en Table ReportLab."""
    rows = []
    for line in lines:
        if re.match(r'^\|[-:| ]+\|$', line):
            continue  # ligne séparatrice
        cells = [c.strip() for c in line.strip("|").split("|")]
        rows.append(cells)

    if not rows:
        return []

    # Convertir en Paragraphs
    rl_rows = []
    for r_idx, row in enumerate(rows):
        rl_row = []
        for cell in row:
            style = styles["table_header"] if r_idx == 0 else styles["table_cell"]
            rl_row.append(Paragraph(md_inline(cell), style))
        rl_rows.append(rl_row)

    col_w = (W - MARGIN_L - MARGIN_R) / max(len(rows[0]), 1)
    col_widths = [col_w] * len(rows[0])

    t = Table(rl_rows, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, 0),  C_DARK),
        ("TEXTCOLOR",    (0, 0), (-1, 0),  C_WHITE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [C_WHITE, C_BG]),
        ("GRID",         (0, 0), (-1, -1), 0.4, C_RULE),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",   (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 5),
        ("LEFTPADDING",  (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    return [Spacer(1, 3*mm), t, Spacer(1, 3*mm)]


# ---------------------------------------------------------------------------
# Page de couverture
# ---------------------------------------------------------------------------

def build_cover(
    denomination: str,
    siren: str,
    score: int,
    niveau: str,
    date_str: str,
    styles: dict,
    sirene_info: dict | None = None,
) -> list:
    """Génère la page de couverture."""
    fg, bg = niveau_colors(niveau)
    flowables = []

    # Bandeau couleur pleine page simulé via Table
    cover_data = [[
        Paragraph("KERELIA", ParagraphStyle("logo",
            fontName="Helvetica-Bold", fontSize=11, textColor=colors.HexColor("#94a3b8"), leading=14)),
    ]]
    cover_table = Table(cover_data, colWidths=[W - MARGIN_L - MARGIN_R])
    cover_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), C_DARK),
        ("TOPPADDING",    (0, 0), (-1, -1), 8*mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6*mm),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6*mm),
    ]))
    flowables.append(cover_table)
    flowables.append(Spacer(1, 6*mm))

    # Titre principal
    flowables.append(Paragraph("Rapport de Dureté Foncière",
        ParagraphStyle("main_title", fontName="Helvetica-Bold", fontSize=26,
                       textColor=C_DARK, leading=30, spaceAfter=2)))
    flowables.append(Paragraph(denomination,
        ParagraphStyle("pm_title", fontName="Helvetica", fontSize=16,
                       textColor=C_MID, leading=20, spaceAfter=8)))
    flowables.append(HRFlowable(width="100%", thickness=2, color=C_GREEN, spaceAfter=8))

    # Score (splitLongWords=0 : évite « 78/10 » + « 0 » sur deux lignes)
    score_txt = f"{score}/100"
    score_data = [[
        Paragraph("SCORE FINAL", ParagraphStyle("sl", fontName="Helvetica-Bold",
            fontSize=10, textColor=C_WHITE, leading=14)),
        Paragraph(score_txt, ParagraphStyle("sv", fontName="Helvetica-Bold",
            fontSize=28, textColor=C_WHITE, leading=32, alignment=TA_CENTER,
            splitLongWords=0)),
        Paragraph(niveau.upper(), ParagraphStyle("sn", fontName="Helvetica-Bold",
            fontSize=11, textColor=C_WHITE, leading=14, alignment=TA_RIGHT)),
    ]]
    usable_w = W - MARGIN_L - MARGIN_R
    score_table = Table(score_data,
        colWidths=[usable_w * 0.30,
                   usable_w * 0.32,
                   usable_w * 0.38])
    score_table.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), fg),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 10*mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10*mm),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8*mm),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 8*mm),
        ("ROUNDEDCORNERS", [4]),
    ]))
    flowables.append(score_table)
    flowables.append(Spacer(1, 8*mm))

    # Méta
    meta = [
        ["SIREN", siren],
        ["Date d'analyse", date_str],
        ["Contexte", "Acquisition SNCRR — Compensation écologique"],
        ["Source", "Annuaire Entreprises · BODACC · DVF · RPG IGN"],
    ]
    if sirene_info:
        tranche = sirene_info.get("trancheEffectifsUniteLegale")
        tranche_label = sirene_info.get("tranche_label")
        annee_eff = sirene_info.get("anneeEffectifsUniteLegale")
        nb_per = sirene_info.get("nombrePeriodesUniteLegale")
        eff = tranche_label or tranche or "NC"
        if annee_eff:
            eff = f"{eff} ({annee_eff})"
        if nb_per:
            meta.append(["Sirene (effectifs)", eff])
            meta.append(["Sirene (périodes)", str(nb_per)])
        else:
            meta.append(["Sirene (effectifs)", eff])

    for label, value in meta:
        flowables.append(Paragraph(
            f'<font color="#94a3b8">{label} :</font>  <b>{value}</b>',
            ParagraphStyle("meta_line", fontName="Helvetica", fontSize=9.5,
                           textColor=C_DARK, leading=14, spaceAfter=2)
        ))

    flowables.append(Spacer(1, 6*mm))
    flowables.append(HRFlowable(width="100%", thickness=0.5, color=C_RULE))
    flowables.append(Spacer(1, 4*mm))
    flowables.append(Paragraph(
        "Ce rapport est généré automatiquement par le pipeline de scoring dureté foncière Kerelia. "
        "Il est destiné à l'usage interne de l'équipe de prospection foncière.",
        ParagraphStyle("disclaimer", fontName="Helvetica", fontSize=8,
                       textColor=C_LIGHT, leading=11, alignment=TA_JUSTIFY)
    ))
    flowables.append(PageBreak())
    return flowables


# ---------------------------------------------------------------------------
# Extraction score depuis markdown
# ---------------------------------------------------------------------------

def extraire_score_markdown(md: str) -> tuple[int, str]:
    """
    Extrait le score final et le niveau depuis le markdown Gemini.
    Cherche le tableau de score final.
    """
    # Pattern : | **SCORE FINAL** | **75/100** |
    m = re.search(r'\*\*SCORE FINAL\*\*[^\|]*\|[^\|]*\*\*(\d+)/100\*\*', md)
    if m:
        score = int(m.group(1))
    else:
        # Fallback : cherche "Score : XX/100" ou "XX/100"
        m2 = re.search(r'SCORE FINAL.*?(\d+)/100', md, re.IGNORECASE)
        score = int(m2.group(1)) if m2 else 0

    # Niveau de dureté
    niveau = "Dureté modérée"
    for pattern in [r'Dureté\s+(\w+)', r'(\w+\s+\w*dureté\w*)', r'Dureté\s+forte', r'rédhibitoire']:
        m3 = re.search(r'(?:SCORE FINAL|niveau)[^\n]*\n?[^\n]*?((?:Opportunité exceptionnelle|Dureté\s+(?:faible|modérée|forte|rédhibitoire)))',
                       md, re.IGNORECASE)
        if m3:
            niveau = m3.group(1)
            break

    # Extraction directe du niveau depuis ligne synthèse
    m4 = re.search(r'\*\*(?:SCORE FINAL|Score final)[^*]*\*\*.*?—\s*([^\*\n]+)', md)
    if m4:
        niveau = m4.group(1).strip()

    return score, niveau


def extraire_denomination(md: str) -> tuple[str, str]:
    """Extrait dénomination et SIREN depuis le titre H2 du rapport."""
    m = re.search(r'^## (.+?) — SIREN (\d+)', md, re.MULTILINE)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m2 = re.search(r'^## (.+)', md, re.MULTILINE)
    return (m2.group(1).strip() if m2 else "Personne Morale"), "N/A"


# ---------------------------------------------------------------------------
# En-tête et pied de page
# ---------------------------------------------------------------------------

class HeaderFooter:
    def __init__(self, denomination: str, score: int, niveau: str):
        self.denomination = denomination
        self.score = score
        self.niveau = niveau

    def __call__(self, canvas, doc):
        canvas.saveState()
        w, h = A4

        # Header
        canvas.setFillColor(C_DARK)
        canvas.rect(0, h - 14*mm, w, 14*mm, fill=1, stroke=0)
        canvas.setFillColor(C_WHITE)
        canvas.setFont("Helvetica-Bold", 9)
        canvas.drawString(MARGIN_L, h - 9*mm, "KERELIA — Rapport Dureté Foncière")
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#94a3b8"))
        canvas.drawRightString(w - MARGIN_R, h - 9*mm, self.denomination[:60])

        # Footer
        canvas.setFillColor(C_RULE)
        canvas.rect(0, 0, w, 10*mm, fill=1, stroke=0)
        canvas.setFillColor(C_MID)
        canvas.setFont("Helvetica", 7.5)
        canvas.drawString(MARGIN_L, 4*mm,
            f"Score : {self.score}/100 — {self.niveau} | Usage interne Kerelia | {date.today().isoformat()}")
        canvas.setFont("Helvetica-Bold", 8)
        canvas.drawRightString(w - MARGIN_R, 4*mm, f"Page {doc.page}")

        canvas.restoreState()


# ---------------------------------------------------------------------------
# Génération PDF principale
# ---------------------------------------------------------------------------

def corriger_coherence_score(md: str) -> str:
    """
    Garantit que le score dans la SYNTHÈSE EXÉCUTIVE correspond exactement
    au score du tableau section 8 (source de vérité).
    Remplace la ligne **SCORE FINAL : XX/100 — ...** dans la synthèse.
    """
    import re

    # 1. Extraire le score du tableau (source de vérité)
    m_tableau = re.search(
        r'\*\*SCORE FINAL\*\*\s*\|\s*\*\*(\d+)/100\*\*',
        md
    )
    if not m_tableau:
        return md  # pas de tableau trouvé, on ne touche à rien

    score_ref = int(m_tableau.group(1))

    # Niveau correspondant
    if score_ref <= 20:   niveau = "Opportunité exceptionnelle"
    elif score_ref <= 40: niveau = "Dureté faible"
    elif score_ref <= 60: niveau = "Dureté modérée"
    elif score_ref <= 80: niveau = "Dureté forte"
    else:                 niveau = "Dureté rédhibitoire"

    # 2. Remplacer la ligne score dans la synthèse exécutive
    # Pattern : **SCORE FINAL : XX/100 — Quelque chose**
    md_corrige = re.sub(
        r'\*\*SCORE FINAL\s*:\s*\d+/100\s*[—\-][^\*]*\*\*',
        f'**SCORE FINAL : {score_ref}/100 — {niveau}**',
        md
    )

    return md_corrige


def _extraire_axes_markdown(md: str) -> dict:
    """
    Extrait les scores d'axes depuis le tableau section 8 du markdown Gemini.
    Retourne {axe1, axe2, axe3, axe4} (valeurs int ou None).
    """
    def _get(pat: str):
        m = re.search(pat, md, re.IGNORECASE)
        return int(m.group(1)) if m else None

    return {
        "axe1": _get(r"\|\s*Axe\s*1[^\|]*\|\s*(\d+)\s*/\s*40\s*\|"),
        "axe2": _get(r"\|\s*Axe\s*2[^\|]*\|\s*(\d+)\s*/\s*25\s*\|"),
        "axe3": _get(r"\|\s*Axe\s*3[^\|]*\|\s*(\d+)\s*/\s*20\s*\|"),
        "axe4": _get(r"\|\s*Axe\s*4[^\|]*\|\s*(\d+)\s*/\s*15\s*\|"),
    }


def generer_pdf(md_text: str, output_path: str, raw_payload: dict | None = None) -> str:
    """
    Convertit un rapport markdown Gemini en PDF professionnel.
    Retourne le chemin du fichier généré.
    """
    styles = build_styles()

    # Correction cohérence score (tableau = source de vérité)
    md_text = corriger_coherence_score(md_text)

    # Extraction métadonnées
    score, niveau      = extraire_score_markdown(md_text)
    denomination, siren = extraire_denomination(md_text)
    date_str           = date.today().strftime("%d/%m/%Y")

    print(f"  Génération PDF : {denomination} | Score {score}/100 | {niveau}")

    # Enrichissement optionnel via Sirene INSEE (si clé présente)
    sirene_info = (raw_payload or {}).get("sirene") if isinstance(raw_payload, dict) else {}
    if not sirene_info:
        try:
            from sirene import fetch_sirene
            sirene_info = fetch_sirene(siren) if siren and siren.isdigit() else {}
        except Exception:
            sirene_info = {}

    # Génère d'abord le PDF principal dans un fichier temporaire.
    tmp_main = tempfile.NamedTemporaryFile(prefix="rapport_durete_main_", suffix=".pdf", delete=False)
    tmp_main.close()

    hf = HeaderFooter(denomination, score, niveau)

    # Flowables
    story = []

    # Page de couverture (sans header/footer)
    story.extend(build_cover(denomination, siren, score, niveau, date_str, styles, sirene_info=sirene_info))

    # Contenu du rapport
    story.extend(parse_markdown(md_text, styles))

    # Build
    doc = SimpleDocTemplate(
        tmp_main.name,
        pagesize=A4,
        leftMargin=MARGIN_L, rightMargin=MARGIN_R,
        topMargin=MARGIN_T + 14*mm,  # espace header
        bottomMargin=MARGIN_B + 10*mm,  # espace footer
        title=f"Dureté Foncière — {denomination}",
        author="Kerelia",
        subject="Scoring Dureté Foncière SNCRR",
    )
    doc.build(story, onFirstPage=lambda c, d: None,  # pas de header sur couverture
              onLaterPages=hf)

    # Si on a le JSON brut du pipeline, générer la carte d'identité en page 2 (après la couverture).
    if raw_payload:
        tmp_id = tempfile.NamedTemporaryFile(prefix="carte_identite_", suffix=".pdf", delete=False)
        tmp_id.close()
        try:
            # Le chart RPG empilé doit être injecté EN AMONT (dans le raw_payload)
            # sans refaire de fetch réseau ici, pour éviter doublons.

            from rapport_identite import extraire_donnees, generer_carte_identite
            axes = _extraire_axes_markdown(md_text)
            data = extraire_donnees(
                raw_payload,
                score_final=score,
                axe1=axes.get("axe1"),
                axe2=axes.get("axe2"),
                axe3=axes.get("axe3"),
                axe4=axes.get("axe4"),
            )
            generer_carte_identite(data, tmp_id.name)

            # Ordre : page 1 = couverture du rapport, page 2 = carte d'identité, puis le reste
            np_r = subprocess.run(
                ["qpdf", "--show-npages", tmp_main.name],
                check=True,
                capture_output=True,
                text=True,
            )
            n_main = int((np_r.stdout or "").strip() or "1")
            if n_main <= 1:
                subprocess.run(
                    ["qpdf", "--empty", "--pages", tmp_main.name, "1-1", tmp_id.name, "--", output_path],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            else:
                subprocess.run(
                    [
                        "qpdf",
                        "--empty",
                        "--pages",
                        tmp_main.name,
                        "1-1",
                        tmp_id.name,
                        tmp_main.name,
                        f"2-{n_main}",
                        "--",
                        output_path,
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            print(f"  PDF généré : {output_path}")
            return output_path
        except subprocess.CalledProcessError as e:
            err = (e.stderr or "").strip()
            print(f"  ⚠️  Fusion qpdf échouée, fallback sans carte : {err[:400]}")
        except Exception as e:
            print(f"  ⚠️  Carte d'identité indisponible, fallback sans carte : {e}")

    # Fallback : sans carte, on publie le PDF principal.
    Path(tmp_main.name).replace(output_path)
    print(f"  PDF généré : {output_path}")
    return output_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Génération PDF rapport dureté foncière")
    parser.add_argument("--input",  help="Fichier markdown (.md) en entrée")
    parser.add_argument("--output", default="rapport_durete.pdf", help="Fichier PDF de sortie")
    # Mode direct (sans fichier intermédiaire)
    parser.add_argument("--siren",  help="SIREN (mode direct sans fichier md)")
    parser.add_argument("--idus",   nargs="+", help="IDUs (mode direct)")
    parser.add_argument("--no-rpg", action="store_true")
    parser.add_argument("--raw", action="store_true", help="Mode brut : renvoie JSON brut et ne génère pas de PDF")
    args = parser.parse_args()

    if args.input:
        md_text = Path(args.input).read_text(encoding="utf-8")
    elif args.siren and args.idus:
        if args.raw:
            sys.path.insert(0, str(Path(__file__).parent))
            from rapport import collecter_donnees_raw
            print(json.dumps(collecter_donnees_raw(args.siren, args.idus), ensure_ascii=False, indent=2))
            sys.exit(0)
        # Mode direct : collecte unique → Gemini → PDF (avec carte identité)
        sys.path.insert(0, str(Path(__file__).parent))
        from rapport import collecter_donnees_brutes, construire_prompt, generer_rapport_gemini
        from rpg_chart import plot_rpg_history, reset_color_cache
        print("Génération rapport Gemini ...")
        contexte = collecter_donnees_brutes(args.siren, args.idus, avec_rpg=not args.no_rpg)
        prompt = construire_prompt(contexte)
        md_text = generer_rapport_gemini(prompt)

        # Construire le payload carte identité à partir des données déjà collectées (zéro refetch)
        raw_payload = {
            "siren": args.siren,
            "idus": args.idus,
            "date_analyse": contexte.get("date_analyse"),
            "annuaire_raw": contexte.get("annuaire_raw", {}),
            "sirene": contexte.get("sirene", {}),
            "bodacc": contexte.get("bodacc", {}),
            "parcelles": contexte.get("parcelles", []),
            "urba": contexte.get("urba") or {},
        }

        # Générer le chart RPG empilé à partir du by_year_raw déjà collecté
        try:
            parcelles = raw_payload.get("parcelles") or []
            if parcelles:
                idu0 = parcelles[0].get("idu")
                rpg0 = (parcelles[0].get("rpg") or {})
                by_year = rpg0.get("by_year_raw") or {}
                if by_year:
                    reset_color_cache()
                    chart_data = {}
                    for year, d in by_year.items():
                        if not isinstance(d, dict):
                            continue
                        if d.get("status") != "agricole":
                            chart_data[year] = {"status": d.get("status")}
                            continue
                        chart_data[year] = {
                            "status": "agricole",
                            "total": float(d.get("total_m2") or 0),
                            "cultures": {k: float(v) for k, v in (d.get("cultures") or {}).items()},
                            "labels": d.get("labels") or {},
                        }
                    tmp_png = tempfile.NamedTemporaryFile(prefix="rpg_chart_", suffix=".png", delete=False)
                    tmp_png.close()
                    plot_rpg_history(
                        chart_data,
                        output_path=tmp_png.name,
                        title="Proportions des cultures (RPG)",
                        parcelle_id=str(idu0 or ""),
                    )
                    raw_payload["rpg_chart_png"] = Path(tmp_png.name).read_bytes()
        except Exception:
            pass
    else:
        parser.error("Fournir --input ou (--siren + --idus)")

    generer_pdf(md_text, args.output, raw_payload=locals().get("raw_payload"))
    print(f"Terminé : {args.output}")