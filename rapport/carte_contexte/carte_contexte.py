"""
Module : Génération de la carte de contexte du projet (page 1 du rapport)
--------------------------------------------------------------------------
Affiche sur une page A4 :
  - Fond de carte (CartoDB Positron)
  - Périmètre foncier (polygone rose — ecocompensation.foncier), zoomé sur le site
  - L'AOI / buffer n'est pas dessinée sur la carte (souvent trop large) ; le buffer reste dans le tableau
  - Informations du projet : surface, commune, buffer utilisé

Input  : foncier_gdf (GeoDataFrame EPSG:2154), aoi_gdf (GeoDataFrame EPSG:2154)
         ou IDs Supabase pour les charger automatiquement
Output : BytesIO PNG (image carte) + PDF A4 une page

Usage standalone :
    python carte_contexte.py
Ou depuis l'orchestrateur :
    from carte_contexte import generer_page_contexte_pdf, charger_foncier_et_aoi
"""

import os
import sys
import warnings
from io import BytesIO
from pathlib import Path
from typing import Optional, Tuple
import tempfile

warnings.filterwarnings("ignore")

import numpy as np
import geopandas as gpd
from shapely.ops import unary_union
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.patheffects as pe
from matplotlib.lines import Line2D
import contextily as ctx
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import cm
from reportlab.lib.colors import HexColor
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.platypus import (
    SimpleDocTemplate, Image as RLImage, Spacer, Paragraph, Table, TableStyle
)

# ── Chemin backend pour les imports DB ──────────────────────────────────────
_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent

# ── Palette ─────────────────────────────────────────────────────────────────
VERT_FONCE   = "#1B4332"
FONCIER_FACE = "#f9a8d4"   # rose clair — emprise foncier
FONCIER_EDGE = "#be185d"   # rose foncé
AOI_FACE     = "#bfdbfe"   # bleu clair — anneau AOI
AOI_EDGE     = "#1d4ed8"   # bleu foncé
AOI_ALPHA    = 0.25
PAGE_SIZE    = landscape(A4)


