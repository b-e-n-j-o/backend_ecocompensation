"""Section 4.1 — Résultats écologiques (carte + tableau attributs)."""

from __future__ import annotations

import json
import math
import xml.sax.saxutils as xml_esc
from io import BytesIO
from typing import Optional

import geopandas as gpd
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import contextily as ctx
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib.units import cm

from carte_parcelles.carte_parcelles import (
    FONCIER_EDGE,
    FONCIER_FACE,
    _add_north_arrow,
    _add_scalebar,
    couleur_eco,
)
from generer_rapport import pick_col

# Clé « virtuelle » pour colonne dérivée (pas un nom de colonne GeoDataFrame).
_COL_OCC_SOL = "__occ_sol_pdf__"
PAGE_SIZE = landscape(A4)


def gdf_sorted_eco_for_ranking(gdf_in: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Tri pour tableau + carte : score éco décroissant, puis distance espèce (m)
    croissante (``rayon_esp`` ; valeurs négatives ex. -1 en dernier).
    """
    if gdf_in is None or gdf_in.empty:
        return gdf_in
    df = gdf_in.copy()
    eco_col = pick_col(df, "score_eco", "eco_tot")
    df["_eco_sort"] = pd.to_numeric(df[eco_col], errors="coerce")
    if "rayon_esp" in df.columns:
        ray = pd.to_numeric(df["rayon_esp"], errors="coerce")
        df["_ray_sort"] = ray.where(ray >= 0, np.nan)
        out = df.sort_values(
            by=["_eco_sort", "_ray_sort"],
            ascending=[False, True],
            na_position="last",
        ).drop(columns=["_eco_sort", "_ray_sort"])
    else:
        out = df.sort_values(
            by="_eco_sort", ascending=False, na_position="last",
        ).drop(columns=["_eco_sort"])
    return out.reset_index(drop=True)


def generer_image_carte_eco(
    gdf_2154: gpd.GeoDataFrame,
    *,
    foncier_projet: Optional[gpd.GeoDataFrame] = None,
    buffer_m: int = 800,
) -> BytesIO:
    """Carte 4.1 : remplissage éco uniquement (sans contours dureté)."""
    import matplotlib.patheffects as pe

    def _fit_bounds_to_aspect(
        xlim_in: tuple[float, float],
        ylim_in: tuple[float, float],
        target_aspect: float,
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        """Ajuste l'emprise pour matcher le ratio paysage cible (x/y) sans rogner."""
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

    gdf_2154 = gdf_sorted_eco_for_ranking(gdf_2154)
    gdf = gdf_2154.to_crs(3857)
    bounds_rows = [gdf.total_bounds]
    if foncier_projet is not None and not foncier_projet.empty:
        bounds_rows.append(foncier_projet.to_crs(3857).total_bounds)

    bmat = np.array(bounds_rows)
    bounds = np.array([bmat[:, 0].min(), bmat[:, 1].min(), bmat[:, 2].max(), bmat[:, 3].max()])
    dx = bounds[2] - bounds[0]
    dy = bounds[3] - bounds[1]
    # Zoom plus serré : petite marge relative (8 %) avec garde-fous mini/maxi.
    # `buffer_m` est conservé comme plafond optionnel pour éviter une marge excessive.
    span = max(dx, dy)
    buf = max(80.0, span * 0.08)
    buf = min(buf, 450.0)
    if isinstance(buffer_m, (int, float)) and buffer_m > 0:
        buf = min(buf, float(buffer_m))
    fig_w_in = (PAGE_SIZE[0] - 1.5 * cm * 2) / 28.35
    fig_h_in = (PAGE_SIZE[1] - 4.5 * cm) / 28.35
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
            linewidth=2.0, alpha=0.55, zorder=2,
        )

    eco_col = pick_col(gdf, "score_eco", "eco_tot")
    for _, row in gdf.iterrows():
        face, _ = couleur_eco(row[eco_col])
        geom_series = gpd.GeoSeries([row.geometry], crs=gdf.crs)
        geom_series.plot(ax=ax, facecolor=face, edgecolor="#666666",
                         linewidth=0.8, alpha=0.82, zorder=4)

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


def _couleur_score_eco(score: float) -> tuple[str, str]:
    """Retourne (couleur_fond, couleur_texte) selon le score éco /6."""
    if score >= 4:
        return "#2D6A4F", "#FFFFFF"   # vert
    if score <= 1:
        return "#B7B7B7", "#333333"  # gris
    return "#F4A261", "#FFFFFF"      # orange (2 à 3)


# Fond score tableau : opacité 100 % + texte blanc (comme colonnes éco/dureté du tableau composite).
_SCORE_CELL_TEXT = HexColor("#FFFFFF")


