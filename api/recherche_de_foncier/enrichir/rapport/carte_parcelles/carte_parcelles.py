"""
Module : Génération de la carte PDF des parcelles éco-compensation
------------------------------------------------------------------
Input  : SHP des parcelles (EPSG:2154) avec colonnes rang, score_eco, eco_max,
         score_dur, score_comp (alias legacy : eco_tot, dur_tot, cmp_tot), surf_ha, idu, section, numero
Output : PDF A4 pleine page avec la carte + légende

Usage  :
    python carte_parcelles.py
    # ou depuis un autre module :
    from carte_parcelles import generer_carte_pdf
    generer_carte_pdf(shp_path, output_pdf, titre_projet="...", site_geojson=None, project_id="<uuid>")
"""

import os
import sys
import uuid
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import numpy as np
import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.patheffects as pe
from matplotlib.lines import Line2D
from matplotlib.patches import FancyBboxPatch
import contextily as ctx
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Image as RLImage, Spacer
from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from io import BytesIO
import tempfile
from typing import Optional, Tuple

from dotenv import load_dotenv

from generer_rapport import pick_col

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_BACKEND_ROOT / ".env")

# ─────────────────────────────────────────────────────
# PALETTE
# ─────────────────────────────────────────────────────
VERT_FONCE  = "#1B4332"
VERT_MOYEN  = "#2D6A4F"
VERT_CLAIR  = "#95D5B2"

# Foncier projet (initial) — remplissage rose sur la carte
FONCIER_FACE = "#f9a8d4"
FONCIER_EDGE = "#be185d"


