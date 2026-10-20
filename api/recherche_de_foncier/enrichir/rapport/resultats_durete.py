"""Sections 4.2 / 4.3 — Dureté foncière et synthèse."""

from __future__ import annotations

from io import BytesIO
import xml.sax.saxutils as xml_esc
from typing import Optional

import geopandas as gpd
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import contextily as ctx
from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph, Spacer, Table, TableStyle, PageBreak

from carte_parcelles.carte_parcelles import (
    FONCIER_EDGE,
    FONCIER_FACE,
    _add_north_arrow,
    _add_scalebar,
    couleur_durete,
)
from generer_rapport import pick_col
from resultats_eco import gdf_sorted_eco_for_ranking

_SCORE_CELL_TEXT = HexColor("#FFFFFF")
PAGE_SIZE = landscape(A4)


def _table_score_cell_fill(hex6: str) -> HexColor:
    h = (hex6 or "#CCCCCC").lstrip("#")
    if len(h) != 6:
        return HexColor("#CCCCCC")
    return HexColor("#" + h)


def gdf_sorted_durete_for_ranking(gdf_in: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Tri pour tableau + carte : score de dureté croissant (plus bas = foncier plus favorable),
    puis distance au projet (km) croissante. Scores dureté non valides (≤ 0, ex. propriétaire
    privé) en dernier.
    """
    if gdf_in is None or gdf_in.empty:
        return gdf_in
    df = gdf_in.copy()
    dur_col = pick_col(df, "score_dur", "dur_tot")
    du = pd.to_numeric(df[dur_col], errors="coerce")
    df["_du_sort"] = du.where(du > 0, np.nan)
    if "dist_km" in df.columns:
        df["_dk_sort"] = pd.to_numeric(df["dist_km"], errors="coerce").fillna(1e9)
        out = df.sort_values(
            by=["_du_sort", "_dk_sort"],
            ascending=[True, True],
            na_position="last",
        ).drop(columns=["_du_sort", "_dk_sort"])
    else:
        out = df.sort_values(
            by="_du_sort", ascending=True, na_position="last",
        ).drop(columns=["_du_sort"])
    return out.reset_index(drop=True)


def generer_image_carte_durete(
    gdf_2154: gpd.GeoDataFrame,
    *,
    foncier_projet: Optional[gpd.GeoDataFrame] = None,
    buffer_m: int = 800,
) -> BytesIO:
    """Carte 4.2 : contours colorés par dureté (sans remplissage éco)."""
    import matplotlib.patheffects as pe

    def _fit_bounds_to_aspect(
        xlim_in: tuple[float, float],
        ylim_in: tuple[float, float],
        target_aspect: float,
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        x0, x1 = xlim_in
        y0, y1 = ylim_in
        wx = max(1.0, x1 - x0)
        wy = max(1.0, y1 - y0)
        cx = (x0 + x1) / 2.0
        cy = (y0 + y1) / 2.0
        cur = wx / wy
        if cur < target_aspect:
            wx = wy * target_aspect
        else:
            wy = wx / target_aspect
        return (cx - wx / 2.0, cx + wx / 2.0), (cy - wy / 2.0, cy + wy / 2.0)

    gdf_2154 = gdf_sorted_durete_for_ranking(gdf_2154)
    gdf = gdf_2154.to_crs(3857)
    bounds_rows = [gdf.total_bounds]
    if foncier_projet is not None and not foncier_projet.empty:
        bounds_rows.append(foncier_projet.to_crs(3857).total_bounds)

    bmat = np.array(bounds_rows)
    bounds = np.array([bmat[:, 0].min(), bmat[:, 1].min(), bmat[:, 2].max(), bmat[:, 3].max()])
    dx = bounds[2] - bounds[0]
    dy = bounds[3] - bounds[1]
    buf = max(buffer_m * 3.5, max(dx, dy) * 0.55)
    fig_w_in = (PAGE_SIZE[0] - 1.5 * 2 * 28.3465 / 28.35) / 28.35
    fig_h_in = (PAGE_SIZE[1] - 4.5 * 28.3465 / 28.35) / 28.35
    xlim = (bounds[0] - buf, bounds[2] + buf)
    ylim = (bounds[1] - buf, bounds[3] + buf)
    xlim, ylim = _fit_bounds_to_aspect(xlim, ylim, target_aspect=fig_w_in / fig_h_in)
    dpi = 220
    fig, ax = plt.subplots(figsize=(fig_w_in, fig_h_in), dpi=dpi)
    ax.set_xlim(xlim)
    ax.set_ylim(ylim)
    ax.set_aspect("equal")

    try:
        ctx.add_basemap(ax, crs=gdf.crs.to_string(),
                        source=ctx.providers.CartoDB.Positron,
                        zoom="auto", attribution=False)
    except Exception:
        ax.set_facecolor("#f0ede8")

    if foncier_projet is not None and not foncier_projet.empty:
        foncier_projet.to_crs(3857).plot(
            ax=ax, facecolor=FONCIER_FACE, edgecolor=FONCIER_EDGE,
            linewidth=2.0, alpha=0.45, zorder=2,
        )

    dur_col = pick_col(gdf, "score_dur", "dur_tot")
    has_pm_col = "p_morale" in gdf.columns
    for _, row in gdf.iterrows():
        non_pm = has_pm_col and not _is_personne_morale(row.get("p_morale"))
        if non_pm:
            edge, dashed = "#B7B7B7", True
        else:
            edge, dashed, _ = couleur_durete(row[dur_col])
        linestyle = (0, (4, 3)) if dashed else "solid"
        gpd.GeoSeries([row.geometry], crs=gdf.crs).plot(
            ax=ax, facecolor="none", edgecolor=edge,
            linewidth=2.6, linestyle=linestyle, alpha=1.0, zorder=4,
        )

    for rank, (_, row) in enumerate(gdf.iterrows(), start=1):
        c = row.geometry.centroid
        ax.text(
            c.x,
            c.y,
            str(rank),
            fontsize=10.5,
            ha="center",
            va="center",
            fontweight="bold",
            color="#152028",
            zorder=6,
            path_effects=[pe.withStroke(linewidth=3.2, foreground="white")],
        )

    _add_scalebar(ax, xlim, ylim)
    _add_north_arrow(ax, xlim, ylim)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_edgecolor("#AAAAAA")
        spine.set_linewidth(0.8)

    plt.tight_layout(pad=0.3)
    buf_io = BytesIO()
    fig.savefig(buf_io, format="png", dpi=dpi, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf_io.seek(0)
    return buf_io


def _fmt_p_morale(v) -> str:
    """Colonne binaire export : T / F (ou équivalents booléens)."""
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "T" if v else "F"
    t = str(v).strip()
    if t == "" or t.lower() == "nan":
        return "—"
    u = t.upper()
    if u in ("T", "TRUE", "1", "Y", "OUI", "YES"):
        return "T"
    if u in ("F", "FALSE", "0", "N", "NON", "NO"):
        return "F"
    return t[:1].upper() if len(t) == 1 else t


def _is_personne_morale(v) -> bool:
    """True si l'export indique une personne morale (T / true / 1 / etc.)."""
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    t = str(v).strip()
    if t == "" or t.lower() == "nan":
        return False
    u = t.upper()
    return u in ("T", "TRUE", "1", "Y", "OUI", "YES")


def _fmt_txt_dure_paragraph(value) -> str:
    """Justification dureté : retours ligne → <br/>, XML échappé (texte long GPKG / CSV)."""
    if value is None:
        return "—"
    raw = str(value).strip()
    if raw == "" or raw.lower() == "nan":
        return "—"
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    parts = raw.split("\n")
    return "<br/>".join(xml_esc.escape(p) for p in parts)


def _durete_col_weight(col: str, dur_col: str) -> float:
    if col == "txt_dure":
        return 2.7
    if col == dur_col:
        return 0.9
    if col == "idu":
        return 1.05
    if col in ("p_morale", "siren", "section", "numero"):
        return 0.62
    if col in ("pm_denom", "pm_forme"):
        return 1.05
    return 0.82


def _tableau_durete_rl(gdf: gpd.GeoDataFrame, styles: dict, width_pt: float) -> Table:
    gdf = gdf_sorted_durete_for_ranking(gdf)
    dur_col = pick_col(gdf, "score_dur", "dur_tot")
    # Justification puis score dureté en dernière colonne (droite)
    selected_cols = [
        "idu",
        "p_morale",
        "siren",
        "pm_denom",
        "pm_forme",
        "section",
        "numero",
        "surf_ha",
        "dist_km",
        dur_col,
    ]
    cols = [c for c in selected_cols if c in gdf.columns]
    labels = {
        "idu": "Référence (IDU)",
        "p_morale": "Pers. morale",
        "siren": "SIREN",
        "pm_denom": "Dénomination PM",
        "pm_forme": "Forme juridique PM",
        "section": "Section",
        "numero": "Numéro",
        "surf_ha": "Surface (ha)",
        "dist_km": "Dist. projet (km)",
        "score_dur": "Score dureté",
        "dur_tot": "Score dureté",
        "txt_dure": "Justification dureté",
    }
    rows = [[Paragraph(labels.get(c, c), styles["th"]) for c in cols]]
    td_small = ParagraphStyle(
        "td_small_dur", fontSize=6.4, fontName="Helvetica",
        textColor=HexColor("#2D3436"), alignment=1, leading=8.0
    )
    td_score_dur = ParagraphStyle(
        "td_score_dur",
        parent=td_small,
        fontName="Helvetica-Bold",
        textColor=_SCORE_CELL_TEXT,
    )
    td_score_dur_nonpm = ParagraphStyle(
        "td_score_dur_nonpm",
        parent=td_small,
        fontName="Helvetica-Bold",
        textColor=HexColor("#333333"),
    )

    def _fmt(col, v):
        if v is None:
            return "—"
        t = str(v).strip()
        if t == "" or t.lower() == "nan":
            return "—"
        if col == "p_morale":
            return _fmt_p_morale(v)
        if col == "siren":
            return t if t else "—"
        if col in {"surf_ha", "dist_km"}:
            try:
                return f"{float(v):.2f}".replace(".", ",")
            except Exception:
                return t
        if col in {"score_dur", "dur_tot"}:
            try:
                return str(int(round(float(v))))
            except Exception:
                return t
        return t

    has_pm_col = "p_morale" in gdf.columns
    for _, row in gdf.iterrows():
        row_cells: list[Paragraph] = []
        for c in cols:
            if c == dur_col:
                pm = _is_personne_morale(row.get("p_morale")) if has_pm_col else True
                row_cells.append(
                    Paragraph(_fmt(c, row.get(c)), td_score_dur if pm else td_score_dur_nonpm)
                )
            else:
                row_cells.append(Paragraph(_fmt(c, row.get(c)), td_small))
        rows.append(row_cells)

    wts = [_durete_col_weight(c, dur_col) for c in cols]
    sw = sum(wts) or 1.0
    col_widths = [w * width_pt / sw for w in wts]
    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), HexColor("#1B4332")),
        ("GRID", (0, 0), (-1, -1), 0.3, HexColor("#CCCCCC")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
        ("VALIGN", (0, 1), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [HexColor("#D8F3DC"), HexColor("#FFFFFF")]),
    ]

    if dur_col in cols:
        idx_col = cols.index(dur_col)
        for i, (_, row) in enumerate(gdf.iterrows(), start=1):
            v = row[dur_col]
            non_pm = has_pm_col and not _is_personne_morale(row.get("p_morale"))
            if non_pm:
                edge = "#B7B7B7"
                txt = HexColor("#333333")
            else:
                try:
                    edge, _, _ = couleur_durete(float(v))
                except Exception:
                    edge = "#AAAAAA"
                txt = _SCORE_CELL_TEXT
            style_cmds.extend([
                ("BACKGROUND", (idx_col, i), (idx_col, i), _table_score_cell_fill(edge)),
                ("TEXTCOLOR", (idx_col, i), (idx_col, i), txt),
                ("FONTNAME", (idx_col, i), (idx_col, i), "Helvetica-Bold"),
            ])

    t = Table(rows, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle(style_cmds))
    return t


