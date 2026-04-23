"""
pdf/sections/section_intro.py
=============================
Section d'introduction / page de garde :
- bloc d'identité foncière,
- table de détail parcellaire,
- carte UF (fond + limites + numéros).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape as xml_escape

from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import HRFlowable, Image, Paragraph, Spacer, Table, TableStyle

C_LIGHT = colors.HexColor("#52B788")
C_BORDER = colors.HexColor("#B7D9C8")


def _styles() -> Dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    n = base["Normal"]
    return {
        "kicker": ParagraphStyle(
            "IntroKicker", parent=n, fontSize=9,
            textColor=colors.HexColor("#5c7268"),
            fontName="Helvetica", spaceAfter=4, leading=11,
        ),
        "title": ParagraphStyle(
            "IntroTitle", parent=n, fontSize=20,
            textColor=colors.HexColor("#2D6A4F"),
            fontName="Helvetica-Bold", spaceAfter=4, leading=24,
        ),
        "subtitle": ParagraphStyle(
            "IntroSubtitle", parent=n, fontSize=11,
            textColor=colors.HexColor("#555555"),
            fontName="Helvetica", spaceAfter=2, leading=14,
        ),
        "cover_label": ParagraphStyle(
            "IntroCoverLbl", parent=n, fontSize=9,
            textColor=colors.HexColor("#5a5a5a"),
            fontName="Helvetica-Bold", leading=12,
        ),
        "cover_value": ParagraphStyle(
            "IntroCoverVal", parent=n, fontSize=9.5,
            textColor=colors.HexColor("#1a1a1a"),
            fontName="Helvetica", leading=12,
        ),
        "cover_zonage": ParagraphStyle(
            "IntroCoverZon", parent=n, fontSize=10,
            textColor=colors.HexColor("#1a4d36"),
            fontName="Helvetica-Bold", leading=13,
        ),
        "attr_key": ParagraphStyle(
            "IntroAttrKey", parent=n, fontSize=8,
            textColor=colors.HexColor("#666666"),
            fontName="Helvetica-Bold", leading=11,
        ),
        "attr_val": ParagraphStyle(
            "IntroAttrVal", parent=n, fontSize=9,
            textColor=colors.HexColor("#222222"),
            fontName="Helvetica", leading=12,
        ),
    }


def _image_size(png_path: Path, target_width: float) -> tuple[float, float]:
    try:
        from PIL import Image as PILImage
        with PILImage.open(png_path) as im:
            pw, ph = im.size
        w = max(float(target_width), 1.0)
        return w, w * ph / pw
    except Exception:
        w = max(float(target_width), 1.0)
        return w, w * 0.68


def build_intro_page_flowables(
    result: Dict[str, Any],
    table_width: float,
    intro_map_png: Optional[str] = None,
) -> List[Any]:
    st = _styles()
    tw = max(float(table_width), 120.0)
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

    # Zonage PLU principal trouvé dans les intersections.
    zonage_str = "—"
    for inter in result.get("intersections", []):
        if inter.get("table") != "zone_urba":
            continue
        if inter.get("_plu_all_zonages_below_min_pct"):
            zonage_str = "Aucun zonage ≥ 1 % surface"
            break
        els = [e.get("libelle", "") for e in inter.get("elements", []) if e.get("libelle")]
        if els:
            zonage_str = ", ".join(dict.fromkeys(els))
            if len(zonage_str) > 200:
                zonage_str = zonage_str[:197] + "…"
        break

    refs = result.get("parcelles_cadastrales", [])
    if refs:
        ref_lines = [
            f"<b>{xml_escape(p['section'])} {xml_escape(p['numero'])}</b>"
            for p in refs if p.get("section")
        ]
        ref_html = "<br/>".join(ref_lines) or "—"
        ref_label = "Références cadastrales" if len(refs) > 1 else "Référence cadastrale"
    else:
        ref_html = "<b>" + xml_escape(result.get("parcelle", "—")) + "</b>"
        ref_label = "Référence cadastrale"

    flow: List[Any] = []
    flow.append(Spacer(1, 0.6 * cm))
    flow.append(Paragraph("IDENTITÉ FONCIÈRE", st["kicker"]))
    flow.append(Paragraph("CARTE D'IDENTITÉ FONCIÈRE", st["title"]))
    flow.append(Paragraph("Synthèse des intersections réglementaires et du zonage PLU.", st["subtitle"]))
    flow.append(HRFlowable(width="100%", thickness=2, color=C_LIGHT))
    flow.append(Spacer(1, 10))

    lw, vw = tw * 0.34, tw * 0.66
    rows = [
        (Paragraph("Commune", st["cover_label"]), Paragraph(xml_escape(commune), st["cover_value"])),
        (Paragraph("Code INSEE", st["cover_label"]), Paragraph(xml_escape(insee), st["cover_value"])),
        (Paragraph(xml_escape(ref_label), st["cover_label"]), Paragraph(ref_html, st["cover_value"])),
        (Paragraph("Zonage urbain (PLU)", st["cover_label"]), Paragraph(xml_escape(zonage_str), st["cover_zonage"])),
        (Paragraph("Superficie estimée", st["cover_label"]), Paragraph(xml_escape(surface_str), st["cover_value"])),
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

    if parcelles_detail and len(parcelles_detail) > 1:
        flow.append(Spacer(1, 10))
        flow.append(Paragraph("Détail des parcelles cadastrales", st["cover_label"]))
        flow.append(Spacer(1, 4))
        hdr = [
            Paragraph("Référence", st["attr_key"]),
            Paragraph("Surface cadastrale", st["attr_key"]),
            Paragraph("% de l'UF", st["attr_key"]),
        ]
        pr_rows: List[List[Any]] = [hdr]
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

    if intro_map_png and Path(intro_map_png).is_file():
        flow.append(Spacer(1, 12))
        img_w, img_h = _image_size(Path(intro_map_png), tw * 0.98)
        flow.append(Image(str(Path(intro_map_png)), width=img_w, height=img_h))

    flow.append(Spacer(1, 10))
    return flow