def _ensure_backend_on_path() -> None:
    root = str(_BACKEND_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def resolve_project_id_for_foncier(
    project_id_explicit: Optional[str],
    shp_path: str,
) -> Tuple[Optional[str], str]:
    """
    Détermine l’UUID projet pour charger le foncier.

    Ordre : argument explicite → env CARTE_PROJECT_ID → fichier project_id.txt
    à côté du .shp (une ligne, UUID).

    Retourne (uuid ou None, libellé court pour les logs).
    """
    if project_id_explicit and str(project_id_explicit).strip():
        return str(project_id_explicit).strip(), "paramètre project_id"
    env = os.getenv("CARTE_PROJECT_ID", "").strip()
    if env:
        return env, "variable d'environnement CARTE_PROJECT_ID"
    shp_dir = os.path.dirname(os.path.abspath(shp_path))
    sidecar = os.path.join(shp_dir, "project_id.txt")
    if os.path.isfile(sidecar):
        try:
            with open(sidecar, encoding="utf-8") as f:
                raw_lines = f.read().splitlines()
            first = next((ln.strip() for ln in raw_lines if ln.strip()), "")
            if first:
                return first, f"fichier {os.path.basename(sidecar)}"
        except OSError as e:
            print(f"⚠️  {sidecar} : {e}")
    return None, ""


def load_foncier_projet_gdf(project_id: str) -> Optional[gpd.GeoDataFrame]:
    """
    Charge une géométrie depuis ecocompensation.foncier.

    1) Jointure projects → foncier (``ecocompensation.projects.id`` = UUID fourni).
    2) Si vide : lecture directe par ``ecocompensation.foncier.id`` (même UUID),
       car l’utilisateur peut passer l’id foncier au lieu de l’id projet.
    """
    pid = (project_id or "").strip()
    if not pid:
        return None
    try:
        uuid.UUID(pid)
    except ValueError:
        print(f"⚠️  project_id invalide (UUID attendu) : {project_id!r}")
        return None

    _ensure_backend_on_path()
    from sqlalchemy import text

    from db import get_engine

    sql_via_project = text("""
        SELECT f.id, f.name, f.geom_2154
        FROM ecocompensation.projects p
        INNER JOIN ecocompensation.foncier f ON f.id = p.foncier_id
        WHERE p.id = CAST(:uid AS uuid)
    """)
    sql_via_foncier_pk = text("""
        SELECT f.id, f.name, f.geom_2154
        FROM ecocompensation.foncier f
        WHERE f.id = CAST(:uid AS uuid)
    """)
    try:
        with get_engine().connect() as conn:
            gdf = gpd.read_postgis(
                sql_via_project,
                conn,
                geom_col="geom_2154",
                params={"uid": pid},
            )
            if gdf is None or gdf.empty:
                gdf = gpd.read_postgis(
                    sql_via_foncier_pk,
                    conn,
                    geom_col="geom_2154",
                    params={"uid": pid},
                )
                if gdf is not None and len(gdf):
                    print(
                        "      → UUID reconnu comme ecocompensation.foncier.id "
                        "(aucun projet avec projects.id = cet UUID)."
                    )
    except Exception as e:
        print(f"⚠️  Impossible de charger le foncier depuis la base : {e}")
        return None

    if gdf is None or gdf.empty:
        return None
    if gdf.crs is None:
        gdf = gdf.set_crs(2154)
    else:
        gdf = gdf.to_crs(2154)
    return gdf


def couleur_eco(eco_tot, eco_max=6):
    """
    Remplissage parcelle selon score écologique (barème courant 0..6).
      ≥ 4/6  → vert
      2-3/6  → orange
      ≤ 1/6  → gris
    """
    score = float(eco_tot) if eco_tot is not None else 0
    if score >= 4:
        return "#2D6A4F", "Potentiel fort (éco ≥ 4/6)"
    elif score >= 2:
        return "#F4A261", "Potentiel moyen (éco 2-3/6)"
    else:
        return "#B7B7B7", "Potentiel faible (éco ≤ 1/6)"


def couleur_durete(dur_tot):
    """
    Contour parcelle selon score de dureté foncière (0-100).
    -1 = propriétaire privé (pas de dureté calculée).
      < 50   → vert foncé
      < 70   → vert clair
      < 80   → jaune
      ≥ 80   → orange
      -1     → gris (privé)
    """
    d = float(dur_tot) if dur_tot is not None else -1
    if d < 0:
        return "#AAAAAA", True, "Privé (dureté N/A)"   # (couleur, pointillé, label)
    elif d < 50:
        return "#1B4332", False, "Dureté faible (<50)"
    elif d < 70:
        return "#52B788", False, "Dureté modérée (50-69)"
    elif d < 80:
        return "#F4A261", False, "Dureté élevée (70-79)"
    else:
        return "#E76F51", False, "Dureté forte (≥80)"


REDHIBITOIRE_THRESHOLD = 20   # attractivité < 20 → dureté rédhibitoire


def niveau_composite(cmp_tot, eco_tot, dur_tot):
    """
    Niveau final affiché dans la colonne 'Niveau éco' du tableau.

    Règles (en ordre de priorité) :
    1. Privé (dur=-1, cmp<=0)  → niveau basé sur eco seul, label "(privé)"
    2. Dureté rédhibitoire (attractivité = 100 - dur < REDHIBITOIRE_THRESHOLD)
       → "Rédhibitoire" peu importe l'éco
    3. Score composite valide :
         cmp ≥ 55  → Fort
         cmp ≥ 40  → Moyen
         cmp < 40  → Faible

    Retourne (label: str, couleur_fond: str, couleur_texte: str)
    """
    dur = float(dur_tot) if dur_tot is not None else -1
    cmp = float(cmp_tot) if cmp_tot is not None else -9999
    eco = float(eco_tot) if eco_tot is not None else 0

    # Cas privé
    if dur < 0 or cmp <= 0:
        if eco >= 4:
            return "Fort (privé)", "#2D6A4F", "white"
        elif eco >= 2:
            return "Moyen (privé)", "#F4A261", "white"
        else:
            return "Faible (privé)", "#B7B7B7", "white"

    # Garde-fou rédhibitoire
    attractivite = 100 - dur
    if attractivite < REDHIBITOIRE_THRESHOLD:
        return "Rédhibitoire", "#9D0208", "white"

    # Score composite
    if cmp >= 55:
        return "Fort", "#2D6A4F", "white"
    elif cmp >= 40:
        return "Moyen", "#F4A261", "white"
    else:
        return "Faible", "#B7B7B7", "#333333"


def rang_composite(gdf):
    """
    Calcule le rang basé sur le score composite décroissant.
    Les parcelles privées (score_comp <= 0) reçoivent None → affiché 'NA'.
    Retourne un dict {index: rang_str}.
    """
    cmp_col = pick_col(gdf, "score_comp", "cmp_tot")
    # Sépare PM (score composite valide) vs privées
    pm_mask = gdf[cmp_col] > 0
    pm_sorted = gdf[pm_mask].sort_values(cmp_col, ascending=False)
    result = {}
    for i, idx in enumerate(pm_sorted.index, start=1):
        result[idx] = str(i)
    for idx in gdf[~pm_mask].index:
        result[idx] = "NA"
    return result


# ─────────────────────────────────────────────────────
# GÉNÉRATION CARTE MATPLOTLIB
# ─────────────────────────────────────────────────────
def generer_image_carte(
    gdf_2154,
    titre_projet="",
    site_geom=None,
    buffer_m=800,
    *,
    foncier_projet=None,
):
    """
    Génère la carte matplotlib et retourne un BytesIO PNG haute résolution.
    - Remplissage  = couleur score écologique (score_eco)
    - Contour      = couleur dureté foncière (score_dur), pointillé si privé
    - Numéro       = rang composite décroissant, 'NA' si privé
    - foncier_projet : GeoDataFrame optionnel (EPSG:2154) — périmètre projet initial en rose
    """

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

    # ── Reprojection en Web Mercator pour contextily ──
    gdf = gdf_2154.to_crs(3857)
    bound_rows = [gdf.total_bounds]
    if site_geom is not None and len(site_geom):
        bound_rows.append(site_geom.to_crs(3857).total_bounds)
    if foncier_projet is not None and len(foncier_projet):
        bound_rows.append(foncier_projet.to_crs(3857).total_bounds)
    bmat = np.array(bound_rows)
    bounds = np.array(
        [bmat[:, 0].min(), bmat[:, 1].min(), bmat[:, 2].max(), bmat[:, 3].max()]
    )

    dx = bounds[2] - bounds[0]
    dy = bounds[3] - bounds[1]
    # Zoom plus serré : même logique que les cartes 4.1/4.2.
    span = max(dx, dy)
    buf = max(80.0, span * 0.08)
    buf = min(buf, 450.0)
    if isinstance(buffer_m, (int, float)) and buffer_m > 0:
        buf = min(buf, float(buffer_m))

    fig_w_in = (A4[0] - 1.5*cm*2) / 28.35
    fig_h_in = (A4[1] - 4.5*cm) / 28.35
    xlim = (bounds[0] - buf, bounds[2] + buf)
    ylim = (bounds[1] - buf, bounds[3] + buf)
    xlim, ylim = _fit_bounds_to_aspect(xlim, ylim, target_aspect=fig_w_in / fig_h_in)
    dpi = 220

    fig, ax = plt.subplots(figsize=(fig_w_in, fig_h_in), dpi=dpi)
    ax.set_xlim(xlim)
    ax.set_ylim(ylim)
    ax.set_aspect("equal")

    # ── Fond de carte ──
    try:
        ctx.add_basemap(ax, crs=gdf.crs.to_string(),
                        source=ctx.providers.CartoDB.Positron,
                        zoom="auto", attribution=False)
    except Exception:
        try:
            ctx.add_basemap(ax, crs=gdf.crs.to_string(),
                            source=ctx.providers.OpenStreetMap.Mapnik,
                            zoom="auto", attribution=False)
        except Exception:
            ax.set_facecolor("#f0ede8")

    # ── Foncier projet initial (base ecocompensation.foncier) — rose, sous le site optionnel ──
    if foncier_projet is not None and len(foncier_projet):
        foncier_projet.to_crs(3857).plot(
            ax=ax,
            facecolor=FONCIER_FACE,
            edgecolor=FONCIER_EDGE,
            linewidth=2.0,
            alpha=0.55,
            zorder=2,
        )

    # ── Site projet (GeoJSON optionnel, distinct du foncier DB) ──
    if site_geom is not None:
        site_geom.to_crs(3857).plot(ax=ax, facecolor="#E63946",
                                     edgecolor="#9D0208", linewidth=2,
                                     alpha=0.45, zorder=3)

    # ── Calcul rangs composites ──
    rangs = rang_composite(gdf_2154)   # index → "1"/"2"/... ou "NA"
    # Remap vers index du GDF reprojeté (même index)
    rangs_3857 = {idx: rangs.get(idx, "NA") for idx in gdf.index}

    eco_col = pick_col(gdf_2154, "score_eco", "eco_tot")
    dur_col = pick_col(gdf_2154, "score_dur", "dur_tot")

    # ── Tracé des parcelles ──
    for idx, row in gdf.iterrows():
        face, _   = couleur_eco(row[eco_col])
        edge, dashed, _ = couleur_durete(row[dur_col])
        lw = 3.0

        geom_series = gpd.GeoSeries([row.geometry], crs=gdf.crs)

        # Remplissage
        geom_series.plot(ax=ax, facecolor=face, edgecolor="none",
                         alpha=0.78, zorder=4)

        # Contour (dessiné séparément pour gérer linestyle)
        linestyle = (0, (4, 3)) if dashed else "solid"
        geom_series.plot(ax=ax, facecolor="none", edgecolor=edge,
                         linewidth=lw, linestyle=linestyle,
                         alpha=1.0, zorder=5)

    # ── Numéros de rang ──
    for idx, row in gdf.iterrows():
        centroid = row.geometry.centroid
        label = rangs_3857[idx]
        fontsize = 8 if label == "NA" else 9
        ax.text(centroid.x, centroid.y, label,
                fontsize=fontsize, fontweight="bold",
                ha="center", va="center",
                color="#1B4332" if label != "NA" else "#666666",
                zorder=7,
                path_effects=[pe.withStroke(linewidth=3.5, foreground="white")])

    _add_scalebar(ax, xlim, ylim)
    _add_north_arrow(ax, xlim, ylim)

    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_edgecolor("#AAAAAA")
        spine.set_linewidth(0.8)

    plt.tight_layout(pad=0.3)

    buf_io = BytesIO()
    fig.savefig(buf_io, format="png", dpi=dpi, bbox_inches="tight",
                facecolor="white")
    plt.close(fig)
    buf_io.seek(0)
    return buf_io


def _add_scalebar(ax, xlim, ylim, nb_segments=4):
    """Barre d'échelle en bas à gauche (Web Mercator → mètres approx)."""
    xrange = xlim[1] - xlim[0]
    yrange = ylim[1] - ylim[0]

    # Longueur cible ~20% de la largeur
    target_m = xrange * 0.20
    # Arrondi à un beau chiffre
    magnitude = 10 ** int(np.log10(target_m))
    nice = [1, 2, 5, 10]
    bar_m = min(nice, key=lambda x: abs(x * magnitude - target_m)) * magnitude

    x0 = xlim[0] + xrange * 0.05
    y0 = ylim[0] + yrange * 0.04
    bar_len = bar_m   # en mètres Web Mercator (approximation valide à ces latitudes)

    seg_len = bar_len / nb_segments
    for i in range(nb_segments):
        color = "#333333" if i % 2 == 0 else "#FFFFFF"
        rect = mpatches.FancyBboxPatch(
            (x0 + i * seg_len, y0), seg_len, yrange * 0.008,
            boxstyle="square,pad=0",
            facecolor=color, edgecolor="#333333", linewidth=0.7,
            zorder=7
        )
        ax.add_patch(rect)

    # Labels
    for i in range(nb_segments + 1):
        val = int(bar_m * i / nb_segments)
        label = f"{val/1000:.1f} km" if val >= 1000 else f"{val} m"
        ax.text(x0 + i * seg_len, y0 + yrange * 0.012, label,
                fontsize=6.5, ha="center", va="bottom",
                color="#333333", zorder=7,
                path_effects=[pe.withStroke(linewidth=2, foreground="white")])


def _add_north_arrow(ax, xlim, ylim):
    """Flèche nord en haut à droite."""
    xrange = xlim[1] - xlim[0]
    yrange = ylim[1] - ylim[0]
    x = xlim[1] - xrange * 0.06
    y = ylim[1] - yrange * 0.10
    arrow_len = yrange * 0.055

    ax.annotate(
        "", xy=(x, y), xytext=(x, y - arrow_len),
        arrowprops=dict(arrowstyle="-|>", color=VERT_FONCE,
                        lw=2, mutation_scale=14),
        zorder=8
    )
    ax.text(x, y + arrow_len * 0.18, "N",
            fontsize=11, fontweight="bold", ha="center", va="bottom",
            color=VERT_FONCE, zorder=8,
            path_effects=[pe.withStroke(linewidth=3, foreground="white")])


# ─────────────────────────────────────────────────────
# LÉGENDE MATPLOTLIB (sous la carte)
# ─────────────────────────────────────────────────────
def generer_image_legende(gdf_2154, include_foncier_layer: bool = False):
    """Légende : score éco | dureté | (optionnel) fond rose = foncier projet initial."""

    ncols = 3 if include_foncier_layer else 2
    fig_w = 10.5 if include_foncier_layer else 8.5
    fig, axes = plt.subplots(1, ncols, figsize=(fig_w, 1.15), dpi=180)
    if ncols == 2:
        axes = [axes[0], axes[1]]

    # ── Légende gauche : score écologique (remplissage) ──
    eco_items = [
        ("#2D6A4F", "Score éco ≥ 4/6  —  Potentiel fort"),
        ("#F4A261", "Score éco 2-3/6  —  Potentiel moyen"),
        ("#B7B7B7", "Score éco ≤ 1/6  —  Potentiel faible"),
    ]
    handles_eco = [
        mpatches.Patch(facecolor=c, edgecolor="#555555", linewidth=0.8, label=l)
        for c, l in eco_items
    ]
    axes[0].axis("off")
    axes[0].legend(handles=handles_eco, loc="center", frameon=True,
                   framealpha=0.95, edgecolor="#CCCCCC", fontsize=7.5,
                   title="Remplissage — Score écologique", title_fontsize=8)

    # ── Légende droite : dureté foncière (contour) ──
    dur_items = [
        ("#1B4332", "solid",  "Dureté <50  —  Faible"),
        ("#52B788", "solid",  "Dureté 50-69  —  Modérée"),
        ("#F4A261", "solid",  "Dureté 70-79  —  Élevée"),
        ("#E76F51", "solid",  "Dureté ≥80  —  Forte"),
        ("#AAAAAA", "dashed", "Propriété privée  —  N/A"),
    ]
    handles_dur = [
        Line2D([0], [0], color=c, linewidth=2.5,
               linestyle=ls, label=l)
        for c, ls, l in dur_items
    ]
    axes[1].axis("off")
    axes[1].legend(handles=handles_dur, loc="center", frameon=True,
                   framealpha=0.95, edgecolor="#CCCCCC", fontsize=7.5,
                   title="Contour — Dureté foncière", title_fontsize=8)

    if include_foncier_layer:
        handles_fn = [
            mpatches.Patch(
                facecolor=FONCIER_FACE,
                edgecolor=FONCIER_EDGE,
                linewidth=1.2,
                label="Périmètre foncier projet (initial)",
            ),
        ]
        axes[2].axis("off")
        axes[2].legend(
            handles=handles_fn,
            loc="center",
            frameon=True,
            framealpha=0.95,
            edgecolor="#CCCCCC",
            fontsize=7.5,
            title="Calque — Projet", title_fontsize=8,
        )

    plt.tight_layout(pad=0.3)
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=180, bbox_inches="tight",
                facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf


# ─────────────────────────────────────────────────────
# GÉNÉRATION PDF
# ─────────────────────────────────────────────────────
def generer_carte_pdf(
    shp_path: str,
    output_pdf: str,
    titre_projet: str = "Pré-identification foncière — Éco-compensation",
    commune: str = "",
    site_geojson_path: str = None,
    buffer_m: int = 800,
    project_id: Optional[str] = None,
):
    """
    Point d'entrée principal.

    Params
    ------
    shp_path         : chemin vers le .shp des parcelles (EPSG:2154)
    output_pdf       : chemin de sortie du PDF
    titre_projet     : titre affiché sur la page
    commune          : nom de la commune (affiché en sous-titre)
    site_geojson_path: chemin GeoJSON du polygone site projet (optionnel)
    buffer_m         : marge autour des emprises (parcelles + foncier + site)
    project_id       : UUID projet — charge ecocompensation.foncier (géométrie rose) si lié.
                       Si omis : utilise CARTE_PROJECT_ID ou <dossier_shp>/project_id.txt
    """
    print(f"📂 Lecture SHP : {shp_path}")
    gdf = gpd.read_file(shp_path)
    assert gdf.crs and gdf.crs.to_epsg() == 2154, "Le SHP doit être en EPSG:2154"
    print(f"   → {len(gdf)} parcelles chargées")

    # Site projet optionnel
    site_geom = None
    if site_geojson_path and os.path.exists(site_geojson_path):
        site_geom = gpd.read_file(site_geojson_path).to_crs(2154)
        print(f"   → Site projet chargé : {site_geojson_path}")

    resolved_pid, pid_source = resolve_project_id_for_foncier(project_id, shp_path)
    foncier_gdf = None

    if resolved_pid:
        print(f"   → Foncier projet : UUID depuis {pid_source} ({resolved_pid[:8]}…)")
        print("   → Chargement ecocompensation.foncier (via jointure projects)…")
        foncier_gdf = load_foncier_projet_gdf(resolved_pid)
        if foncier_gdf is not None and len(foncier_gdf):
            print(
                f"      ✓ Géométrie : {foncier_gdf['name'].iloc[0]!s} "
                f"— {len(foncier_gdf)} entité(s), tracé rose sur la carte"
            )
        else:
            print(
                "      ⚠ Aucun foncier trouvé : projet sans foncier_id, ou UUID inconnu, "
                "ou erreur SQL (voir message éventuel ci-dessus)."
            )
    else:
        print(
            "   → Foncier projet (carte rose) : non chargé — aucun UUID projet.\n"
            "      Fournir : variable CARTE_PROJECT_ID, ou argument project_id=…,\n"
            "      ou un fichier project_id.txt (une ligne UUID) dans le dossier du .shp."
        )

    # ── Génération image carte ──
    print("🗺️  Génération de la carte...")
    img_carte_buf = generer_image_carte(
        gdf,
        titre_projet,
        site_geom,
        buffer_m,
        foncier_projet=foncier_gdf,
    )

    # ── Génération légende ──
    print("📊 Génération de la légende...")
    _has_foncier = foncier_gdf is not None and len(foncier_gdf) > 0
    img_legende_buf = generer_image_legende(gdf, include_foncier_layer=_has_foncier)

    # ── Tableau récapitulatif des parcelles ──
    # (généré en image matplotlib aussi, plus simple à intégrer)
    print("📋 Génération du tableau récapitulatif...")
    img_tableau_buf = generer_image_tableau(gdf)

    # ── Assemblage PDF ──
    print(f"📄 Assemblage PDF → {output_pdf}")
    os.makedirs(os.path.dirname(os.path.abspath(output_pdf)), exist_ok=True)

    from reportlab.platypus import SimpleDocTemplate, Image as RLImage, Spacer, Paragraph, Table, TableStyle
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.colors import HexColor

    doc = SimpleDocTemplate(
        output_pdf,
        pagesize=A4,
        rightMargin=1.2*cm,
        leftMargin=1.2*cm,
        topMargin=1.2*cm,
        bottomMargin=1.2*cm,
    )

    W_pt = A4[0] - 2.4*cm   # largeur utile en points

    story = []

    # ── Header texte ──
    style_titre = ParagraphStyle("t", fontSize=11, fontName="Helvetica-Bold",
        textColor=HexColor(VERT_FONCE), alignment=TA_CENTER, spaceAfter=2)
    style_sous = ParagraphStyle("s", fontSize=8.5, fontName="Helvetica",
        textColor=HexColor("#555555"), alignment=TA_CENTER, spaceAfter=6)

    story.append(Paragraph(titre_projet, style_titre))
    if commune:
        story.append(Paragraph(f"Localisation des unités foncières retenues — {commune}", style_sous))

    # ── Image carte (occupe la majorité de la page) ──
    # Hauteur disponible : A4 hauteur - marges - header - légende - tableau
    carte_h = A4[1] - 2.4*cm - 1.2*cm - 2.8*cm - 4.5*cm
    carte_img = _buf_to_rl_image(img_carte_buf, width=W_pt, max_height=carte_h)
    story.append(carte_img)

    # ── Légende ──
    leg_img = _buf_to_rl_image(img_legende_buf, width=W_pt * 0.80, max_height=2.5*cm)
    story.append(Spacer(1, 0.2*cm))
    story.append(leg_img)

    # ── Tableau parcelles ──
    story.append(Spacer(1, 0.3*cm))
    tab_img = _buf_to_rl_image(img_tableau_buf, width=W_pt, max_height=5*cm)
    story.append(tab_img)

    # ── Footer ──
    def footer_cb(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(HexColor("#888888"))
        canvas.setFont("Helvetica", 6.5)
        canvas.drawCentredString(A4[0]/2, 0.6*cm,
            "KERELIA × ECO-COMPENSATION — Document confidentiel")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer_cb, onLaterPages=footer_cb)
    print(f"✅ PDF généré : {output_pdf}")
    return output_pdf


def _buf_to_rl_image(buf, width, max_height):
    """Convertit un BytesIO PNG en Image ReportLab avec ratio préservé."""
    from PIL import Image as PILImage
    from reportlab.platypus import Image as RLImage
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


# ─────────────────────────────────────────────────────
# TABLEAU RÉCAPITULATIF (image matplotlib)
# ─────────────────────────────────────────────────────
def generer_image_tableau(gdf):
    """
    Tableau récap trié par score composite décroissant.
    Colonne N° : rang composite (NA pour privés).
    Dernière colonne : cellule colorée selon score éco.
    """
    import pandas as pd

    cmp_col = pick_col(gdf, "score_comp", "cmp_tot")
    eco_col = pick_col(gdf, "score_eco", "eco_tot")
    dur_col = pick_col(gdf, "score_dur", "dur_tot")

    rangs = rang_composite(gdf)
    pm   = gdf[gdf[cmp_col] > 0].sort_values(cmp_col, ascending=False)
    priv = gdf[gdf[cmp_col] <= 0]
    gdf_sorted = pd.concat([pm, priv])

    col_headers = ["N°", "IDU", "Surface (ha)", "Score éco", "Score composite", "Dureté foncière", "Niveau final"]

    rows_data = []
    row_meta  = []
    # meta: (face_eco, edge_dur, face_niveau, txt_niveau, face_cmp, txt_cmp)

    for idx, row in gdf_sorted.iterrows():
        rang_label  = rangs[idx]
        dur_val     = float(row[dur_col])
        eco_val     = float(row[eco_col])
        cmp_val     = float(row[cmp_col])

        dur_str = f"{int(dur_val)}/100" if dur_val > 0 else "N/A"
        eco_str = f"{int(eco_val)}/{int(row['eco_max'])}"
        cmp_str = f"{cmp_val:.1f}/100" if cmp_val > 0 else "N/A"

        face_eco, _          = couleur_eco(eco_val)
        edge_dur, dashed, _  = couleur_durete(dur_val)
        niv_label, face_niv, txt_niv = niveau_composite(cmp_val, eco_val, dur_val)

        # Couleur cellule score composite : même palette que niveau mais plus sobre
        if cmp_val <= 0:
            face_cmp, txt_cmp = "#EEEEEE", "#666666"
        elif cmp_val >= 55:
            face_cmp, txt_cmp = "#D8F3DC", "#1B4332"
        elif cmp_val >= 40:
            face_cmp, txt_cmp = "#FFF3E0", "#A04000"
        else:
            face_cmp, txt_cmp = "#F5F5F5", "#555555"

        rows_data.append([rang_label, row["idu"], f"{row['surf_ha']:.2f}",
                          eco_str, cmp_str, dur_str, niv_label])
        row_meta.append((face_eco, edge_dur, face_niv, txt_niv, face_cmp, txt_cmp))

    n_rows = len(rows_data) + 1
    n_cols = len(col_headers)

    fig_w = 10.5
    row_h = 0.32
    fig_h = row_h * n_rows + 0.4

    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=160)
    ax.axis("off")

    all_rows = [col_headers] + rows_data
    col_widths = [0.05, 0.22, 0.09, 0.09, 0.13, 0.13, 0.29]

    tbl = ax.table(cellText=all_rows, colWidths=col_widths,
                   loc="center", cellLoc="center")
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(7)

    # Header
    for j in range(n_cols):
        cell = tbl[0, j]
        cell.set_facecolor(VERT_FONCE)
        cell.set_text_props(color="white", fontweight="bold")
        cell.set_edgecolor("#FFFFFF")

    # Lignes de données
    COL_RANG  = 0
    COL_ECO   = 3
    COL_CMP   = 4
    COL_DUR   = 5
    COL_NIV   = 6

    for i, (face_eco, edge_dur, face_niv, txt_niv, face_cmp, txt_cmp) in enumerate(row_meta):
        bg = "#F5F5F5" if i % 2 == 0 else "#FFFFFF"
        for j in range(n_cols):
            cell = tbl[i+1, j]
            cell.set_edgecolor("#DDDDDD")

            if j == COL_RANG:
                # N° : fond = couleur dureté (contour carte)
                cell.set_facecolor(edge_dur)
                cell.set_text_props(color="white", fontweight="bold", fontsize=7.5)
            elif j == COL_ECO:
                # Score éco : fond = couleur éco (remplissage carte)
                cell.set_facecolor(face_eco)
                cell.set_text_props(color="white", fontweight="bold")
            elif j == COL_CMP:
                # Score composite : teinte douce proportionnelle
                cell.set_facecolor(face_cmp)
                cell.set_text_props(color=txt_cmp, fontweight="bold")
            elif j == COL_DUR:
                # Dureté : fond = couleur dureté (même que contour carte)
                cell.set_facecolor(edge_dur)
                cell.set_text_props(color="white", fontweight="bold")
            elif j == COL_NIV:
                # Niveau final composite
                cell.set_facecolor(face_niv)
                cell.set_text_props(color=txt_niv, fontweight="bold")
            else:
                cell.set_facecolor(bg)

    tbl.scale(1, 1.4)
    plt.tight_layout(pad=0.1)

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf


