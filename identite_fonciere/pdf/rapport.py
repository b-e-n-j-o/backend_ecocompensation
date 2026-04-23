"""
pdf/rapport.py
Génération du rapport PDF d'identité foncière V0 (France entière, données WFS).
Calqué sur rapport_identite_fonciere.py de Latresne, simplifié :
- pas de PostGIS, pas de SSE, pas d'annexe réglementation markdown
- même palette Kerelia verte/orange
- structure : page de garde → page PLU → corps par article
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape as xml_escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.platypus import (
    KeepTogether, PageBreak,
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from .sections.section_prescriptions import PRESCRIPTION_TABLE_NAMES

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Palette Kerelia
# ---------------------------------------------------------------------------
C_GREEN = colors.HexColor("#2D6A4F")
C_LIGHT = colors.HexColor("#52B788")
C_BG = colors.HexColor("#F0F7F4")
C_BORDER = colors.HexColor("#B7D9C8")

TYPE_COLORS = {
    "servitude": colors.HexColor("#1B4F72"),
    "prescription": colors.HexColor("#784212"),
    "information": colors.HexColor("#145A32"),
    "information_ou_prescription": colors.HexColor("#4A235A"),
}
TYPE_LABELS = {
    "servitude": "Servitude",
    "prescription": "Prescription",
    "information": "Information",
    "information_ou_prescription": "Info / Prescription",
}
ARTICLE_LABELS = {
    "3": "Zonage PLU",
    "4": "Servitudes d'utilité publique",
    "5": "Risques et nuisances",
    "6": "Réseaux et équipements",
    "7": "Informations et Prescriptions",
    "8": "Autres",
    "9": "Droits de préemption",
    "10": "Subdivision fiscale",
}

# Codes typeinf GPU → libellés lisibles pour les couches info_surf / info_lin / info_pct
# Source : nomenclature GPU / CNIG standard PLU
TYPEINF_LABELS: Dict[str, str] = {
    "01": "Espace boisé classé (EBC)",
    "02": "Élément remarquable du paysage",
    "03": "Zone archéologique",
    "04": "Droit de Préemption Urbain (DPU)",
    "05": "Zone d'aménagement concerté (ZAC)",
    "06": "Zone d'aménagement différé (ZAD)",
    "07": "Périmètre de sauvegarde du commerce",
    "08": "Patrimoine bâti identifié",
    "09": "Zone humide",
    "10": "Trame verte et bleue",
    "11": "Alignement d'arbres",
    "12": "Recul par rapport à la voie",
    "13": "Marge de recul",
    "99": "Autre information",
}

def _typeinf_label(code: str) -> str:
    """Retourne le libellé lisible d'un code typeinf GPU."""
    if not code:
        return ""
    return TYPEINF_LABELS.get(str(code).strip().zfill(2), f"Type {code}")

# Tables couches informations (info_surf / info_lin / info_pct)
_INFO_TABLES = frozenset({"info_surf", "info_lin", "info_pct"})

# Logo Kerelia (optionnel)
_LOGO_CANDIDATES = [
    Path(__file__).parents[3] / "CUA" / "logos" / "logo_kerelia.png",
    Path(__file__).parent / "logo_kerelia.png",
]


def _find_logo() -> Optional[Path]:
    for p in _LOGO_CANDIDATES:
        if p.exists():
            return p
    return None


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------

