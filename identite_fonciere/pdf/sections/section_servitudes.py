"""
pdf/sections/section_servitudes.py
==================================
Section dédiée aux servitudes d'utilité publique (SUP) :
- carte servitudes (si disponible)
- tableau des attributs métier
- répartition UF et parcelle par parcelle (% + ha)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape as xml_escape

import geopandas as gpd
import pandas as pd
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import HRFlowable, Image, Paragraph, Spacer, Table, TableStyle
from shapely.ops import unary_union


C_GREEN = colors.HexColor("#2D6A4F")
C_LIGHT = colors.HexColor("#52B788")
C_BORDER = colors.HexColor("#B7D9C8")


def compute_servitudes_result(
    uf_gdf: gpd.GeoDataFrame,
    parcelle_results: List[Any],
    sup_gdfs: Dict[str, gpd.GeoDataFrame],
    intersections: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Contrat servitudes enrichi :
    - attributs : suptype, nomsuplitt, nomass, typeass
    - répartition UF (surface + %)
    - répartition parcelle par parcelle (surface + %)
    """
    result: Dict[str, Any] = {
        "intersecte": False,
        "attributs": [],
        "uf_repartition": [],
        "parcelles_repartition": [],
    }

    # Attributs lisibles depuis les intersections déjà filtrées.
    attrs_seen = set()
    attrs_rows: List[Dict[str, str]] = []
    for layer in intersections:
        if str(layer.get("article", "")).startswith("4") is False:
            continue
        for el in (layer.get("elements") or []):
            row = {
                "suptype": str(el.get("suptype") or "—").strip() or "—",
                "nomsuplitt": str(el.get("nomsuplitt") or "—").strip() or "—",
                "nomass": str(el.get("nomass") or "—").strip() or "—",
                "typeass": str(el.get("typeass") or "—").strip() or "—",
            }
            key = (row["suptype"], row["nomsuplitt"], row["nomass"], row["typeass"])
            if key in attrs_seen:
                continue
            attrs_seen.add(key)
            attrs_rows.append(row)
    result["attributs"] = attrs_rows

    # Répartition surfacique : uniquement sur couches polygonales.
    poly_parts: List[gpd.GeoDataFrame] = []
    for table_name, gdf in (sup_gdfs or {}).items():
        if gdf is None or gdf.empty:
            continue
        if table_name != "assiette_sup_s":
            continue
        if "geometry" not in gdf.columns:
            continue
        one = gdf.copy()
        for col in ("suptype", "nomsuplitt", "nomass", "typeass"):
            if col not in one.columns:
                one[col] = "—"
        poly_parts.append(one[["suptype", "nomsuplitt", "nomass", "typeass", "geometry"]])

    if not poly_parts:
        result["intersecte"] = len(attrs_rows) > 0
        return result

    sup_poly = gpd.GeoDataFrame(
        pd.concat(poly_parts, ignore_index=True),
        crs=poly_parts[0].crs,
    )
    sup_3857 = sup_poly.to_crs(3857)
    uf_union = unary_union(uf_gdf.to_crs(3857).geometry)
    uf_area_m2 = max(float(uf_union.area), 1.0)

    agg_uf: Dict[tuple, float] = {}
    for _, row in sup_3857.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        inter = geom.intersection(uf_union)
        area = float(inter.area) if not inter.is_empty else 0.0
        if area <= 0.0:
            continue
        key = (
            str(row.get("suptype") or "—"),
            str(row.get("nomsuplitt") or "—"),
            str(row.get("nomass") or "—"),
            str(row.get("typeass") or "—"),
        )
        agg_uf[key] = agg_uf.get(key, 0.0) + area

    uf_rows: List[Dict[str, Any]] = []
    for (suptype, nomsuplitt, nomass, typeass), area in agg_uf.items():
        uf_rows.append(
            {
                "suptype": suptype,
                "nomsuplitt": nomsuplitt,
                "nomass": nomass,
                "typeass": typeass,
                "surface_ha": round(area / 10_000.0, 4),
                "pct_uf": round((area / uf_area_m2) * 100.0, 2),
            }
        )
    uf_rows.sort(key=lambda r: (-r["pct_uf"], r["suptype"], r["nomsuplitt"]))
    result["uf_repartition"] = uf_rows

    parc_rows: List[Dict[str, Any]] = []
    for parc in [p for p in parcelle_results if getattr(p, "ok", False) and not p.gdf.empty]:
        parc_union = unary_union(parc.gdf.to_crs(3857).geometry)
        parc_area_m2 = max(float(parc_union.area), 1.0)
        agg_parc: Dict[tuple, float] = {}

        for _, row in sup_3857.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue
            inter = geom.intersection(parc_union)
            area = float(inter.area) if not inter.is_empty else 0.0
            if area <= 0.0:
                continue
            key = (
                str(row.get("suptype") or "—"),
                str(row.get("nomsuplitt") or "—"),
                str(row.get("nomass") or "—"),
                str(row.get("typeass") or "—"),
            )
            agg_parc[key] = agg_parc.get(key, 0.0) + area

        for (suptype, nomsuplitt, nomass, typeass), area in agg_parc.items():
            parc_rows.append(
                {
                    "parcelle_ref": parc.ref.label,
                    "suptype": suptype,
                    "nomsuplitt": nomsuplitt,
                    "nomass": nomass,
                    "typeass": typeass,
                    "surface_ha": round(area / 10_000.0, 4),
                    "pct_parcelle": round((area / parc_area_m2) * 100.0, 2),
                }
            )

    parc_rows.sort(key=lambda r: (r["parcelle_ref"], -r["pct_parcelle"], r["suptype"]))
    result["parcelles_repartition"] = parc_rows
    result["intersecte"] = len(attrs_rows) > 0 or len(uf_rows) > 0
    return result