# ─────────────────────────────────────────────────────────────────────────────
# CHARGEMENT DEPUIS SUPABASE
# ─────────────────────────────────────────────────────────────────────────────
def _ensure_backend_on_path() -> None:
    root = str(_BACKEND_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def charger_foncier_et_aoi(
    foncier_id: str,
    aoi_id: str,
) -> Tuple[Optional[gpd.GeoDataFrame], Optional[gpd.GeoDataFrame]]:
    """
    Charge foncier et AOI depuis Supabase.

    Params
    ------
    foncier_id : UUID de ecocompensation.foncier
    aoi_id     : UUID de ecocompensation.aoi

    Retourne (foncier_gdf, aoi_gdf) en EPSG:2154, ou (None, None) si erreur.
    """
    _ensure_backend_on_path()
    from dotenv import load_dotenv
    load_dotenv(_BACKEND_ROOT / ".env")

    from sqlalchemy import text
    from db import get_engine

    foncier_gdf = None
    aoi_gdf = None

    try:
        with get_engine().connect() as conn:
            # ── Foncier ──
            if foncier_id:
                sql_f = text("""
                    SELECT id, name, area_ha, geom_2154
                    FROM ecocompensation.foncier
                    WHERE id = CAST(:fid AS uuid)
                """)
                foncier_gdf = gpd.read_postgis(
                    sql_f, conn, geom_col="geom_2154", params={"fid": foncier_id}
                )
                if foncier_gdf is not None and not foncier_gdf.empty:
                    if foncier_gdf.crs is None:
                        foncier_gdf = foncier_gdf.set_crs(2154)
                    else:
                        foncier_gdf = foncier_gdf.to_crs(2154)
                    print(f"   ✓ Foncier : {foncier_gdf['name'].iloc[0]} "
                          f"({float(foncier_gdf['area_ha'].iloc[0]):.2f} ha)")
                else:
                    print(f"   ⚠ Foncier introuvable : {foncier_id}")
                    foncier_gdf = None

            # ── AOI ──
            if aoi_id:
                sql_a = text("""
                    SELECT id, code_insee, buffer_m, geom_2154
                    FROM ecocompensation.aoi
                    WHERE id = CAST(:aid AS uuid)
                """)
                aoi_gdf = gpd.read_postgis(
                    sql_a, conn, geom_col="geom_2154", params={"aid": aoi_id}
                )
                if aoi_gdf is not None and not aoi_gdf.empty:
                    if aoi_gdf.crs is None:
                        aoi_gdf = aoi_gdf.set_crs(2154)
                    else:
                        aoi_gdf = aoi_gdf.to_crs(2154)
                    buffer_m = int(aoi_gdf["buffer_m"].iloc[0])
                    print(f"   ✓ AOI : {aoi_gdf['code_insee'].iloc[0]} "
                          f"(buffer {buffer_m:,} m)")
                else:
                    print(f"   ⚠ AOI introuvable : {aoi_id}")
                    aoi_gdf = None

    except Exception as e:
        print(f"   ⚠ Erreur chargement DB : {e}")

    return foncier_gdf, aoi_gdf


# ─────────────────────────────────────────────────────────────────────────────
# GÉNÉRATION CARTE CONTEXTE (matplotlib)
# ─────────────────────────────────────────────────────────────────────────────
def generer_image_contexte(
    foncier_gdf: Optional[gpd.GeoDataFrame],
    aoi_gdf: Optional[gpd.GeoDataFrame],
    buffer_extra_m: int = 2000,
) -> BytesIO:
    """
    Génère l'image PNG de la carte de contexte.

    - Fond de carte centré sur le foncier si disponible (sinon sur l'AOI)
    - Polygone rose = emprise foncier projet (l'AOI buffer n'est pas tracée)
    - Barre d'échelle + flèche Nord
    """
    # ── Détermination de l'emprise d'affichage ──────────────────────────────
    # Priorité au foncier pour le zoom : un buffer AOI très large réduirait le site à un point.
    layers_3857 = []
    if foncier_gdf is not None and not foncier_gdf.empty:
        layers_3857.append(foncier_gdf.to_crs(3857))
    elif aoi_gdf is not None and not aoi_gdf.empty:
        layers_3857.append(aoi_gdf.to_crs(3857))

    if not layers_3857:
        raise ValueError("Au moins un GeoDataFrame (foncier ou AOI) est requis.")

    all_bounds = np.array([g.total_bounds for g in layers_3857])
    bounds = np.array([
        all_bounds[:, 0].min(), all_bounds[:, 1].min(),
        all_bounds[:, 2].max(), all_bounds[:, 3].max(),
    ])

    dx = bounds[2] - bounds[0]
    dy = bounds[3] - bounds[1]
    buf_display = max(buffer_extra_m, max(dx, dy) * 0.20)

    xlim = (bounds[0] - buf_display, bounds[2] + buf_display)
    ylim = (bounds[1] - buf_display, bounds[3] + buf_display)

    # ── Figure ──────────────────────────────────────────────────────────────
    fig_w_in = (PAGE_SIZE[0] - 1.5*cm*2) / 28.35
    fig_h_in = (PAGE_SIZE[1] - 5.5*cm) / 28.35
    dpi = 220

    fig, ax = plt.subplots(figsize=(fig_w_in, fig_h_in), dpi=dpi)
    ax.set_xlim(xlim)
    ax.set_ylim(ylim)
    ax.set_aspect("equal")

    # ── Fond de carte ───────────────────────────────────────────────────────
    crs_str = layers_3857[0].crs.to_string()
    try:
        ctx.add_basemap(ax, crs=crs_str,
                        source=ctx.providers.CartoDB.Positron,
                        zoom="auto", attribution=False)
    except Exception:
        try:
            ctx.add_basemap(ax, crs=crs_str,
                            source=ctx.providers.OpenStreetMap.Mapnik,
                            zoom="auto", attribution=False)
        except Exception:
            ax.set_facecolor("#f0ede8")

    # ── Foncier — polygone rose plein ───────────────────────────────────────
    if foncier_gdf is not None and not foncier_gdf.empty:
        fonc_3857 = foncier_gdf.to_crs(3857)
        fonc_3857.plot(ax=ax,
                       facecolor=FONCIER_FACE,
                       edgecolor=FONCIER_EDGE,
                       linewidth=2.5,
                       alpha=0.75,
                       zorder=3)

        # Label surface au centroïde
        centroid = fonc_3857.geometry.union_all().centroid
        area_ha = float(foncier_gdf["area_ha"].iloc[0]) \
            if "area_ha" in foncier_gdf.columns \
            else foncier_gdf.to_crs(2154).geometry.area.sum() / 10_000
        name = "Emprise projet"
        ax.text(centroid.x, centroid.y,
                f"{name}\n{area_ha:.1f} ha",
                fontsize=8, fontweight="bold",
                ha="center", va="center",
                color=FONCIER_EDGE, zorder=7,
                path_effects=[pe.withStroke(linewidth=3, foreground="white")])

    # ── Barre d'échelle ─────────────────────────────────────────────────────
    _add_scalebar(ax, xlim, ylim)

    # ── Flèche Nord ─────────────────────────────────────────────────────────
    _add_north_arrow(ax, xlim, ylim)

    # ── Axes ────────────────────────────────────────────────────────────────
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_edgecolor("#BBBBBB")
        spine.set_linewidth(0.8)

    plt.tight_layout(pad=0.3)

    buf_io = BytesIO()
    fig.savefig(buf_io, format="png", dpi=dpi, bbox_inches="tight",
                facecolor="white")
    plt.close(fig)
    buf_io.seek(0)
    return buf_io


def _add_scalebar(ax, xlim, ylim, nb_segments=4):
    xrange = xlim[1] - xlim[0]
    yrange = ylim[1] - ylim[0]
    target_m = xrange * 0.22
    magnitude = 10 ** int(np.log10(max(target_m, 1)))
    nice = [1, 2, 5, 10]
    bar_m = min(nice, key=lambda x: abs(x * magnitude - target_m)) * magnitude
    x0 = xlim[0] + xrange * 0.05
    y0 = ylim[0] + yrange * 0.035
    seg_len = bar_m / nb_segments
    for i in range(nb_segments):
        color = "#333333" if i % 2 == 0 else "#FFFFFF"
        rect = mpatches.FancyBboxPatch(
            (x0 + i * seg_len, y0), seg_len, yrange * 0.007,
            boxstyle="square,pad=0",
            facecolor=color, edgecolor="#333333", linewidth=0.6, zorder=8)
        ax.add_patch(rect)
    for i in range(nb_segments + 1):
        val = int(bar_m * i / nb_segments)
        label = f"{val/1000:.0f} km" if val >= 1000 else f"{val} m"
        ax.text(x0 + i * seg_len, y0 + yrange * 0.011, label,
                fontsize=6.5, ha="center", va="bottom", color="#333333", zorder=8,
                path_effects=[pe.withStroke(linewidth=2, foreground="white")])


def _add_north_arrow(ax, xlim, ylim):
    xrange = xlim[1] - xlim[0]
    yrange = ylim[1] - ylim[0]
    x = xlim[1] - xrange * 0.06
    y = ylim[1] - yrange * 0.10
    arrow_len = yrange * 0.055
    ax.annotate("", xy=(x, y), xytext=(x, y - arrow_len),
                arrowprops=dict(arrowstyle="-|>", color=VERT_FONCE,
                                lw=2, mutation_scale=14), zorder=9)
    ax.text(x, y + arrow_len * 0.18, "N",
            fontsize=11, fontweight="bold", ha="center", va="bottom",
            color=VERT_FONCE, zorder=9,
            path_effects=[pe.withStroke(linewidth=3, foreground="white")])


# ─────────────────────────────────────────────────────────────────────────────
# LÉGENDE CONTEXTE
# ─────────────────────────────────────────────────────────────────────────────
def generer_image_legende_contexte(has_aoi: bool = True) -> BytesIO:
    items = []
    if has_aoi:
        items.append(mpatches.Patch(
            facecolor=AOI_FACE, edgecolor=AOI_EDGE,
            linewidth=1.2, alpha=0.6, linestyle="--",
            label="Zone d'étude (buffer AOI)"))
    items.append(mpatches.Patch(
        facecolor=FONCIER_FACE, edgecolor=FONCIER_EDGE,
        linewidth=1.5, label="Périmètre foncier — site projet"))

    ncols = len(items)
    fig, ax = plt.subplots(figsize=(6.0, 0.65), dpi=180)
    ax.axis("off")
    ax.legend(handles=items, loc="center", frameon=True,
              framealpha=0.95, edgecolor="#CCCCCC",
              fontsize=8, ncol=ncols,
              title="Légende", title_fontsize=8.5)
    plt.tight_layout(pad=0.2)
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf


# ─────────────────────────────────────────────────────────────────────────────
# ENCART INFOS PROJET (sous la carte)
# ─────────────────────────────────────────────────────────────────────────────
def _infos_projet_table(
    foncier_gdf: Optional[gpd.GeoDataFrame],
    aoi_gdf: Optional[gpd.GeoDataFrame],
    meta: dict,
    W_pt: float,
):
    """Retourne un élément ReportLab Table avec les infos clés du projet."""
    VERT = HexColor(VERT_FONCE)
    BLANC = HexColor("#FFFFFF")
    PALE  = HexColor("#D8F3DC")
    GRIS  = HexColor("#555555")

    area_ha = "—"
    if foncier_gdf is not None and not foncier_gdf.empty:
        if "area_ha" in foncier_gdf.columns:
            area_ha = f"{float(foncier_gdf['area_ha'].iloc[0]):.2f} ha"
        else:
            area_ha = f"{foncier_gdf.to_crs(2154).geometry.area.sum()/10_000:.2f} ha"

    buffer_m = "—"
    code_insee = "—"
    if aoi_gdf is not None and not aoi_gdf.empty:
        buffer_m = f"{int(aoi_gdf['buffer_m'].iloc[0]):,} m".replace(",", " ")
        code_insee = str(aoi_gdf["code_insee"].iloc[0])

    s_label = ParagraphStyle("lbl", fontSize=7.5, fontName="Helvetica-Bold",
                              textColor=VERT, leading=11)
    s_val   = ParagraphStyle("val", fontSize=8, fontName="Helvetica",
                              textColor=HexColor(GRIS.hexval()), leading=11)

    def row(label, val):
        return [Paragraph(label, s_label), Paragraph(str(val), s_val)]

    rows = [
        row("Maître d'ouvrage", meta.get("maitre_ouvrage", "—")),
        row("Commune / Code INSEE", f"{meta.get('commune', '—')}  —  {code_insee}"),
        row("Type de projet", meta.get("type_projet", "—")),
        row("Surface foncier", area_ha),
        row("Zone d'étude (buffer)", buffer_m),
        row("Besoin compensatoire", f"{meta.get('besoin_compensatoire_ha', '—')} ha"),
        row("Espèces cibles", ", ".join(meta.get("especes_cibles", []))),
        row("Bureau d'études", meta.get("bureau_etudes", "—")),
    ]

    tbl = Table(rows, colWidths=[W_pt * 0.32, W_pt * 0.68])
    tbl.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (0, -1), PALE),
        ("BACKGROUND",   (1, 0), (1, -1), HexColor("#FAFAFA")),
        ("GRID",         (0, 0), (-1, -1), 0.4, HexColor("#CCCCCC")),
        ("TOPPADDING",   (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 4),
        ("LEFTPADDING",  (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (1, 0), (1, -1),
         [HexColor("#FAFAFA"), HexColor("#F0F0F0")]),
    ]))
    return tbl


