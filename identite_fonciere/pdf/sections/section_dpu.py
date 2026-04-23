"""
pdf/section_dpu.py
==================
Génère les flowables ReportLab pour la page dédiée au Droit de Préemption Urbain.

Deux cas :
  - UF soumise au DPU  → carte + tableau récap + message explicatif
  - UF non soumise     → carte satellite simple + message "Non soumise"

La section est TOUJOURS générée (même si non soumise), ce qui permet à l'utilisateur
d'avoir une réponse explicite dans tous les cas — convention alignée sur les CUA.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape as xml_escape

from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable, Image, Paragraph, Spacer, Table, TableStyle,
)

C_DPU       = colors.HexColor("#6D28D9")    # violet — identique à carte_dpu.py
C_DPU_LIGHT = colors.HexColor("#EDE9FE")    # violet très clair pour fonds de tableau
C_GREEN     = colors.HexColor("#2D6A4F")
C_LIGHT     = colors.HexColor("#52B788")
C_BORDER    = colors.HexColor("#B7D9C8")


def _styles() -> Dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    N    = base["Normal"]
    return {
        "kicker": ParagraphStyle(
            "DpuKicker", parent=N,
            fontSize=8, textColor=colors.HexColor("#6b7280"),
            fontName="Helvetica-Bold", spaceAfter=6, leading=10,
        ),
        "title": ParagraphStyle(
            "DpuTitle", parent=N,
            fontSize=17, textColor=colors.HexColor("#1e4d2f"),
            fontName="Helvetica-Bold", spaceAfter=8, leading=22,
        ),
        "soumise_badge": ParagraphStyle(
            "DpuBadge", parent=N,
            fontSize=10, textColor=colors.white,
            fontName="Helvetica-Bold", leading=13,
        ),
        "non_soumise_badge": ParagraphStyle(
            "DpuBadgeNo", parent=N,
            fontSize=10, textColor=colors.HexColor("#374151"),
            fontName="Helvetica-Bold", leading=13,
        ),
        "body": ParagraphStyle(
            "DpuBody", parent=N,
            fontSize=9, textColor=colors.HexColor("#374151"),
            fontName="Helvetica", leading=13, spaceAfter=4,
        ),
        "tbl_hdr": ParagraphStyle(
            "DpuTblHdr", parent=N,
            fontSize=8.5, textColor=colors.HexColor("#1e4d2f"),
            fontName="Helvetica-Bold", leading=11,
        ),
        "tbl_cell": ParagraphStyle(
            "DpuTblCell", parent=N,
            fontSize=8.5, textColor=colors.HexColor("#1f2937"),
            fontName="Helvetica", leading=11,
        ),
        "note": ParagraphStyle(
            "DpuNote", parent=N,
            fontSize=8, textColor=colors.HexColor("#6b7280"),
            fontName="Helvetica-Oblique", leading=11, spaceBefore=6,
        ),
    }


def _image_size(png_path: Path, target_width: float) -> tuple:
    try:
        from PIL import Image as PILImage
        with PILImage.open(png_path) as im:
            pw, ph = im.size
        w = max(float(target_width), 1.0)
        return w, w * ph / pw
    except Exception:
        w = max(float(target_width), 1.0)
        return w, w / (1.0 + 0.34)


def build_dpu_page_flowables(
    dpu_map_png: Optional[str],
    dpu_result: Dict[str, Any],
    table_width: float,
) -> List[Any]:
    """
    Construit les flowables de la page DPU.

    Args:
        dpu_map_png  : chemin du PNG généré par carte_dpu.render_dpu_map()
        dpu_result   : dict retourné par carte_dpu.compute_dpu_result()
                       { intersecte, dpu_gdf, libelles, nb_entites }
        table_width  : largeur utile de la page en points (page_w - marges)

    Returns:
        Liste de flowables ReportLab.
    """
    pp = Path(dpu_map_png) if dpu_map_png else None

    st         = _styles()
    tw         = max(float(table_width), 120.0)
    img_w = tw * 0.98
    if pp and pp.is_file():
        img_w, img_h = _image_size(pp, img_w)
    else:
        img_h = 0.0
    intersecte = dpu_result.get("intersecte", False)
    libelles   = dpu_result.get("libelles", [])
    nb         = dpu_result.get("nb_entites", 0)

    flow: List[Any] = []

    # ── En-tête ────────────────────────────────────────────────────────────
    flow.append(Spacer(1, 0.4 * cm))
    flow.append(Paragraph("ARTICLE 9 — DROITS DE PRÉEMPTION", st["kicker"]))
    flow.append(Paragraph("Droit de Préemption Urbain (DPU)", st["title"]))

    # Badge soumise / non soumise
    if intersecte:
        badge_text = f"✓  Unité foncière soumise au DPU — {nb} entité(s) intersectée(s)"
        badge_tbl = Table(
            [[Paragraph(xml_escape(badge_text), st["soumise_badge"])]],
            colWidths=[tw], rowHeights=[26],
        )
        badge_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), C_DPU),
            ("TOPPADDING",    (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING",   (0, 0), (-1, -1), 12),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ]))
    else:
        badge_text = "✗  Unité foncière non soumise au DPU dans ce périmètre"
        badge_tbl = Table(
            [[Paragraph(xml_escape(badge_text), st["non_soumise_badge"])]],
            colWidths=[tw], rowHeights=[26],
        )
        badge_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#F3F4F6")),
            ("TOPPADDING",    (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING",   (0, 0), (-1, -1), 12),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
            ("BOX",           (0, 0), (-1, -1), 0.5, colors.HexColor("#D1D5DB")),
        ]))

    flow.append(badge_tbl)
    flow.append(Spacer(1, 10))
    flow.append(HRFlowable(width="100%", thickness=1, color=C_LIGHT))
    flow.append(Spacer(1, 12))

    # ── Carte (optionnelle) ────────────────────────────────────────────────
    if pp and pp.is_file():
        flow.append(Image(str(pp), width=img_w, height=img_h))

    # ── Tableau récap si soumise ────────────────────────────────────────────
    if intersecte:
        flow.append(Spacer(1, 14))
        flow.append(HRFlowable(width="100%", thickness=1, color=C_LIGHT))
        flow.append(Spacer(1, 10))

        ph = st["tbl_hdr"]
        pc = st["tbl_cell"]
        hdr = [
            Paragraph(xml_escape("Type"),                    ph),
            Paragraph(xml_escape("Code"),                    ph),
            Paragraph(xml_escape("Libellé"),                 ph),
            Paragraph(xml_escape("Source GPU (info_surf)"),  ph),
        ]
        rows: List[List] = [hdr]
        for lb in (libelles or ["Droit de préemption urbain"]):
            rows.append([
                Paragraph("Information réglementaire", pc),
                Paragraph("typeinf = 04",              pc),
                Paragraph(xml_escape(lb),              pc),
                Paragraph("wfs_du:info_surf",          pc),
            ])

        tbl = Table(rows, colWidths=[tw*0.24, tw*0.18, tw*0.34, tw*0.24])
        tbl.setStyle(TableStyle([
            ("GRID",         (0, 0), (-1, -1), 0.5, C_BORDER),
            ("VALIGN",       (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING",   (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING",(0, 0), (-1, -1), 6),
            ("LEFTPADDING",  (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("BACKGROUND",   (0, 0), (-1, 0),  colors.HexColor("#EDE9FE")),
            ("ROWBACKGROUNDS",(0, 1), (-1, -1),
             [colors.white, colors.HexColor("#F5F3FF")]),
        ]))
        flow.append(tbl)

        flow.append(Spacer(1, 10))
        flow.append(Paragraph(
            "Le Droit de Préemption Urbain (DPU) permet à la collectivité "
            "d'acquérir prioritairement un bien immobilier mis en vente, "
            "dans les zones délimitées au PLU. La présence d'une zone DPU "
            "intersectant cette unité foncière implique d'informer l'acquéreur "
            "et de respecter la procédure de déclaration d'intention d'aliéner (DIA).",
            st["body"],
        ))

    else:
        # Message explicatif même si non soumise
        flow.append(Spacer(1, 14))
        flow.append(Paragraph(
            "Aucune zone de Droit de Préemption Urbain (DPU) n'a été détectée "
            "intersectant cette unité foncière dans les données du Géoportail "
            "de l'Urbanisme (GPU). Cette information est issue de la couche "
            "wfs_du:info_surf (typeinf=04) et peut ne pas être exhaustive si "
            "la commune n'a pas encore publié ses données sur le GPU.",
            st["body"],
        ))

    flow.append(Spacer(1, 8))
    flow.append(Paragraph(
        "Source : Géoportail de l'Urbanisme — flux WFS wfs_du:info_surf "
        "(typeinf=04). Données susceptibles d'évoluer ; se référer au "
        "service urbanisme de la commune pour confirmation.",
        st["note"],
    ))

    return flow