# ─────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────
if __name__ == "__main__":
    SHP_PATH = (
        "/Volumes/T7/Travaux_Freelance/KERELIA/CUAs/COMPENSATION_PARCELLE/"
        "COMPENSATION_ECO/backend/rapport/carte/parcelles_6643c835/parcelles.shp"
    )
    OUTPUT_PDF = "/Volumes/T7/Travaux_Freelance/KERELIA/CUAs/COMPENSATION_PARCELLE/" \
                 "COMPENSATION_ECO/backend/rapport/carte/carte_parcelles.pdf"

    # Pour le dev local on utilise le SHP uploadé
    import sys
    if not os.path.exists(SHP_PATH):
        print("⚠️  SHP prod introuvable, utilisation du SHP de dev...")
        SHP_PATH = "/mnt/user-data/uploads/parcelles.shp"
        OUTPUT_PDF = "/mnt/user-data/outputs/carte_parcelles.pdf"

    # ID en dur : peut être projects.id ou ecocompensation.foncier.id (les deux sont essayés).
    # Sinon : CARTE_PROJECT_ID ou project_id.txt à côté du .shp.
    _DEFAULT_PROJECT_ID = "4cb71955-82c7-4a3c-a3dc-8c61a29080e5"
    generer_carte_pdf(
        shp_path=SHP_PATH,
        output_pdf=OUTPUT_PDF,
        titre_projet="Pré-identification du foncier mobilisable — Compensation espèces protégées",
        commune="La Brède (33)",
        site_geojson_path=None,  # optionnel : GeoJSON local distinct du foncier DB
        buffer_m=600,
        project_id=_DEFAULT_PROJECT_ID,
    )