# ─────────────────────────────────────────────────────────────────────────────
# GÉNÉRATION PDF UNE PAGE
# ─────────────────────────────────────────────────────────────────────────────
def generer_page_contexte_pdf(
    output_pdf: str,
    foncier_gdf: Optional[gpd.GeoDataFrame],
    aoi_gdf: Optional[gpd.GeoDataFrame],
    meta: dict,
    buffer_extra_m: int = 2000,
) -> str:
    """
    Génère une page PDF de contexte projet.

    Params
    ------
    output_pdf     : chemin de sortie
    foncier_gdf    : GeoDataFrame foncier (EPSG:2154)
    aoi_gdf        : GeoDataFrame AOI (EPSG:2154)
    meta           : dict avec maitre_ouvrage, commune, type_projet,
                     besoin_compensatoire_ha, especes_cibles, bureau_etudes
    buffer_extra_m : marge supplémentaire autour de l'AOI en mètres
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_pdf)), exist_ok=True)

    W_pt = PAGE_SIZE[0] - 2.4*cm

    print("🗺️  Génération carte contexte...")
    img_carte_buf = generer_image_contexte(foncier_gdf, aoi_gdf, buffer_extra_m)

    print("📊 Génération légende contexte...")
    img_leg_buf = generer_image_legende_contexte(has_aoi=False)

    doc = SimpleDocTemplate(
        output_pdf, pagesize=PAGE_SIZE,
        rightMargin=1.2*cm, leftMargin=1.2*cm,
        topMargin=1.2*cm, bottomMargin=1.2*cm,
    )

    story = []

    # ── Titre ──
    s_titre = ParagraphStyle("t", fontSize=11, fontName="Helvetica-Bold",
        textColor=HexColor(VERT_FONCE), alignment=TA_CENTER, spaceAfter=2)
    s_sous  = ParagraphStyle("s", fontSize=8.5, fontName="Helvetica",
        textColor=HexColor("#555555"), alignment=TA_CENTER, spaceAfter=6)

    story.append(Paragraph("Localisation du site projet", s_titre))
    story.append(Paragraph(
        f"{meta.get('type_projet', '')} — {meta.get('commune', '')}",
        s_sous))

    # ── Carte ──
    carte_h = PAGE_SIZE[1] - 2.4*cm - 1.2*cm - 3.5*cm - 5.5*cm
    carte_img = _buf_to_rl_image(img_carte_buf, width=W_pt, max_height=carte_h)
    story.append(carte_img)

    # ── Légende ──
    leg_img = _buf_to_rl_image(img_leg_buf, width=W_pt * 0.6, max_height=1.5*cm)
    story.append(Spacer(1, 0.2*cm))
    story.append(leg_img)

    # ── Tableau infos projet ──
    story.append(Spacer(1, 0.4*cm))
    story.append(_infos_projet_table(foncier_gdf, aoi_gdf, meta, W_pt))

    # ── Footer ──
    def footer_cb(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(HexColor("#888888"))
        canvas.setFont("Helvetica", 6.5)
        canvas.drawCentredString(PAGE_SIZE[0]/2, 0.6*cm,
            "KERELIA × ECO-COMPENSATION — Document confidentiel")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer_cb, onLaterPages=footer_cb)
    print(f"✅ Page contexte générée : {output_pdf}")
    return output_pdf


def _buf_to_rl_image(buf: BytesIO, width: float, max_height: float) -> RLImage:
    from PIL import Image as PILImage
    buf.seek(0)
    pil = PILImage.open(buf)
    pw, ph = pil.size
    ratio = ph / pw
    h = min(width * ratio, max_height)
    w = h / ratio
    buf.seek(0)
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.write(buf.read())
    tmp.flush()
    return RLImage(tmp.name, width=w, height=h)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN standalone (dev / test)
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(_BACKEND_ROOT / ".env")

    # IDs en dur pour le dev — à paramétrer via l'orchestrateur en prod
    FONCIER_ID = "4cb71955-82c7-4a3c-a3dc-8c61a29080e5"
    AOI_ID     = "7ba1e7b4-078b-4e47-99ae-6c97a4754d07"

    # Chemin portable : rapport/carte/ à côté de ce module (créé par makedirs si besoin).
    # Ne pas utiliser /mnt/... (réservé aux sandboxes cloud) : en local macOS cela provoque
    # OSError: [Errno 30] Read-only file system sur la création de /mnt.
    _rapport_dir = Path(__file__).resolve().parent.parent
    OUTPUT_PDF = str(_rapport_dir / "carte" / "carte_contexte.pdf")

    META = {
        "maitre_ouvrage":          "Groupe QENERGY",
        "commune":                 "La Brède (33)",
        "type_projet":             "Centrale agrivoltaïque",
        "besoin_compensatoire_ha": 6.4,
        "especes_cibles":          ["Cisticole des joncs", "Tarier pâtre"],
        "bureau_etudes":           "SIMETHIS",
    }

    print("📂 Chargement foncier + AOI depuis Supabase...")
    foncier_gdf, aoi_gdf = charger_foncier_et_aoi(FONCIER_ID, AOI_ID)

    # Fallback dev : GeoDataFrame synthétique depuis le WKB fourni
    if foncier_gdf is None:
        print("   → Fallback : géométrie foncier en dur (WKB)")
        from shapely import wkb
        import pandas as pd
        WKB_FONCIER = bytes.fromhex(
            "0106000020 6A08000001000000..."  # tronqué — remplacer par le WKB complet
            .replace(" ", "")
        )
        try:
            geom = wkb.loads(WKB_FONCIER)
            foncier_gdf = gpd.GeoDataFrame(
                [{"id": FONCIER_ID, "name": "ZIP_MOR33", "area_ha": 14.64}],
                geometry=[geom], crs=2154)
        except Exception as e:
            print(f"   ⚠ WKB fallback impossible : {e}")

    generer_page_contexte_pdf(
        output_pdf=OUTPUT_PDF,
        foncier_gdf=foncier_gdf,
        aoi_gdf=aoi_gdf,
        meta=META,
        buffer_extra_m=1500,
    )