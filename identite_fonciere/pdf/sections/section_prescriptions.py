"""
pdf/sections/section_prescriptions.py
=====================================
Section dédiée aux prescriptions PLU (surfacique, linéaire, ponctuelle) :
une seule carte, tableau d'attributs, pas de stats parcelle par parcelle.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape as xml_escape

import geopandas as gpd
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import HRFlowable, Image, Paragraph, Spacer, Table, TableStyle

C_GREEN = colors.HexColor("#2D6A4F")
C_LIGHT = colors.HexColor("#52B788")
C_BORDER = colors.HexColor("#B7D9C8")

PRESCRIPTION_TABLE_NAMES = frozenset({"prescription_surf", "prescription_lin", "prescription_pct"})

_NATURE_LABEL = {
    "prescription_surf": "Surfacique",
    "prescription_lin": "Linéaire",
    "prescription_pct": "Ponctuelle",
}


def compute_prescriptions_result(
    intersections: List[Dict[str, Any]],
    pres_gdfs: Dict[str, gpd.GeoDataFrame],
) -> Dict[str, Any]:
    """
    Contrat prescriptions :
    - intersecte : au moins une entité intersectant l'UF
    - attributs : nature + libelle + txt + typepsc (dédupliqué)
    """
    result: Dict[str, Any] = {
        "intersecte": False,
        "attributs": [],
    }

    attrs_seen: set = set()
    rows: List[Dict[str, str]] = []

    for layer in intersections:
        tbl = str(layer.get("table") or "")
        if tbl not in PRESCRIPTION_TABLE_NAMES:
            continue
        nature = _NATURE_LABEL.get(tbl, tbl)
        for el in layer.get("elements") or []:
            lib = str(el.get("libelle") or "—").strip() or "—"
            txt = str(el.get("txt") or "—").strip() or "—"
            tpsc = str(el.get("typepsc") or "—").strip() or "—"
            key = (tbl, lib, txt, tpsc)
            if key in attrs_seen:
                continue
            attrs_seen.add(key)
            rows.append({
                "nature": nature,
                "libelle": lib,
                "txt": txt,
                "typepsc": tpsc,
            })

    any_geom = any(
        gdf is not None and not gdf.empty and "geometry" in gdf.columns
        for t, gdf in (pres_gdfs or {}).items()
        if t in PRESCRIPTION_TABLE_NAMES
    )
    result["intersecte"] = any_geom or len(rows) > 0
    rows.sort(key=lambda r: (r["nature"], r["libelle"], r["typepsc"]))
    result["attributs"] = rows
    return result


def _styles() -> Dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    n = base["Normal"]
    return {
        "kicker": ParagraphStyle(
            "PscKickerSec", parent=n,
            fontSize=8, textColor=colors.HexColor("#6b7280"),
            fontName="Helvetica-Bold", spaceAfter=6, leading=10,
        ),
        "title": ParagraphStyle(
            "PscTitleSec", parent=n,
            fontSize=17, textColor=colors.HexColor("#1e4d2f"),
            fontName="Helvetica-Bold", spaceAfter=8, leading=22,
        ),
        "badge_ok": ParagraphStyle(
            "PscBadgeOk", parent=n,
            fontSize=10, textColor=colors.white,
            fontName="Helvetica-Bold", leading=13,
        ),
        "badge_no": ParagraphStyle(
            "PscBadgeNo", parent=n,
            fontSize=10, textColor=colors.HexColor("#374151"),
            fontName="Helvetica-Bold", leading=13,
        ),
        "tbl_hdr": ParagraphStyle(
            "PscTblHdrSec", parent=n,
            fontSize=8.2, textColor=colors.HexColor("#1e4d2f"),
            fontName="Helvetica-Bold", leading=10.5,
        ),
        "tbl_cell": ParagraphStyle(
            "PscTblCellSec", parent=n,
            fontSize=7.9, textColor=colors.HexColor("#1f2937"),
            fontName="Helvetica", leading=10.2,
        ),
        "body": ParagraphStyle(
            "PscBodySec", parent=n,
            fontSize=9, textColor=colors.HexColor("#374151"),
            fontName="Helvetica", leading=13, spaceAfter=4,
        ),
        "note": ParagraphStyle(
            "PscNoteSec", parent=n,
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
        return w, w / (1.0 + 0.40)


def build_prescriptions_page_flowables(
    prescriptions_map_png: Optional[str],
    prescriptions_result: Dict[str, Any],
    table_width: float,
) -> List[Any]:
    st = _styles()
    tw = max(float(table_width), 120.0)
    intersecte = bool(prescriptions_result.get("intersecte", False))
    attrs = prescriptions_result.get("attributs") or []

    flow: List[Any] = []
    flow.append(Spacer(1, 0.4 * cm))
    flow.append(Paragraph("ARTICLE 7 — PRESCRIPTIONS PLU", st["kicker"]))
    flow.append(Paragraph("Prescriptions surfaciques, linéaires et ponctuelles", st["title"]))

    if intersecte:
        badge_text = f"✓  Prescriptions détectées — {len(attrs)} enregistrement(s)"
        badge_tbl = Table([[Paragraph(xml_escape(badge_text), st["badge_ok"])]], colWidths=[tw], rowHeights=[26])
        badge_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), C_GREEN),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
    else:
        badge_text = "✗  Aucune prescription PLU intersectant l'unité foncière"
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

    if prescriptions_map_png and Path(prescriptions_map_png).is_file():
        img_w, img_h = _image_size(Path(prescriptions_map_png), tw * 0.98)
        flow.append(Image(str(Path(prescriptions_map_png)), width=img_w, height=img_h))

    if intersecte and attrs:
        flow.append(Spacer(1, 14))
        flow.append(HRFlowable(width="100%", thickness=1, color=C_LIGHT))
        flow.append(Spacer(1, 10))
        flow.append(Paragraph("Synthèse attributaire", st["body"]))

        ph = st["tbl_hdr"]
        pc = st["tbl_cell"]
        tbl_rows: List[List[Any]] = [[
            Paragraph("Nature", ph),
            Paragraph("Libellé", ph),
            Paragraph("Référence (txt)", ph),
            Paragraph("Type PSC", ph),
        ]]
        for r in attrs:
            tbl_rows.append([
                Paragraph(xml_escape(str(r.get("nature", "—"))), pc),
                Paragraph(xml_escape(str(r.get("libelle", "—"))), pc),
                Paragraph(xml_escape(str(r.get("txt", "—"))), pc),
                Paragraph(xml_escape(str(r.get("typepsc", "—"))), pc),
            ])
        tbl = Table(tbl_rows, colWidths=[tw * 0.16, tw * 0.34, tw * 0.22, tw * 0.28])
        tbl.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, C_BORDER),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8F5EE")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7FCF9")]),
        ]))
        flow.append(tbl)
    elif not intersecte:
        flow.append(Spacer(1, 12))
        flow.append(Paragraph(
            "Aucune prescription surfacique, linéaire ou ponctuelle n'a été détectée sur cette unité foncière "
            "dans les données du Géoportail de l'Urbanisme pour les couches prescription_surf, prescription_lin "
            "et prescription_pct.",
            st["body"],
        ))

    flow.append(Spacer(1, 8))
    flow.append(Paragraph(
        "Source : Géoportail de l'Urbanisme — couches wfs_du:prescription_surf, prescription_lin, prescription_pct. "
        "Données indicatives susceptibles d'évoluer.",
        st["note"],
    ))
    return flow