def _tableau_synthese_rl(gdf: gpd.GeoDataFrame, styles: dict, width_pt: float) -> Table:
    from carte_parcelles.carte_parcelles import couleur_eco, couleur_durete

    cmp_col = pick_col(gdf, "score_comp", "cmp_tot")
    eco_col = pick_col(gdf, "score_eco", "eco_tot")
    dur_col = pick_col(gdf, "score_dur", "dur_tot")

    gdf_s = gdf_sorted_eco_for_ranking(gdf)

    selected_cols = [
        "rang",
        "idu",
        "siren",
        "pm_denom",
        "section",
        "numero",
        "surf_ha",
        "score_eco_cell",
        "score_comp_cell",
        "durete_cell",
    ]
    cols = [
        c for c in selected_cols
        if c in {"rang", "idu", "surf_ha", "score_eco_cell", "score_comp_cell", "durete_cell"}
        or c in gdf_s.columns
    ]
    labels = {
        "rang": "N°",
        "idu": "IDU",
        "siren": "SIREN",
        "pm_denom": "Dénomination PM",
        "section": "Section",
        "numero": "Numéro",
        "surf_ha": "Surface (ha)",
        "score_eco_cell": "Score éco",
        "score_comp_cell": "Score composite",
        "durete_cell": "Score dureté",
    }
    col_weights = {
        "rang": 0.55,
        "idu": 1.6,
        "siren": 1.15,
        "pm_denom": 2.2,
        "section": 0.75,
        "numero": 0.8,
        "surf_ha": 1.0,
        "score_eco_cell": 1.0,
        "score_comp_cell": 1.05,
        "durete_cell": 0.9,
    }
    sum_w = sum(col_weights.get(c, 1.0) for c in cols) or 1.0
    col_w = [width_pt * col_weights.get(c, 1.0) / sum_w for c in cols]
    header = [Paragraph(labels[c], styles["th"]) for c in cols]
    rows = [header]
    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), HexColor("#1B4332")),
        ("GRID", (0, 0), (-1, -1), 0.3, HexColor("#CCCCCC")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]

    sty_dur_synth_pm = ParagraphStyle(
        "dur_synth_pm", fontSize=7.5, fontName="Helvetica-Bold",
        textColor=HexColor("#FFFFFF"), alignment=1,
    )
    sty_dur_synth_nonpm = ParagraphStyle(
        "dur_synth_nonpm", fontSize=7.5, fontName="Helvetica-Bold",
        textColor=HexColor("#333333"), alignment=1,
    )

    has_pm_col = "p_morale" in gdf_s.columns
    for i, (idx, row) in enumerate(gdf_s.iterrows()):
        rang = str(i + 1)
        eco_v = float(row[eco_col])
        cmp_v = float(row[cmp_col])
        dur_v = float(row[dur_col])
        face_eco, _ = couleur_eco(eco_v)
        is_pm = _is_personne_morale(row.get("p_morale")) if has_pm_col else True
        if is_pm:
            edge_dur, _, _ = couleur_durete(dur_v)
        else:
            edge_dur = "#B7B7B7"

        if cmp_v <= 0:
            face_cmp, txt_cmp_hex = "#EEEEEE", "#666666"
            cmp_txt = "N/A"
        else:
            cmp_txt = f"{cmp_v:.1f}/100".replace(".", ",")
            if cmp_v >= 55:
                face_cmp, txt_cmp_hex = "#D8F3DC", "#1B4332"
            elif cmp_v >= 40:
                face_cmp, txt_cmp_hex = "#FFF3E0", "#A04000"
            else:
                face_cmp, txt_cmp_hex = "#F5F5F5", "#555555"

        row_values = {
            "rang": Paragraph(rang, ParagraphStyle("rng2", fontSize=7.5, fontName="Helvetica-Bold", textColor=HexColor("#2D3436"), alignment=1)),
            "idu": Paragraph(row["idu"], ParagraphStyle("idu2", fontSize=6.5, fontName="Helvetica", textColor=HexColor("#2D3436"), alignment=1)),
            "siren": Paragraph(str(row.get("siren") or "—"), ParagraphStyle("sir2", fontSize=6.3, fontName="Helvetica", textColor=HexColor("#2D3436"), alignment=1)),
            "pm_denom": Paragraph(str(row.get("pm_denom") or "—"), ParagraphStyle("pmd2", fontSize=6.1, fontName="Helvetica", textColor=HexColor("#2D3436"), alignment=0, leading=7.5)),
            "section": Paragraph(str(row.get("section") or "—"), ParagraphStyle("sec2", fontSize=6.3, fontName="Helvetica", textColor=HexColor("#2D3436"), alignment=1)),
            "numero": Paragraph(str(row.get("numero") or "—"), ParagraphStyle("num2", fontSize=6.3, fontName="Helvetica", textColor=HexColor("#2D3436"), alignment=1)),
            "surf_ha": Paragraph(f"{row['surf_ha']:.2f} ha", styles["td"]),
            "score_eco_cell": Paragraph(f"{int(eco_v)}/{int(row['eco_max'])}", ParagraphStyle("eco2", fontSize=7.5, fontName="Helvetica-Bold", textColor=HexColor("#FFFFFF"), alignment=1)),
            "score_comp_cell": Paragraph(
                cmp_txt,
                ParagraphStyle(
                    "cmp2",
                    fontSize=7.5,
                    fontName="Helvetica-Bold",
                    textColor=HexColor(txt_cmp_hex),
                    alignment=1,
                ),
            ),
            "durete_cell": Paragraph(
                f"{int(dur_v)}" if dur_v > 0 else "N/A",
                sty_dur_synth_pm if is_pm else sty_dur_synth_nonpm,
            ),
        }
        rows.append([row_values[c] for c in cols])
        bg = HexColor("#D8F3DC") if i % 2 == 0 else HexColor("#FFFFFF")
        r = i + 1
        idx = {c: cols.index(c) for c in cols}
        for c in cols:
            if c in {"score_eco_cell", "score_comp_cell", "durete_cell"}:
                continue
            style_cmds.append(("BACKGROUND", (idx[c], r), (idx[c], r), bg))
        style_cmds.append(("BACKGROUND", (idx["score_eco_cell"], r), (idx["score_eco_cell"], r), HexColor(face_eco)))
        if "score_comp_cell" in idx:
            style_cmds.append(("BACKGROUND", (idx["score_comp_cell"], r), (idx["score_comp_cell"], r), HexColor(face_cmp)))
        style_cmds.append(("BACKGROUND", (idx["durete_cell"], r), (idx["durete_cell"], r), HexColor(edge_dur)))

    t = Table(rows, colWidths=col_w, repeatRows=1)
    t.setStyle(TableStyle(style_cmds))
    return t


