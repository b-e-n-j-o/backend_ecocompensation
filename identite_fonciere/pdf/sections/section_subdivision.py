"""
pdf/sections/section_subdivision.py
===================================
Section dediee a la subdivision fiscale de l'unite fonciere.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
from xml.sax.saxutils import escape as xml_escape

from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import HRFlowable, Image, Paragraph, Spacer, Table, TableStyle

C_SUBDIV = colors.HexColor("#2563EB")
C_LIGHT = colors.HexColor("#52B788")
C_BORDER = colors.HexColor("#B7D9C8")


def _styles() -> Dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    n = base["Normal"]
    return {
        "kicker": ParagraphStyle(
            "SubdivKicker",
            parent=n,
            fontSize=8,
            textColor=colors.HexColor("#6b7280"),
            fontName="Helvetica-Bold",
            spaceAfter=6,
            leading=10,
        ),
        "title": ParagraphStyle(
            "SubdivTitle",
            parent=n,
            fontSize=17,
            textColor=colors.HexColor("#1e4d2f"),
            fontName="Helvetica-Bold",
            spaceAfter=8,
            leading=22,
        ),
        "badge_ok": ParagraphStyle(
            "SubdivBadgeOk",
            parent=n,
            fontSize=10,
            textColor=colors.white,
            fontName="Helvetica-Bold",
            leading=13,
        ),
        "badge_no": ParagraphStyle(
            "SubdivBadgeNo",
            parent=n,
            fontSize=10,
            textColor=colors.HexColor("#374151"),
            fontName="Helvetica-Bold",
            leading=13,
        ),
        "tbl_hdr": ParagraphStyle(
            "SubdivTblHdr",
            parent=n,
            fontSize=8.5,
            textColor=colors.HexColor("#1e4d2f"),
            fontName="Helvetica-Bold",
            leading=11,
        ),
        "tbl_cell": ParagraphStyle(
            "SubdivTblCell",
            parent=n,
            fontSize=8.5,
            textColor=colors.HexColor("#1f2937"),
            fontName="Helvetica",
            leading=11,
        ),
        "body": ParagraphStyle(
            "SubdivBody",
            parent=n,
            fontSize=9,
            textColor=colors.HexColor("#374151"),
            fontName="Helvetica",
            leading=13,
            spaceAfter=4,
        ),
        "note": ParagraphStyle(
            "SubdivNote",
            parent=n,
            fontSize=8,
            textColor=colors.HexColor("#6b7280"),
            fontName="Helvetica-Oblique",
            leading=11,
            spaceBefore=6,
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


def build_subdivision_page_flowables(
    subdivision_map_png: str,
    subdivision_result: Dict[str, Any],
    table_width: float,
) -> List[Any]:
    pp = Path(subdivision_map_png)
    if not pp.is_file():
        return []

    st = _styles()
    tw = max(float(table_width), 120.0)
    img_w, img_h = _image_size(pp, tw * 0.98)

    subdivisee = bool(subdivision_result.get("subdivisee", False))
    nb = int(subdivision_result.get("nb_entites", 0) or 0)
    nb_parc = int(subdivision_result.get("nb_parcelles_avec_subdivision", 0) or 0)
    rows = subdivision_result.get("rows") or []

    flow: List[Any] = []
    flow.append(Spacer(1, 0.4 * cm))
    flow.append(Paragraph("ARTICLE 10 — SUBDIVISION FISCALE", st["kicker"]))
    flow.append(Paragraph("Subdivision fiscale de l'unite fonciere", st["title"]))

    if subdivisee:
        badge_text = f"✓  Unite fonciere subdivisee - {nb} entite(s) fiscale(s) sur {nb_parc} parcelle(s)"
        badge_tbl = Table([[Paragraph(xml_escape(badge_text), st["badge_ok"])]], colWidths=[tw], rowHeights=[26])
        badge_tbl.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), C_SUBDIV),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("LEFTPADDING", (0, 0), (-1, -1), 12),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ]
            )
        )
    else:
        badge_text = "✗  Aucune subdivision fiscale detectee sur cette unite fonciere"
        badge_tbl = Table([[Paragraph(xml_escape(badge_text), st["badge_no"])]], colWidths=[tw], rowHeights=[26])
        badge_tbl.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F3F4F6")),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("LEFTPADDING", (0, 0), (-1, -1), 12),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D1D5DB")),
                ]
            )
        )

    flow.append(badge_tbl)
    flow.append(Spacer(1, 10))
    flow.append(HRFlowable(width="100%", thickness=1, color=C_LIGHT))
    flow.append(Spacer(1, 12))
    flow.append(Image(str(pp), width=img_w, height=img_h))

    if subdivisee and rows:
        flow.append(Spacer(1, 14))
        flow.append(HRFlowable(width="100%", thickness=1, color=C_LIGHT))
        flow.append(Spacer(1, 10))
        ph = st["tbl_hdr"]
        pc = st["tbl_cell"]
        table_rows = [
            [
                Paragraph("IDU parcelle", ph),
                Paragraph("Subdivision", ph),
                Paragraph("Surface calculee", ph),
                Paragraph("% de l'UF", ph),
            ]
        ]
        for r in rows:
            surf = f"{float(r.get('surface_calc_m2', 0.0)):.0f} m2"
            pct = f"{float(r.get('pct_uf', 0.0)):.1f} %".replace(".", ",")
            table_rows.append(
                [
                    Paragraph(xml_escape(str(r.get("idu_parcel", "n/a"))), pc),
                    Paragraph(xml_escape(str(r.get("lettre", "n/a"))), pc),
                    Paragraph(xml_escape(surf), pc),
                    Paragraph(xml_escape(pct), pc),
                ]
            )
        tbl = Table(table_rows, colWidths=[tw * 0.32, tw * 0.18, tw * 0.28, tw * 0.22])
        tbl.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.5, C_BORDER),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DBEAFE")),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#EFF6FF")]),
                ]
            )
        )
        flow.append(tbl)
        flow.append(Spacer(1, 10))
        flow.append(
            Paragraph(
                "La subdivision fiscale detaille les entites fiscales rattachees a une parcelle cadastrale. "
                "Une unite fonciere est consideree subdivisee des lors que plusieurs subdivisions fiscales "
                "sont presentes sur son emprise.",
                st["body"],
            )
        )
    else:
        flow.append(Spacer(1, 14))
        flow.append(
            Paragraph(
                "Aucune entite de subdivision fiscale n'a ete detectee sur l'unite fonciere dans "
                "les donnees IGN disponibles. Cela correspond le plus souvent a une parcelle mono-entite.",
                st["body"],
            )
        )

    flow.append(Spacer(1, 8))
    flow.append(
        Paragraph(
            "Source : IGN Parcellaire Express - couche subdivision_fiscale. "
            "Donnees indicatives, sous reserve de mise a jour du flux WFS.",
            st["note"],
        )
    )
    return flow
