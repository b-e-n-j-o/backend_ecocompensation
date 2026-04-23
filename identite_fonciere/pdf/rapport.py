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
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.platypus import (
    HRFlowable, Image, KeepTogether, PageBreak,
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

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
# Page de garde
# ---------------------------------------------------------------------------

def _cover_page(
    result: Dict[str, Any],
    st: Dict[str, ParagraphStyle],
    tw: float,
) -> List[Any]:
    flow: List[Any] = []
    commune = result.get("commune", "—")
    insee = result.get("insee", "—")
    surface_m2 = result.get("surface_uf_m2")
    parcelles_detail = result.get("parcelles_uf_detail", [])

    surface_str = "—"
    if surface_m2:
        try:
            m2 = float(surface_m2)
            ha = m2 / 10_000
            sep = "\u202f"
            s = f"{int(round(m2)):,}".replace(",", sep)
            surface_str = f"{s} m² ({ha:.2f} ha)".replace(".", ",")
        except Exception:
            pass

    # Zonage PLU (premier libellé intersecté)
    zonage_str = "—"
    for inter in result.get("intersections", []):
        if inter.get("table") == "zone_urba":
            if inter.get("_plu_all_zonages_below_min_pct"):
                zonage_str = "Aucun zonage ≥ 1 % surface"
                break
            els = [e.get("libelle", "") for e in inter.get("elements", []) if e.get("libelle")]
            if els:
                zonage_str = ", ".join(dict.fromkeys(els))
                if len(zonage_str) > 200:
                    zonage_str = zonage_str[:197] + "…"
            break

    # Références cadastrales
    refs = result.get("parcelles_cadastrales", [])
    if refs:
        ref_lines = [f"<b>{xml_escape(p['section'])} {xml_escape(p['numero'])}</b>"
                     for p in refs if p.get("section")]
        ref_html = "<br/>".join(ref_lines) or "—"
        ref_label = "Références cadastrales" if len(refs) > 1 else "Référence cadastrale"
    else:
        ref_html = "<b>" + xml_escape(result.get("parcelle", "—")) + "</b>"
        ref_label = "Référence cadastrale"

    flow.append(Spacer(1, 0.6 * cm))
    flow.append(Paragraph("IDENTITÉ FONCIÈRE", st["kicker"]))
    flow.append(Paragraph("CARTE D'IDENTITÉ FONCIÈRE", st["title"]))
    flow.append(Paragraph(
        "Synthèse des intersections réglementaires et du zonage PLU.",
        st["subtitle"],
    ))
    flow.append(HRFlowable(width="100%", thickness=2, color=C_LIGHT))
    flow.append(Spacer(1, 10))

    lw, vw = tw * 0.34, tw * 0.66
    rows = [
        (Paragraph(xml_escape("Commune"), st["cover_label"]),
         Paragraph(xml_escape(commune), st["cover_value"])),
        (Paragraph(xml_escape("Code INSEE"), st["cover_label"]),
         Paragraph(xml_escape(insee), st["cover_value"])),
        (Paragraph(xml_escape(ref_label), st["cover_label"]),
         Paragraph(ref_html, st["cover_value"])),
        (Paragraph(xml_escape("Zonage urbain (PLU)"), st["cover_label"]),
         Paragraph(xml_escape(zonage_str), st["cover_zonage"])),
        (Paragraph(xml_escape("Superficie estimée"), st["cover_label"]),
         Paragraph(xml_escape(surface_str), st["cover_value"])),
    ]
    t = Table([[a, b] for a, b in rows], colWidths=[lw, vw])
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, C_BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F0F7F4")),
        ("BACKGROUND", (0, 3), (-1, 3), colors.HexColor("#E8F5EE")),
    ]))
    flow.append(t)

    # Détail parcelles si plusieurs
    if parcelles_detail and len(parcelles_detail) > 1:
        flow.append(Spacer(1, 10))
        flow.append(Paragraph(
            xml_escape("Détail des parcelles cadastrales"),
            st["cover_label"],
        ))
        flow.append(Spacer(1, 4))
        hdr = [
            Paragraph(xml_escape("Référence"), st["attr_key"]),
            Paragraph(xml_escape("Surface cadastrale"), st["attr_key"]),
            Paragraph(xml_escape("% de l'UF"), st["attr_key"]),
        ]
        pr_rows: List[List] = [hdr]
        for it in parcelles_detail:
            m2 = it.get("contenance_m2", 0)
            pct = it.get("pct_uf", 0)
            try:
                sep = "\u202f"
                srf = f"{int(round(float(m2))):,}".replace(",", sep) + " m²"
            except Exception:
                srf = "—"
            pr_rows.append([
                Paragraph(xml_escape(it.get("ref", "—")), st["attr_val"]),
                Paragraph(xml_escape(srf), st["attr_val"]),
                Paragraph(xml_escape(f"{float(pct):.1f} %".replace(".", ",")), st["attr_val"]),
            ])
        pt = Table(pr_rows, colWidths=[tw * 0.28, tw * 0.36, tw * 0.36])
        pt.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, C_BORDER),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F0F7F4")),
        ]))
        flow.append(pt)

    flow.append(Spacer(1, 12))
    return flow


