"""
pdf/sections/section_plu.py
===========================
Section dédiée au zonage PLU :
- carte PLU (si disponible)
- répartition UF par zonage (typezone/libelle/libelong + % + ha)
- répartition parcelle par parcelle
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape as xml_escape

from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import HRFlowable, Image, Paragraph, Spacer, Table, TableStyle

C_GREEN = colors.HexColor("#2D6A4F")
C_LIGHT = colors.HexColor("#52B788")
C_BORDER = colors.HexColor("#B7D9C8")


def _styles() -> Dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    n = base["Normal"]
    return {
        "kicker": ParagraphStyle(
            "PluKickerSec", parent=n,
            fontSize=8, textColor=colors.HexColor("#6b7280"),
            fontName="Helvetica-Bold", spaceAfter=6, leading=10,
        ),
        "title": ParagraphStyle(
            "PluTitleSec", parent=n,
            fontSize=17, textColor=colors.HexColor("#1e4d2f"),
            fontName="Helvetica-Bold", spaceAfter=8, leading=22,
        ),
        "badge_ok": ParagraphStyle(
            "PluBadgeOk", parent=n,
            fontSize=10, textColor=colors.white,
            fontName="Helvetica-Bold", leading=13,
        ),
        "badge_no": ParagraphStyle(
            "PluBadgeNo", parent=n,
            fontSize=10, textColor=colors.HexColor("#374151"),
            fontName="Helvetica-Bold", leading=13,
        ),
        "tbl_hdr": ParagraphStyle(
            "PluTblHdrSec", parent=n,
            fontSize=8.2, textColor=colors.HexColor("#1e4d2f"),
            fontName="Helvetica-Bold", leading=10.5,
        ),
        "tbl_cell": ParagraphStyle(
            "PluTblCellSec", parent=n,
            fontSize=7.9, textColor=colors.HexColor("#1f2937"),
            fontName="Helvetica", leading=10.2,
        ),
        "body": ParagraphStyle(
            "PluBodySec", parent=n,
            fontSize=9, textColor=colors.HexColor("#374151"),
            fontName="Helvetica", leading=13, spaceAfter=4,
        ),
        "note": ParagraphStyle(
            "PluNoteSec", parent=n,
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


def build_plu_page_flowables(
    plu_map_png: Optional[str],
    plu_result: Dict[str, Any],
    table_width: float,
) -> List[Any]:
    st = _styles()
    tw = max(float(table_width), 120.0)
    intersecte = bool(plu_result.get("intersecte", False))
    uf_rows = plu_result.get("uf_repartition") or []
    parc_rows = plu_result.get("parcelles_repartition") or []

    flow: List[Any] = []
    flow.append(Spacer(1, 0.4 * cm))
    flow.append(Paragraph("ARTICLE 3 — ZONAGE URBAIN", st["kicker"]))
    flow.append(Paragraph("Zonage PLU de l'unité foncière", st["title"]))

    if intersecte:
        badge_text = f"✓  Zonage PLU détecté — {len(uf_rows)} zone(s) sur l'unité foncière"
        badge_tbl = Table([[Paragraph(xml_escape(badge_text), st["badge_ok"])]], colWidths=[tw], rowHeights=[26])
        badge_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), C_GREEN),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
    else:
        badge_text = "✗  Aucune donnée de zonage PLU intersectant l'unité foncière"
        badge_tbl = Table([[Paragraph(xml_escape(badge_text), st["badge_no"])]], colWidths=[tw], rowHeights=[26])
        badge_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F3F4F6")),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D1D5DB")),
        ]))

    flow.append(badge_tbl)
    flow.append(Spacer(1, 10))
    flow.append(HRFlowable(width="100%", thickness=1, color=C_LIGHT))
    flow.append(Spacer(1, 12))

    if plu_map_png and Path(plu_map_png).is_file():
        img_w, img_h = _image_size(Path(plu_map_png), tw * 0.98)
        flow.append(Image(str(Path(plu_map_png)), width=img_w, height=img_h))

    if intersecte and uf_rows:
        ph = st["tbl_hdr"]
        pc = st["tbl_cell"]

        flow.append(Spacer(1, 14))
        flow.append(HRFlowable(width="100%", thickness=1, color=C_LIGHT))
        flow.append(Spacer(1, 10))
        flow.append(Paragraph("Répartition des zonages sur l'unité foncière", st["body"]))

        uf_table_rows: List[List[Any]] = [[
            Paragraph("Type zone", ph),
            Paragraph("Libellé", ph),
            Paragraph("Libellé long", ph),
            Paragraph("% couverture UF", ph),
            Paragraph("Surface (ha)", ph),
        ]]
        for r in uf_rows:
            uf_table_rows.append([
                Paragraph(xml_escape(str(r.get("typezone", "—"))), pc),
                Paragraph(xml_escape(str(r.get("libelle", "—"))), pc),
                Paragraph(xml_escape(str(r.get("libelong", "—"))), pc),
                Paragraph(xml_escape(str(f"{float(r.get('pct_uf', 0.0)):.2f} %").replace(".", ",")), pc),
                Paragraph(xml_escape(str(f"{float(r.get('surface_ha', 0.0)):.4f}").replace(".", ",")), pc),
            ])
        uf_tbl = Table(uf_table_rows, colWidths=[tw * 0.13, tw * 0.18, tw * 0.41, tw * 0.14, tw * 0.14])
        uf_tbl.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, C_BORDER),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8F5EE")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7FCF9")]),
        ]))
        flow.append(uf_tbl)

        if parc_rows:
            flow.append(Spacer(1, 12))
            flow.append(Paragraph("Répartition des zonages parcelle par parcelle", st["body"]))

            parc_table_rows: List[List[Any]] = [[
                Paragraph("Parcelle", ph),
                Paragraph("Type zone", ph),
                Paragraph("Libellé", ph),
                Paragraph("% parcelle", ph),
                Paragraph("Surface (ha)", ph),
            ]]
            for r in parc_rows:
                parc_table_rows.append([
                    Paragraph(xml_escape(str(r.get("parcelle_ref", "—"))), pc),
                    Paragraph(xml_escape(str(r.get("typezone", "—"))), pc),
                    Paragraph(xml_escape(str(r.get("libelle", "—"))), pc),
                    Paragraph(xml_escape(str(f"{float(r.get('pct_parcelle', 0.0)):.2f} %").replace(".", ",")), pc),
                    Paragraph(xml_escape(str(f"{float(r.get('surface_ha', 0.0)):.4f}").replace(".", ",")), pc),
                ])
            parc_tbl = Table(parc_table_rows, colWidths=[tw * 0.17, tw * 0.15, tw * 0.40, tw * 0.14, tw * 0.14])
            parc_tbl.setStyle(TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.5, C_BORDER),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8F5EE")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7FCF9")]),
            ]))
            flow.append(parc_tbl)
    else:
        flow.append(Spacer(1, 12))
        flow.append(Paragraph(
            "Aucune donnée exploitable de zonage PLU n'a été détectée pour cette unité foncière "
            "dans le Géoportail de l'Urbanisme. Cela peut indiquer : absence de publication du "
            "PLU sur le GPU, document en cours de mise à jour, ou un autre régime d'urbanisme "
            "(carte communale, RNU, SCOT, etc.).",
            st["body"],
        ))

    flow.append(Spacer(1, 8))
    flow.append(Paragraph(
        "Source : Géoportail de l'Urbanisme — couche wfs_du:zone_urba. "
        "Données indicatives susceptibles d'évoluer.",
        st["note"],
    ))
    return flow