def _styles() -> Dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    N = base["Normal"]
    return {
        "title": ParagraphStyle("KTitle", parent=N, fontSize=20,
                                textColor=C_GREEN, fontName="Helvetica-Bold",
                                spaceAfter=4, leading=24),
        "subtitle": ParagraphStyle("KSub", parent=N, fontSize=11,
                                   textColor=colors.HexColor("#555555"),
                                   fontName="Helvetica", spaceAfter=2, leading=14),
        "kicker": ParagraphStyle("KKicker", parent=N, fontSize=9,
                                 textColor=colors.HexColor("#5c7268"),
                                 fontName="Helvetica", spaceAfter=4, leading=11),
        "article_header": ParagraphStyle("KArtHdr", parent=N, fontSize=13,
                                         textColor=colors.white,
                                         fontName="Helvetica-Bold", leading=16),
        "layer_name": ParagraphStyle("KLayerName", parent=N, fontSize=10,
                                     textColor=colors.HexColor("#1a1a1a"),
                                     fontName="Helvetica-Bold", spaceAfter=2, leading=13),
        "type_badge": ParagraphStyle("KBadge", parent=N, fontSize=8,
                                     textColor=colors.white,
                                     fontName="Helvetica-Bold", leading=10),
        "attr_key": ParagraphStyle("KAttrKey", parent=N, fontSize=8,
                                   textColor=colors.HexColor("#666666"),
                                   fontName="Helvetica-Bold", leading=11),
        "attr_val": ParagraphStyle("KAttrVal", parent=N, fontSize=9,
                                   textColor=colors.HexColor("#222222"),
                                   fontName="Helvetica", leading=12),
        "meta_label": ParagraphStyle("KMetaLabel", parent=N, fontSize=9,
                                     textColor=colors.HexColor("#888888"),
                                     fontName="Helvetica-Bold", leading=12),
        "meta_value": ParagraphStyle("KMetaVal", parent=N, fontSize=9,
                                     textColor=colors.HexColor("#222222"),
                                     fontName="Helvetica", leading=12),
        "cover_label": ParagraphStyle("KCovLbl", parent=N, fontSize=9,
                                      textColor=colors.HexColor("#5a5a5a"),
                                      fontName="Helvetica-Bold", leading=12),
        "cover_value": ParagraphStyle("KCovVal", parent=N, fontSize=9.5,
                                      textColor=colors.HexColor("#1a1a1a"),
                                      fontName="Helvetica", leading=12),
        "cover_zonage": ParagraphStyle("KCovZon", parent=N, fontSize=10,
                                       textColor=colors.HexColor("#1a4d36"),
                                       fontName="Helvetica-Bold", leading=13),
        "no_intersection": ParagraphStyle("KNoInt", parent=N, fontSize=9,
                                          textColor=colors.HexColor("#888888"),
                                          fontName="Helvetica-Oblique", leading=12),
        "plu_kicker": ParagraphStyle("KPluKicker", parent=N, fontSize=8,
                                     textColor=colors.HexColor("#6b7f72"),
                                     fontName="Helvetica-Bold", spaceAfter=6, leading=10),
        "plu_title": ParagraphStyle("KPluTitle", parent=N, fontSize=17,
                                    textColor=colors.HexColor("#1e4d2f"),
                                    fontName="Helvetica-Bold", spaceAfter=8, leading=22),
        "plu_tbl_hdr": ParagraphStyle("KPluTblHdr", parent=N, fontSize=8.5,
                                      textColor=colors.HexColor("#1e4d2f"),
                                      fontName="Helvetica-Bold", leading=11),
        "plu_tbl_cell": ParagraphStyle("KPluTblCell", parent=N, fontSize=8,
                                       textColor=colors.HexColor("#2d3748"),
                                       fontName="Helvetica", leading=10),
    }


# ---------------------------------------------------------------------------
# Page decorator (header + footer)
# ---------------------------------------------------------------------------

class _PageDeco:
    def __init__(self, logo: Optional[Path], commune: str, date_str: str):
        self.logo = logo
        self.commune = commune
        self.date_str = date_str

    def __call__(self, canvas, doc):
        canvas.saveState()
        w, h = A4
        canvas.setFillColor(C_GREEN)
        canvas.rect(0, h - 12 * mm, w, 12 * mm, fill=1, stroke=0)
        if self.logo and self.logo.exists():
            try:
                canvas.drawImage(str(self.logo), 8 * mm, h - 10 * mm,
                                 height=8 * mm, preserveAspectRatio=True, mask="auto")
            except Exception:
                pass
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica", 7)
        canvas.drawRightString(w - 8 * mm, h - 5 * mm,
                               f"{self.commune}  |  {self.date_str}")
        canvas.setFillColor(colors.HexColor("#AAAAAA"))
        canvas.setFont("Helvetica", 7)
        canvas.drawCentredString(w / 2, 8 * mm,
                                 f"Carte d'identité foncière – {self.commune} – Page {doc.page}")
        canvas.setStrokeColor(colors.HexColor("#DDDDDD"))
        canvas.line(15 * mm, 13 * mm, w - 15 * mm, 13 * mm)
        canvas.restoreState()