def _table_score_cell_fill(hex6: str) -> HexColor:
    h = (hex6 or "#CCCCCC").lstrip("#")
    if len(h) != 6:
        return HexColor("#CCCCCC")
    return HexColor("#" + h)


def _is_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    t = str(value).strip()
    return t == "" or t.lower() in ("nan", "null", "none")


def _format_eco_table_cell(col: str, value) -> str:
    """Texte brut pour cellules courtes (centrées)."""
    if _is_blank(value):
        return "—"
    text = str(value).strip()

    if col == "rang":
        try:
            return str(int(round(float(value))))
        except Exception:
            return text

    if col in {"surf_ha", "miller", "dist_km"}:
        try:
            return f"{float(value):.2f}".replace(".", ",")
        except Exception:
            return text

    if col in {"score_eco", "eco_tot"}:
        try:
            return str(int(round(float(value))))
        except Exception:
            return text

    if col == "rayon_esp":
        try:
            m = float(value)
        except Exception:
            return text
        if m < 0:
            return "—"
        return f"{int(round(m))}"

    if col in {"code_insee", "cinsee", "section", "numero", "idu"}:
        return text

    return text


def _occ_sol_dict(value) -> dict | None:
    if _is_blank(value):
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s or s.lower() in ("null", "none"):
            return None
        if s.startswith("{"):
            try:
                return json.loads(s)
            except Exception:
                return None
    return None


def _format_occ_sol_paragraph(value) -> str:
    """
    Occupation du sol : libellés avec part ≥ 2 % sur la parcelle, tri décroissant.
    Sortie mini-HTML pour ReportLab Paragraph.
    """
    d = _occ_sol_dict(value)
    if not d:
        return "—"
    items: list[tuple[float, str]] = []
    for label, raw_v in d.items():
        try:
            fv = float(raw_v)
        except Exception:
            continue
        if fv >= 0.02:
            items.append((fv, str(label)))
    items.sort(key=lambda x: -x[0])
    if not items:
        return "—"
    lines = [f"{xml_esc.escape(lab)} — {100 * fv:.0f} %" for fv, lab in items]
    return "<br/>".join(lines)


def _eco_table_specs(gdf: gpd.GeoDataFrame) -> tuple[list[tuple[str, str]], str]:
    """
    Liste (clé_colonne_ou_virtuelle, libellé en-tête) et nom de la colonne score éco
    (pour coloration).
    """
    eco_col = pick_col(gdf, "score_eco", "eco_tot")
    specs: list[tuple[str, str]] = [
        ("rang", "Classement"),
        ("idu", "IDU"),
    ]
    if "code_insee" in gdf.columns:
        specs.append(("code_insee", "Code INSEE"))
    elif "cinsee" in gdf.columns:
        specs.append(("cinsee", "Code INSEE"))
    specs.extend(
        [
            ("section", "Section"),
            ("numero", "Numéro"),
            ("surf_ha", "Surface (ha)"),
            ("miller", "Miller"),
            ("dist_km", "Dist. projet (km)"),
        ]
    )
    if "rayon_esp" in gdf.columns:
        specs.append(("rayon_esp", "Dist espèce (m)"))
    if "espece_esp" in gdf.columns:
        specs.append(("espece_esp", "Espèce"))
    if "occ_sol" in gdf.columns:
        specs.append((_COL_OCC_SOL, "Occupation du sol"))
    # Score éco en dernière colonne (droite du tableau)
    specs.append((eco_col, "Score éco"))
    return specs, eco_col


def _eco_col_weights(specs: list[tuple[str, str]], eco_col: str) -> list[float]:
    w: list[float] = []
    for key, _ in specs:
        if key == eco_col:
            w.append(0.95)
        elif key == _COL_OCC_SOL:
            w.append(1.85)
        elif key == "idu":
            w.append(1.05)
        elif key == "espece_esp":
            w.append(1.2)
        elif key in {"section", "numero", "code_insee", "cinsee"}:
            w.append(0.72)
        else:
            w.append(0.82)
    return w