def _styles() -> Dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    n = base["Normal"]
    return {
        "kicker": ParagraphStyle(
            "SupKickerSec", parent=n,
            fontSize=8, textColor=colors.HexColor("#6b7280"),
            fontName="Helvetica-Bold", spaceAfter=6, leading=10,
        ),
        "title": ParagraphStyle(
            "SupTitleSec", parent=n,
            fontSize=17, textColor=colors.HexColor("#1e4d2f"),
            fontName="Helvetica-Bold", spaceAfter=8, leading=22,
        ),
        "badge_ok": ParagraphStyle(
            "SupBadgeOk", parent=n,
            fontSize=10, textColor=colors.white,
            fontName="Helvetica-Bold", leading=13,
        ),
        "badge_no": ParagraphStyle(
            "SupBadgeNo", parent=n,
            fontSize=10, textColor=colors.HexColor("#374151"),
            fontName="Helvetica-Bold", leading=13,
        ),
        "tbl_hdr": ParagraphStyle(
            "SupTblHdrSec", parent=n,
            fontSize=8.2, textColor=colors.HexColor("#1e4d2f"),
            fontName="Helvetica-Bold", leading=10.5,
        ),
        "tbl_cell": ParagraphStyle(
            "SupTblCellSec", parent=n,
            fontSize=7.9, textColor=colors.HexColor("#1f2937"),
            fontName="Helvetica", leading=10.2,
        ),
        "body": ParagraphStyle(
            "SupBodySec", parent=n,
            fontSize=9, textColor=colors.HexColor("#374151"),
            fontName="Helvetica", leading=13, spaceAfter=4,
        ),
        "note": ParagraphStyle(
            "SupNoteSec", parent=n,
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


def build_servitudes_page_flowables(
    servitudes_map_png: Optional[str],
    servitudes_result: Dict[str, Any],
    table_width: float,
) -> List[Any]:
    st = _styles()
    tw = max(float(table_width), 120.0)
    intersecte = bool(servitudes_result.get("intersecte", False))
    attrs = servitudes_result.get("attributs") or []
    uf_rows = servitudes_result.get("uf_repartition") or []
    parc_rows = servitudes_result.get("parcelles_repartition") or []

    flow: List[Any] = []
    flow.append(Spacer(1, 0.4 * cm))
    flow.append(Paragraph("ARTICLE 4 — SERVITUDES D'UTILITÉ PUBLIQUE", st["kicker"]))
    flow.append(Paragraph("Servitudes impactant l'unité foncière", st["title"]))

    if intersecte:
        badge_text = f"✓  Servitudes détectées — {len(attrs)} enregistrement(s)"
        badge_tbl = Table([[Paragraph(xml_escape(badge_text), st["badge_ok"])]], colWidths=[tw], rowHeights=[26])
        badge_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), C_GREEN),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
    else:
        badge_text = "✗  Aucune servitude du Géoportail de l'Urbanisme n'intersecte l'unité foncière"
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

    if servitudes_map_png and Path(servitudes_map_png).is_file():
        img_w, img_h = _image_size(Path(servitudes_map_png), tw * 0.98)
        flow.append(Image(str(Path(servitudes_map_png)), width=img_w, height=img_h))

    if intersecte and attrs:
        ph = st["tbl_hdr"]
        pc = st["tbl_cell"]

        flow.append(Spacer(1, 14))
        flow.append(HRFlowable(width="100%", thickness=1, color=C_LIGHT))
        flow.append(Spacer(1, 10))
        flow.append(Paragraph("Attributs des servitudes", st["body"]))

        attr_rows: List[List[Any]] = [[
            Paragraph("Type servitude", ph),
            Paragraph("Désignation", ph),
            Paragraph("Nom", ph),
            Paragraph("Type", ph),
        ]]
        for r in attrs:
            attr_rows.append([
                Paragraph(xml_escape(str(r.get("suptype", "—"))), pc),
                Paragraph(xml_escape(str(r.get("nomsuplitt", "—"))), pc),
                Paragraph(xml_escape(str(r.get("nomass", "—"))), pc),
                Paragraph(xml_escape(str(r.get("typeass", "—"))), pc),
            ])
        attr_tbl = Table(attr_rows, colWidths=[tw * 0.18, tw * 0.32, tw * 0.30, tw * 0.20])
        attr_tbl.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, C_BORDER),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8F5EE")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7FCF9")]),
        ]))
        flow.append(attr_tbl)

        if uf_rows:
            flow.append(Spacer(1, 12))
            flow.append(Paragraph("Répartition surfacique sur l'unité foncière", st["body"]))
            uf_table_rows: List[List[Any]] = [[
                Paragraph("Type servitude", ph),
                Paragraph("Désignation", ph),
                Paragraph("% couverture UF", ph),
                Paragraph("Surface (ha)", ph),
            ]]
            for r in uf_rows:
                uf_table_rows.append([
                    Paragraph(xml_escape(str(r.get("suptype", "—"))), pc),
                    Paragraph(xml_escape(str(r.get("nomsuplitt", "—"))), pc),
                    Paragraph(xml_escape(str(f"{float(r.get('pct_uf', 0.0)):.2f} %").replace(".", ",")), pc),
                    Paragraph(xml_escape(str(f"{float(r.get('surface_ha', 0.0)):.4f}").replace(".", ",")), pc),
                ])
            uf_tbl = Table(uf_table_rows, colWidths=[tw * 0.20, tw * 0.46, tw * 0.17, tw * 0.17])
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
            flow.append(Paragraph("Répartition des servitudes parcelle par parcelle", st["body"]))
            parc_table_rows: List[List[Any]] = [[
                Paragraph("Parcelle", ph),
                Paragraph("Type servitude", ph),
                Paragraph("% parcelle", ph),
                Paragraph("Surface (ha)", ph),
            ]]
            for r in parc_rows:
                parc_table_rows.append([
                    Paragraph(xml_escape(str(r.get("parcelle_ref", "—"))), pc),
                    Paragraph(xml_escape(str(r.get("suptype", "—"))), pc),
                    Paragraph(xml_escape(str(f"{float(r.get('pct_parcelle', 0.0)):.2f} %").replace(".", ",")), pc),
                    Paragraph(xml_escape(str(f"{float(r.get('surface_ha', 0.0)):.4f}").replace(".", ",")), pc),
                ])
            parc_tbl = Table(parc_table_rows, colWidths=[tw * 0.22, tw * 0.43, tw * 0.17, tw * 0.18])
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
            "Aucune servitude d'utilité publique n'a été détectée sur cette unité foncière "
            "dans les données disponibles du Géoportail de l'Urbanisme.",
            st["body"],
        ))

    flow.append(Spacer(1, 8))
    flow.append(Paragraph(
        "Source : Géoportail de l'Urbanisme — couches SUP (assiette_sup_*). "
        "Données indicatives susceptibles d'évoluer.",
        st["note"],
    ))
    return flow