# ---------------------------------------------------------------------------
# Corps : couche + article
# ---------------------------------------------------------------------------

def _layer_block(layer: Dict[str, Any], st: Dict, inner_w: float) -> List:
    flow: List = []
    display_name = layer.get("display_name") or layer.get("table") or "Couche"
    layer_type = layer.get("type") or ""
    elements = layer.get("elements") or []

    type_color = TYPE_COLORS.get(layer_type, colors.HexColor("#555555"))
    type_lbl = TYPE_LABELS.get(layer_type, layer_type.capitalize() or "—")

    badge = Paragraph(f'<font color="white"><b>{type_lbl}</b></font>', st["type_badge"])
    name_p = Paragraph(f"<b>{xml_escape(display_name)}</b>", st["layer_name"])
    badge_tbl = Table([[badge]],
                      colWidths=[len(type_lbl) * 5.5 + 12], rowHeights=[14])
    badge_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), type_color),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    hdr = Table([[name_p, badge_tbl]],
                colWidths=[inner_w * 0.72, inner_w * 0.28], rowHeights=[18])
    hdr.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (0, 0), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
    ]))

    # Tableau attributs
    if not elements:
        attr_block = Paragraph(
            "Intersection détectée (données attributaires non disponibles)",
            st["no_intersection"],
        )
    elif layer.get("_plu_all_zonages_below_min_pct"):
        attr_block = Paragraph(
            "Les zonages PLU représentent chacun moins de 1 % de la surface d'étude.",
            st["no_intersection"],
        )
    else:
        # Pour les couches info_* : on enrichit typeinf avec son libellé lisible
        is_info = layer.get("table", "") in _INFO_TABLES
        if is_info:
            enriched: List[Dict[str, Any]] = []
            for el in elements:
                row = dict(el)
                ti = row.get("typeinf", "")
                if ti:
                    lbl = _typeinf_label(ti)
                    if lbl:
                        row["typeinf"] = f"{ti} — {lbl}"
                enriched.append(row)
            elements = enriched

        # Colonnes = toutes les clés présentes dans les éléments
        all_keys: List[str] = []
        for el in elements:
            for k in el.keys():
                if k not in all_keys:
                    all_keys.append(k)

        # Libellés de colonnes lisibles pour les couches info_*
        COL_HEADERS: Dict[str, str] = {
            "libelle": "Libellé",
            "txt":     "Référence",
            "typeinf": "Type d'information",
            "typepsc": "Type prescription",
            "suptype": "Code SUP",
            "nomsuplitt": "Désignation",
        }

        if not all_keys:
            attr_block = Paragraph("Intersection détectée.", st["no_intersection"])
        else:
            hdr_cells = [
                Paragraph(f"<b>{xml_escape(COL_HEADERS.get(k, k))}</b>", st["attr_key"])
                for k in all_keys
            ]
            attr_rows = [hdr_cells]
            for el in elements:
                attr_rows.append([
                    Paragraph(xml_escape(str(el.get(k, "—"))), st["attr_val"])
                    for k in all_keys
                ])
            col_w = inner_w / max(len(all_keys), 1)
            repeat = 1 if len(attr_rows) > 1 else 0
            # Largeurs adaptées si 3 colonnes info (libelle large, txt court, typeinf large)
            if is_info and len(all_keys) == 3:
                col_widths = [inner_w * 0.38, inner_w * 0.14, inner_w * 0.48]
            else:
                col_widths = [col_w] * len(all_keys)
            attr_block = Table(attr_rows, colWidths=col_widths, repeatRows=repeat)
            attr_block.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8F5EE")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CCDDCC")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                 [colors.white, colors.HexColor("#F7FCF9")]),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 8),
            ]))

    content = Table(
        [[hdr], [Spacer(1, 4)], [attr_block]],
        colWidths=[inner_w],
    )
    content.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.8, C_BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
    ]))
    flow.append(content)
    flow.append(Spacer(1, 6))
    return flow