# ---------------------------------------------------------------------------
# Page PLU (carte + tableau libellés)
# ---------------------------------------------------------------------------

def _plu_page(
    plu_map_png: str,
    intersections: List[Dict[str, Any]],
    st: Dict[str, ParagraphStyle],
    tw: float,
) -> List[Any]:
    flow: List[Any] = []
    pp = Path(plu_map_png)
    if not pp.is_file():
        return flow

    # Taille image (ratio fixe si PIL absent)
    img_w = max(float(tw) * 0.98, 1.0)
    try:
        from PIL import Image as PILImage
        with PILImage.open(pp) as im:
            pw, ph = im.size
        img_h = img_w * (ph / pw)
    except Exception:
        img_h = img_w / (1 + 0.34)

    flow.append(Spacer(1, 0.4 * cm))
    flow.append(Paragraph("VUE D'ENSEMBLE — ZONAGE PLU", st["plu_kicker"]))
    flow.append(Paragraph("Zonage PLU — carte et répartition surfacique", st["plu_title"]))

    band = Table(
        [[Paragraph('<font color="white"><b>Zonage réglementaire</b></font>',
                    ParagraphStyle("PluBand", parent=getSampleStyleSheet()["Normal"],
                                   fontSize=10, fontName="Helvetica-Bold", leading=12))]],
        colWidths=[tw], rowHeights=[22],
    )
    band.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), C_GREEN),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    flow.append(band)
    flow.append(Spacer(1, 10))
    flow.append(HRFlowable(width="100%", thickness=1, color=C_LIGHT))
    flow.append(Spacer(1, 12))
    flow.append(Image(str(pp), width=img_w, height=img_h))

    # Tableau libellés PLU
    plu_rows: List[Dict] = []
    for inter in intersections:
        if inter.get("table") != "zone_urba":
            continue
        for el in inter.get("elements", []):
            lb = el.get("libelle", "")
            lbe = el.get("libelong", el.get("libelle", ""))
            dest = el.get("destdomi", "")
            if lb:
                plu_rows.append({"libelle": lb, "libelong": lbe, "destdomi": dest})

    if plu_rows:
        flow.append(Spacer(1, 14))
        ph = st["plu_tbl_hdr"]
        pc = st["plu_tbl_cell"]
        hdr = [
            Paragraph(xml_escape("Libellé"), ph),
            Paragraph(xml_escape("Libellé long"), ph),
            Paragraph(xml_escape("Destination dominante"), ph),
        ]
        tbl_rows = [hdr]
        for r in plu_rows:
            tbl_rows.append([
                Paragraph(xml_escape(r["libelle"] or "—"), pc),
                Paragraph(xml_escape(r["libelong"] or "—"), pc),
                Paragraph(xml_escape(r["destdomi"] or "—"), pc),
            ])
        zt = Table(tbl_rows, colWidths=[tw * 0.20, tw * 0.40, tw * 0.40])
        zt.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, C_BORDER),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8F5EE")),
        ]))
        flow.append(zt)

    return flow


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