def _tableau_resultats_eco(gdf: gpd.GeoDataFrame, styles: dict, width_pt: float) -> Table:
    gdf = gdf_sorted_eco_for_ranking(gdf)
    specs, eco_col = _eco_table_specs(gdf)

    td_small = ParagraphStyle(
        "td_small_eco",
        fontSize=6.6,
        fontName="Helvetica",
        textColor=HexColor("#2D3436"),
        alignment=TA_CENTER,
        leading=8.2,
    )
    td_score_eco = ParagraphStyle(
        "td_score_eco",
        parent=td_small,
        fontName="Helvetica-Bold",
        textColor=_SCORE_CELL_TEXT,
    )
    td_occ = ParagraphStyle(
        "td_occ_eco",
        fontSize=5.8,
        fontName="Helvetica",
        textColor=HexColor("#2D3436"),
        alignment=TA_LEFT,
        leading=7.0,
    )
    rows: list[list[Paragraph]] = [
        [Paragraph(h, styles["th"]) for _k, h in specs],
    ]

    for row_pos, (_, row) in enumerate(gdf.iterrows(), start=1):
        row_cells: list[Paragraph] = []
        for key, _h in specs:
            if key == _COL_OCC_SOL:
                row_cells.append(Paragraph(_format_occ_sol_paragraph(row.get("occ_sol")), td_occ))
            elif key == eco_col:
                row_cells.append(Paragraph(_format_eco_table_cell(key, row.get(key)), td_score_eco))
            elif key == "rang":
                row_cells.append(Paragraph(str(row_pos), td_small))
            else:
                row_cells.append(Paragraph(_format_eco_table_cell(key, row.get(key)), td_small))
        rows.append(row_cells)

    weights = _eco_col_weights(specs, eco_col)
    sw = sum(weights) or 1.0
    col_widths = [w * width_pt / sw for w in weights]

    style_cmds: list[tuple] = [
        ("BACKGROUND", (0, 0), (-1, 0), HexColor("#1B4332")),
        ("GRID", (0, 0), (-1, -1), 0.3, HexColor("#CCCCCC")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
        ("VALIGN", (0, 1), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [HexColor("#D8F3DC"), HexColor("#FFFFFF")]),
    ]

    eco_col_idx = next(i for i, (k, _) in enumerate(specs) if k == eco_col)
    for i, v in enumerate(gdf[eco_col].tolist(), start=1):
        try:
            face, _txt_unused = _couleur_score_eco(float(v))
        except Exception:
            face = "#B7B7B7"
        style_cmds.extend(
            [
                ("BACKGROUND", (eco_col_idx, i), (eco_col_idx, i), _table_score_cell_fill(face)),
                ("TEXTCOLOR", (eco_col_idx, i), (eco_col_idx, i), _SCORE_CELL_TEXT),
                ("FONTNAME", (eco_col_idx, i), (eco_col_idx, i), "Helvetica-Bold"),
            ]
        )

    table = Table(rows, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle(style_cmds))
    return table


def append_section_resultats_eco(
    story,
    styles: dict,
    width_pt: float,
    gdf: Optional[gpd.GeoDataFrame],
    img_eco_buf,
    buf_to_rl_image,
    placeholder_carte,
) -> None:
    """Ajoute la section 4.1 au story ReportLab."""
    title = Table(
        [[Paragraph("4.1  Résultats écologiques — Attributs des parcelles",
                    ParagraphStyle("h_eco_41", fontSize=10, fontName="Helvetica-Bold",
                                   textColor=HexColor("#FFFFFF")))]],
        colWidths=[width_pt],
    )
    title.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), HexColor("#2D6A4F")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(title)
    story.append(Spacer(1, 0.3 * cm))

    nb = len(gdf) if gdf is not None else "—"
    story.append(Paragraph(
        f"L'analyse écologique met en évidence <b>{nb} parcelles</b>. "
        "Le tableau reprend les attributs d'export, l'occupation du sol (parts ≥ 2 %), "
        "l'espèce concernée, la distance espèce (m) et le score écologique (dernière colonne). "
        "Tri des lignes : score éco décroissant, puis distance d'observation d'espèce croissante "
        "(chiffre sur la carte = rang dans ce tri). La colonne « Classement » conserve le rang "
        "issu du filtrage initial.",
        styles["body"],
    ))
    story.append(Spacer(1, 0.3 * cm))

    if img_eco_buf is not None:
        # Hauteur contrainte pour garantir "titre + texte + carte" sur la même page.
        carte_h = 8.6 * cm
        story.append(buf_to_rl_image(img_eco_buf, width=width_pt, max_height=carte_h))
    else:
        story.append(placeholder_carte(width_pt, 7 * cm,
                                       "CARTE ECOLOGIQUE — Parcelles (remplissage score éco)"))
    story.append(Paragraph(
        "Fig. 2. Carte écologique des parcelles retenues — chiffre = rang "
        "(score éco puis proximité espèce)",
        styles["legende"],
    ))
    story.append(Spacer(1, 0.2 * cm))

    if gdf is not None and not gdf.empty:
        story.append(PageBreak())
        story.append(_tableau_resultats_eco(gdf, styles, width_pt))
        story.append(Paragraph(
            "Tabl. 1. Attributs des parcelles exportées et score écologique",
            styles["legende"],
        ))