def append_sections_resultats_durete(
    story,
    styles: dict,
    width_pt: float,
    gdf: Optional[gpd.GeoDataFrame],
    img_durete_buf,
    img_parcelles_buf,
    buf_to_rl_image,
    placeholder_carte,
) -> None:
    """Ajoute les sections 4.2 et 4.3 au story ReportLab."""
    from reportlab.lib.units import cm

    title_42 = Table(
        [[Paragraph("4.2  Analyse de la dureté foncière (à titre expérimental)",
                    ParagraphStyle("h_dur_42", fontSize=10, fontName="Helvetica-Bold",
                                   textColor=HexColor("#FFFFFF")))]],
        colWidths=[width_pt],
    )
    title_42.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), HexColor("#2D6A4F")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(title_42)
    story.append(Spacer(1, 0.3 * cm))
    story.append(Paragraph(
        "L'analyse de la dureté foncière a été réalisée pour les unités "
        "appartenant à des personnes morales afin d'anticiper les contraintes "
        "liées à l'acquisition ou à la mobilisation des terrains. "
        "Tableau et carte : tri par score de dureté croissant (score bas = foncier plus favorable), "
        "puis distance au projet croissante (chiffre sur la carte = rang).",
        styles["body"]))
    story.append(Spacer(1, 0.2 * cm))
    story.append(Paragraph(
        "<i>Note : Cette analyse ne peut être étendue aux propriétaires privés "
        "en raison des limitations des données publiques disponibles.</i>",
        styles["note"]))
    story.append(Spacer(1, 0.3 * cm))
    if img_durete_buf is not None:
        from reportlab.lib.units import cm
        carte_h = PAGE_SIZE[1] - 2.4 * cm - 3 * cm - 2 * cm
        story.append(buf_to_rl_image(img_durete_buf, width=width_pt, max_height=carte_h))
    else:
        story.append(placeholder_carte(width_pt, 6 * cm,
                                       "CARTE — Dureté foncière (personnes morales uniquement)"))
    story.append(Paragraph(
        "Fig. 3. Dureté foncière des parcelles retenues — chiffre = rang "
        "(dureté croissante puis distance au projet)",
        styles["legende"]))
    if gdf is not None and not gdf.empty:
        story.append(PageBreak())
        story.append(_tableau_durete_rl(gdf, styles, width_pt))
        story.append(Paragraph(
            "Tabl. 2. Attributs de dureté foncière des unités retenues",
            styles["legende"]))
    story.append(PageBreak())

    title_43 = Table(
        [[Paragraph("4.3  Synthèse — Scoring écologique et dureté foncière",
                    ParagraphStyle("h_dur_43", fontSize=10, fontName="Helvetica-Bold",
                                   textColor=HexColor("#FFFFFF")))]],
        colWidths=[width_pt],
    )
    title_43.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), HexColor("#2D6A4F")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(title_43)
    story.append(Spacer(1, 0.3 * cm))
    story.append(Paragraph(
        "La carte et le tableau ci-dessous croisent le potentiel écologique et la dureté "
        "foncière de chaque unité foncière pour prioriser les démarches d'animation foncière. "
        "Les lignes du tableau sont ordonnées comme en section 4.1 : score écologique décroissant, "
        "puis distance d'observation d'espèce croissante lorsque cette donnée est disponible.",
        styles["body"]))
    story.append(Spacer(1, 0.3 * cm))

    # La carte Fig.2 est déplacée ici (depuis 4.1)
    if img_parcelles_buf is not None:
        carte_h = PAGE_SIZE[1] - 2.4 * cm - 3 * cm - 2 * cm
        story.append(buf_to_rl_image(img_parcelles_buf, width=width_pt, max_height=carte_h))
    else:
        story.append(placeholder_carte(width_pt, 7 * cm,
                                       "CARTE DES PARCELLES — Score écologique (remplissage) × Dureté foncière (contour)"))
    story.append(Paragraph(
        "Fig. 2. Localisation et scoring des unités foncières retenues",
        styles["legende"]))
    story.append(Spacer(1, 0.2 * cm))

    if gdf is not None and not gdf.empty:
        story.append(_tableau_synthese_rl(gdf, styles, width_pt))
        story.append(Paragraph(
            "Tabl. 3. Synthèse — Potentiel écologique × Dureté foncière",
            styles["legende"]))
    story.append(PageBreak())