def _article_section(
    article_key: str,
    layers: List[Dict],
    st: Dict,
    page_w: float,
) -> List:
    flow: List = []
    label = ARTICLE_LABELS.get(article_key, f"Article {article_key}")
    inner_w = page_w - 4 * cm

    hdr_p = Paragraph(label, st["article_header"])
    hdr_tbl = Table([[hdr_p]], colWidths=[page_w - 4 * cm], rowHeights=[22])
    hdr_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), C_GREEN),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
    ]))
    n = len(layers)
    cnt_p = Paragraph(
        f'<font color="#2D6A4F"><b>{n} couche{"s" if n > 1 else ""} intersectée{"s" if n > 1 else ""}</b></font>',
        ParagraphStyle("cnt", fontSize=8, fontName="Helvetica-Bold", leading=10),
    )
    flow.append(Spacer(1, 10))
    flow.append(KeepTogether([hdr_tbl, Spacer(1, 4), cnt_p, Spacer(1, 6)]))

    for layer in layers:
        flow.extend(_layer_block(layer, st, inner_w))
    return flow


def generate_rapport_pdf(
    result: Dict[str, Any],
    output_dir: str = ".",
    logo_path: Optional[str] = None,
    filename: Optional[str] = None,
    plu_map_png: Optional[str] = None,
    plu_result: Optional[Dict[str, Any]] = None,
    servitudes_map_png: Optional[str] = None,
    servitudes_result: Optional[Dict[str, Any]] = None,
    prescriptions_map_png: Optional[str] = None,
    prescriptions_result: Optional[Dict[str, Any]] = None,
    dpu_map_png: Optional[str] = None,
    dpu_result: Optional[Dict[str, Any]] = None,
    subdivision_map_png: Optional[str] = None,
    subdivision_result: Optional[Dict[str, Any]] = None,
    intro_map_png: Optional[str] = None,
) -> str:
    """
    Génère le rapport PDF V0 France entière.

    Args:
        result             : dict avec keys intersections, commune, insee, parcelle,
                             parcelles_cadastrales, parcelles_uf_detail, surface_uf_m2
        output_dir         : répertoire de sortie
        logo_path          : chemin logo Kerelia (optionnel)
        filename           : nom du fichier PDF (auto si None)
        plu_map_png        : chemin PNG carte PLU (optionnel)
        plu_result         : dict PLU enrichi (répartition UF + parcelles)
        servitudes_map_png : chemin PNG carte servitudes SUP (optionnel)
        servitudes_result  : dict servitudes enrichi (attributs + répartition UF + parcelles)
        prescriptions_map_png: chemin PNG carte prescriptions (optionnel)
        prescriptions_result : dict prescriptions (attributs surf/lin/pct)
        dpu_map_png        : chemin PNG carte DPU (optionnel, toujours généré)
        dpu_result         : dict retourné par compute_dpu_result() (optionnel)
        subdivision_map_png: chemin PNG carte subdivision fiscale (optionnel)
        subdivision_result : dict retourné par compute_subdivision_result() (optionnel)

    Returns:
        Chemin absolu du PDF généré.
    """
    intersections: List[Dict] = result.get("intersections", [])
    commune = result.get("commune", "Commune inconnue")
    date_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    date_file = datetime.now().strftime("%Y%m%d_%H%M")

    logo: Optional[Path] = None
    if logo_path:
        logo = Path(logo_path)
        if not logo.exists():
            logo = _find_logo()
    else:
        logo = _find_logo()

    if not filename:
        safe = commune.replace(" ", "_").lower()
        filename = f"rapport_identite_fonciere_{safe}_{date_file}.pdf"

    output_path = Path(output_dir) / filename
    output_path.parent.mkdir(parents=True, exist_ok=True)

    st = _styles()
    page_w, page_h = A4
    tw = page_w - 4 * cm

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=2.2 * cm, bottomMargin=1.8 * cm,
        title=f"Rapport identité foncière – {commune}",
        author="Kerelia",
    )
    decorator = _PageDeco(logo, commune, date_str)
    story: List = []

    # Page de garde
    from .sections.section_intro import build_intro_page_flowables
    story.extend(build_intro_page_flowables(result=result, table_width=tw, intro_map_png=intro_map_png))

    # Page PLU dédiée (toujours présente)
    if plu_result is not None:
        from .sections.section_plu import build_plu_page_flowables
        story.append(PageBreak())
        story.extend(build_plu_page_flowables(plu_map_png, plu_result, tw))

    # Page servitudes dédiée (toujours présente)
    if servitudes_result is not None:
        from .sections.section_servitudes import build_servitudes_page_flowables
        story.append(PageBreak())
        story.extend(build_servitudes_page_flowables(servitudes_map_png, servitudes_result, tw))

    if prescriptions_result is not None:
        from .sections.section_prescriptions import build_prescriptions_page_flowables
        story.append(PageBreak())
        story.extend(
            build_prescriptions_page_flowables(
                prescriptions_map_png,
                prescriptions_result,
                tw,
            )
        )

    story.append(PageBreak())

    # Corps par article (hors article 9 préemption, traité séparément si besoin)
    articles: Dict[str, List[Dict]] = {}
    for layer in intersections:
        art = str(layer.get("article") or "8").split(",")[0].strip()
        if art not in ARTICLE_LABELS:
            art = "8"
        # Exclure PLU du corps (il a sa page dédiée)
        if layer.get("table") == "zone_urba" and plu_result is not None:
            continue
        # Exclure SUP du corps (section servitudes dédiée)
        if str(layer.get("article", "")).startswith("4") and servitudes_result is not None:
            continue
        # Exclure prescriptions du corps (section prescriptions dédiée)
        if layer.get("table") in PRESCRIPTION_TABLE_NAMES and prescriptions_result is not None:
            continue
        # Exclure info_surf DPU du corps si page DPU dédiée présente
        if (layer.get("table") == "info_surf"
                and dpu_result is not None):
            continue
        articles.setdefault(art, []).append(layer)

    for art_key in sorted(articles, key=lambda k: int(k) if k.isdigit() else 99):
        layers = sorted(articles[art_key], key=lambda x: (x.get("display_name") or "").lower())
        story.extend(_article_section(art_key, layers, st, page_w))

    # Articles hors article 9 (préemption gérée séparément via section DPU dédiée)
    # → les couches article=9 (zone_pdc etc.) restent dans le corps si pas de page DPU

    # Page DPU dédiée (toujours présente si dpu_result disponible)
    if dpu_result is not None:
        from .sections.section_dpu import build_dpu_page_flowables
        story.append(PageBreak())
        story.extend(build_dpu_page_flowables(dpu_map_png, dpu_result, tw))
    else:
        # Fallback : affichage dans le corps si zone_pdc a des données
        preemption = [i for i in intersections if str(i.get("article", "")).startswith("9")]
        if preemption:
            story.extend(_article_section("9", preemption, st, page_w))

    # Page subdivision fiscale dédiée (après DPU)
    if subdivision_result is not None:
        from .sections.section_subdivision import build_subdivision_page_flowables

        story.append(PageBreak())
        story.extend(
            build_subdivision_page_flowables(
                subdivision_map_png,
                subdivision_result,
                tw,
            )
        )

    doc.build(story, onFirstPage=decorator, onLaterPages=decorator)
    logger.info("✅ Rapport PDF généré : %s", output_path)
    return str(output_path.resolve())