# ---------------------------------------------------------------------------
# Fonction principale
# ---------------------------------------------------------------------------

def _servitudes_page(
    sup_map_png: str,
    intersections: List[Dict[str, Any]],
    st: Dict[str, ParagraphStyle],
    tw: float,
) -> List[Any]:
    """
    Page dédiée aux servitudes d'utilité publique :
    titre + bandeau vert + carte satellite+SUP + tableau récapitulatif.
    """
    from reportlab.lib.styles import getSampleStyleSheet
    flow: List[Any] = []
    pp = Path(sup_map_png)
    if not pp.is_file():
        return flow

    # Taille image (ratio réel si PIL disponible)
    img_w = max(float(tw) * 0.98, 1.0)
    try:
        from PIL import Image as PILImage
        with PILImage.open(pp) as im:
            pw, ph = im.size
        img_h = img_w * (ph / pw)
    except Exception:
        img_h = img_w / (1.0 + 0.40)  # RIGHT_PANEL_RATIO de carte_servitudes

    ps_kicker = ParagraphStyle(
        "SupKicker", parent=getSampleStyleSheet()["Normal"],
        fontSize=8, textColor=colors.HexColor("#6b7f72"),
        fontName="Helvetica-Bold", spaceAfter=6, leading=10,
    )
    ps_title = ParagraphStyle(
        "SupTitle", parent=getSampleStyleSheet()["Normal"],
        fontSize=17, textColor=colors.HexColor("#1e4d2f"),
        fontName="Helvetica-Bold", spaceAfter=8, leading=22,
    )

    flow.append(Spacer(1, 0.4 * cm))
    flow.append(Paragraph("SERVITUDES D'UTILITÉ PUBLIQUE", ps_kicker))
    flow.append(Paragraph("Servitudes intersectant la parcelle / l'unité foncière", ps_title))

    band = Table(
        [[Paragraph(
            "<font color='white'><b>Périmètres de servitudes</b></font>",
            ParagraphStyle("SupBand", parent=getSampleStyleSheet()["Normal"],
                           fontSize=10, fontName="Helvetica-Bold", leading=12),
        )]],
        colWidths=[tw], rowHeights=[22],
    )
    band.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), C_GREEN),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    flow.append(band)
    flow.append(Spacer(1, 10))
    flow.append(HRFlowable(width="100%", thickness=1, color=C_LIGHT))
    flow.append(Spacer(1, 12))
    flow.append(Image(str(pp), width=img_w, height=img_h))

    # Tableau récapitulatif des SUP intersectées
    sup_layers = [
        i for i in intersections
        if str(i.get("article", "")).startswith("4")
    ]
    if sup_layers:
        flow.append(Spacer(1, 14))
        flow.append(HRFlowable(width="100%", thickness=1, color=C_LIGHT))
        flow.append(Spacer(1, 10))

        ph = ParagraphStyle("SupTblHdr", parent=getSampleStyleSheet()["Normal"],
                            fontSize=8.5, textColor=colors.HexColor("#1e4d2f"),
                            fontName="Helvetica-Bold", leading=11)
        pc = ParagraphStyle("SupTblCell", parent=getSampleStyleSheet()["Normal"],
                            fontSize=8, textColor=colors.HexColor("#2d3748"),
                            fontName="Helvetica", leading=10)

        hdr = [
            Paragraph(xml_escape("Type"), ph),
            Paragraph(xml_escape("Code SUP"), ph),
            Paragraph(xml_escape("Libellé / Désignation"), ph),
        ]
        tbl_rows: List[List[Any]] = [hdr]

        for layer in sup_layers:
            for el in layer.get("elements") or []:
                suptype = el.get("suptype", "—")
                nom = el.get("nomsuplitt", "") or "—"
                display = xml_escape(layer.get("display_name", ""))
                tbl_rows.append([
                    Paragraph(xml_escape(display), pc),
                    Paragraph(xml_escape(str(suptype)), pc),
                    Paragraph(xml_escape(str(nom)), pc),
                ])

        if len(tbl_rows) > 1:
            zt = Table(tbl_rows, colWidths=[tw * 0.32, tw * 0.16, tw * 0.52])
            zt.setStyle(TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.5, C_BORDER),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8F5EE")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                 [colors.white, colors.HexColor("#F7FCF9")]),
            ]))
            flow.append(zt)

    return flow


def generate_rapport_pdf(
    result: Dict[str, Any],
    output_dir: str = ".",
    logo_path: Optional[str] = None,
    filename: Optional[str] = None,
    plu_map_png: Optional[str] = None,
    servitudes_map_png: Optional[str] = None,
    dpu_map_png: Optional[str] = None,
    dpu_result: Optional[Dict[str, Any]] = None,
    subdivision_map_png: Optional[str] = None,
    subdivision_result: Optional[Dict[str, Any]] = None,
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
        servitudes_map_png : chemin PNG carte servitudes SUP (optionnel)
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
    story.extend(_cover_page(result, st, tw))

    # Page PLU (si carte disponible)
    if plu_map_png and Path(plu_map_png).is_file():
        story.append(PageBreak())
        story.extend(_plu_page(plu_map_png, intersections, st, tw))

    # Page servitudes (si carte disponible)
    if servitudes_map_png and Path(servitudes_map_png).is_file():
        story.append(PageBreak())
        story.extend(_servitudes_page(servitudes_map_png, intersections, st, tw))

    story.append(PageBreak())

    # Corps par article (hors article 9 préemption, traité séparément si besoin)
    articles: Dict[str, List[Dict]] = {}
    for layer in intersections:
        art = str(layer.get("article") or "8").split(",")[0].strip()
        if art not in ARTICLE_LABELS:
            art = "8"
        # Exclure PLU du corps (il a sa page dédiée)
        if layer.get("table") == "zone_urba" and plu_map_png:
            continue
        # Exclure info_surf DPU du corps si page DPU dédiée présente
        if (layer.get("table") == "info_surf"
                and dpu_map_png
                and dpu_result is not None):
            continue
        articles.setdefault(art, []).append(layer)

    for art_key in sorted(articles, key=lambda k: int(k) if k.isdigit() else 99):
        layers = sorted(articles[art_key], key=lambda x: (x.get("display_name") or "").lower())
        story.extend(_article_section(art_key, layers, st, page_w))

    # Articles hors article 9 (préemption gérée séparément via section DPU dédiée)
    # → les couches article=9 (zone_pdc etc.) restent dans le corps si pas de page DPU

    # Page DPU dédiée (toujours présente si dpu_map_png fourni)
    if dpu_map_png and Path(dpu_map_png).is_file() and dpu_result is not None:
        from .sections.section_dpu import build_dpu_page_flowables
        story.append(PageBreak())
        story.extend(build_dpu_page_flowables(dpu_map_png, dpu_result, tw))
    else:
        # Fallback : affichage dans le corps si zone_pdc a des données
        preemption = [i for i in intersections if str(i.get("article", "")).startswith("9")]
        if preemption:
            story.extend(_article_section("9", preemption, st, page_w))

    # Page subdivision fiscale dédiée (après DPU)
    if (
        subdivision_map_png
        and Path(subdivision_map_png).is_file()
        and subdivision_result is not None
    ):
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