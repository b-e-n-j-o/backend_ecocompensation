# -*- coding: utf-8 -*-
"""
urba.py — Module zonage urbanistique par IDU
Pipeline : IDU → géométrie parcelle (WFS IGN) → zonage PLU (WFS GPU) → scoring

Usage standalone :
    python urba.py --idu 862750000D0319
    python urba.py --idu 862750000D0319 --verbose
    python urba.py --idu 862750000D0319 --chart-out zonage_plu.png
"""

import io
import time
import argparse
from pathlib import Path
import requests
import geopandas as gpd
import pandas as pd
from shapely.geometry import box

# IDU → (code_insee, section, numero) pour interroger le WFS IGN
from rpg import decompose_idu

# ============================================================
# CONFIG
# ============================================================
WFS_IGN = "https://data.geopf.fr/wfs/ows"
WFS_GPU = "https://data.geopf.fr/wfs/ows"

LAYER_PARCELLE = "CADASTRALPARCELS.PARCELLAIRE_EXPRESS:parcelle"
LAYER_ZONE_URBA = "wfs_du:zone_urba"

SRS = "EPSG:2154"
SRS_WGS84 = "EPSG:4326"
LIMIT_CAP = 4950  # seuil avant carroyage adaptatif

# ============================================================
# GRILLE DE SCORING PLU (sur 10 points)
# ============================================================
# Score = potentiel de facilité d'acquisition pour compensation écologique
# Haut = facile/intéressant, Bas = difficile/spéculatif
SCORING_TYPEZONE = {
    # Zones naturelles — idéales pour compensation
    "N":   10,  # Naturel strict : pas d'alternative de valorisation
    "Ns":  10,  # Naturel strict spécifique
    "Nf":   9,  # Naturel forestier
    "Nl":   7,  # Naturel loisirs
    "Nh":   6,  # Naturel avec habitation possible
    "Ne":   8,  # Naturel avec équipements
    # Zones agricoles — très bonnes pour compensation
    "A":    8,  # Agricole générique
    "Ap":   9,  # Agricole protégé
    "Ah":   6,  # Agricole avec habitation
    "Ai":   5,  # Agricole avec installations
    # Zones à urbaniser — valeur spéculative, propriétaire attend
    "AU":   1,  # À urbaniser (ouvert)
    "1AU":  1,  # À urbaniser court terme
    "2AU":  3,  # À urbaniser long terme / gelé
    "AUc":  2,  # À urbaniser conditionné
    "AUs":  2,  # À urbaniser strict
    # Zones urbaines — forte valeur, peu pertinent compensation
    "U":    2,  # Urbain générique
    "Uc":   2,  # Urbain centre
    "Uh":   2,  # Urbain habitat
    "Ui":   1,  # Urbain industriel/commercial
    "Ue":   3,  # Urbain équipements
    "Ul":   3,  # Urbain loisirs
}

SCORE_DEFAULT = 5  # score par défaut si typezone inconnu


def get_score_zone(typezone: str) -> int:
    """Retourne le score PLU pour un typezone donné."""
    if not typezone:
        return SCORE_DEFAULT
    # Correspondance exacte d'abord
    if typezone in SCORING_TYPEZONE:
        return SCORING_TYPEZONE[typezone]
    # Correspondance par préfixe (ex: "Nco" → "N")
    for prefix in ["Ap", "Ah", "Ai", "Ns", "Nf", "Nl", "Nh", "Ne",
                   "1AU", "2AU", "AUc", "AUs", "AU",
                   "Uc", "Uh", "Ui", "Ue", "Ul",
                   "N", "A", "U"]:
        if typezone.startswith(prefix):
            return SCORING_TYPEZONE[prefix]
    return SCORE_DEFAULT


# ============================================================
# RENDU PNG — camembert zonage (pour PDF carte d'identité)
# ============================================================
def _couleur_typezone(typezone: str) -> str:
    """Associe une couleur à un code typezone (préfixes longs d'abord)."""
    if not typezone:
        return "#94a3b8"
    tz = str(typezone)
    pal = {
        "Ns": "#16a34a", "Nf": "#22c55e", "Nl": "#4ade80", "Nh": "#86efac", "Ne": "#bbf7d0",
        "Ap": "#ca8a04", "Ah": "#ea580c", "Ai": "#ea580c",
        "1AU": "#6d28d9", "2AU": "#5b21b6", "AUc": "#5b21b6", "AUs": "#4c1d95", "AU": "#7c3aed",
        "Uc": "#b91c1c", "Uh": "#dc2626", "Ui": "#991b1b", "Ue": "#dc2626", "Ul": "#dc2626",
        "N": "#16a34a", "A": "#ca8a04", "U": "#dc2626",
    }
    for key in sorted(pal.keys(), key=len, reverse=True):
        if tz.startswith(key):
            return pal[key]
    return "#94a3b8"


def generer_camembert_zonage_png(zonage_detail: list[dict], width_px: int = 520, height_px: int = 360) -> bytes | None:
    """
    Génère une figure PNG (titre + graphique + légende) à partir du zonage_detail
    (sortie de compute_urba_score). Rendu type rapport, pas un simple disque isolé.
    """
    if not zonage_detail:
        return None

    sizes = [float(z.get("proportion_pct", 0) or 0) for z in zonage_detail]
    total = sum(sizes)
    if total <= 0:
        return None

    typezones = [str(z.get("typezone", "?")) for z in zonage_detail]
    cols = [_couleur_typezone(tz) for tz in typezones]

    legend_lines = []
    for z in zonage_detail:
        tz = str(z.get("typezone", "?"))
        pct = float(z.get("proportion_pct", 0) or 0)
        lib = (z.get("libelong") or z.get("libelle") or "").strip() or "—"
        if len(lib) > 48:
            lib = lib[:45] + "…"
        legend_lines.append(f"{tz}  ·  {pct:.1f} %  ·  {lib}")

    # Imports lazy
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches

    fig_w = max(5.2, width_px / 100.0)
    fig_h = max(3.2, height_px / 100.0)
    fig = plt.figure(figsize=(fig_w, fig_h), facecolor="#ffffff")
    fig.patch.set_alpha(1.0)

    fig.suptitle(
        "Répartition du zonage PLU",
        fontsize=12,
        fontweight="bold",
        color="#1e293b",
        y=0.97,
        ha="center",
    )
    fig.text(
        0.5,
        0.905,
        "Surface de la parcelle (ou union) intersectée par type de zone — source GPU / Géoportail de l'urbanisme",
        ha="center",
        va="top",
        fontsize=7.5,
        color="#64748b",
    )

    # Grille : graphique à gauche, légende à droite
    gs = fig.add_gridspec(
        1, 2,
        left=0.06,
        right=0.98,
        top=0.82,
        bottom=0.10,
        wspace=0.35,
        width_ratios=[1.05, 1.0],
    )
    ax_pie = fig.add_subplot(gs[0, 0])
    ax_leg = fig.add_subplot(gs[0, 1])

    wedges, _txt, autotexts = ax_pie.pie(
        sizes,
        labels=None,
        colors=cols,
        autopct=lambda p: (f"{p:.0f} %" if p >= 8 else ""),
        startangle=90,
        counterclock=False,
        pctdistance=0.72,
        wedgeprops={"edgecolor": "#ffffff", "linewidth": 1.2},
        textprops={"fontsize": 7, "color": "#1e293b", "fontweight": "bold"},
    )
    for t in autotexts:
        if t.get_text():
            t.set_color("#1e293b")

    ax_pie.axis("equal")
    ax_pie.set_title("Part par typezone", fontsize=8, color="#475569", pad=6)

    handles = [
        mpatches.Patch(facecolor=cols[i], edgecolor="#ffffff", linewidth=0.8, label=legend_lines[i])
        for i in range(len(legend_lines))
    ]
    ax_leg.axis("off")
    ax_leg.legend(
        handles=handles,
        loc="upper left",
        frameon=True,
        fancybox=False,
        title="Légende (typezone · % · libellé)",
        title_fontsize=7.5,
        fontsize=6.8,
        labelspacing=0.9,
        borderpad=0.8,
        facecolor="#f8fafc",
        edgecolor="#e2e8f0",
        framealpha=1.0,
    )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=140, bbox_inches="tight", facecolor="#ffffff", edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ============================================================
# 1. GÉOMÉTRIE DE LA PARCELLE (WFS IGN)
# ============================================================
def fetch_parcelle(idu: str, verbose: bool = False) -> gpd.GeoDataFrame:
    """Récupère la géométrie d'une parcelle par son IDU."""
    code_insee, section, numero = decompose_idu(idu)
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": LAYER_PARCELLE,
        "srsName": SRS,
        "outputFormat": "application/json",
        # Filtre WFS IGN attendu : code_insee / section / numero
        "CQL_FILTER": (
            "code_insee='{code_insee}' AND section='{section}' AND numero='{numero}'"
        ).format(code_insee=code_insee, section=section, numero=numero),
    }
    if verbose:
        print(
            f"  📡 Fetch parcelle IDU={idu} → "
            f"code_insee={code_insee}, section={section}, numero={numero}"
        )

    r = requests.get(WFS_IGN, params=params, timeout=60)
    r.raise_for_status()
    gdf = gpd.read_file(io.BytesIO(r.content))

    if gdf.empty:
        raise ValueError(f"Parcelle introuvable pour IDU={idu}")

    gdf = gdf.to_crs(SRS)
    if verbose:
        area = gdf.geometry.iloc[0].area
        print(f"  ✅ Parcelle trouvée : {area:.0f} m² ({area/10000:.4f} ha)")
    return gdf


# ============================================================
# 2. FETCH ZONES URBA (WFS GPU) avec carroyage adaptatif
# ============================================================
def _count_features_gpu(bbox_wgs84: tuple) -> int:
    """Compte les entités de zone_urba dans une bbox WGS84."""
    minx, miny, maxx, maxy = bbox_wgs84
    url = (
        f"{WFS_GPU}?service=WFS&version=2.0.0&request=GetFeature"
        f"&typeNames={LAYER_ZONE_URBA}"
        f"&bbox={minx},{miny},{maxx},{maxy},EPSG:4326"
        f"&resultType=hits"
    )
    try:
        r = requests.get(url, timeout=30)
        import re
        match = re.search(r'numberMatched="(\d+)"', r.text)
        return int(match.group(1)) if match else 0
    except Exception:
        return 0


def _fetch_zones_bbox(bbox_wgs84: tuple, depth: int = 0,
                      seen_ids: set = None, verbose: bool = False) -> list:
    """Fetch récursif des zones urba sur une bbox WGS84."""
    if seen_ids is None:
        seen_ids = set()

    indent = "  " * depth
    minx, miny, maxx, maxy = bbox_wgs84
    count = _count_features_gpu(bbox_wgs84)

    if verbose:
        print(f"{indent}↳ [lvl {depth}] BBOX=({minx:.4f},{miny:.4f},{maxx:.4f},{maxy:.4f}) → {count} entités")

    if count == 0:
        return []

    if count >= LIMIT_CAP:
        if verbose:
            print(f"{indent}  ⚠️ Cap atteint, subdivision en 4...")
        midx = (minx + maxx) / 2
        midy = (miny + maxy) / 2
        sub_bboxes = [
            (minx, miny, midx, midy),
            (midx, miny, maxx, midy),
            (minx, midy, midx, maxy),
            (midx, midy, maxx, maxy),
        ]
        results = []
        for sub in sub_bboxes:
            results.extend(_fetch_zones_bbox(sub, depth + 1, seen_ids, verbose))
            time.sleep(0.5)
        return results

    # Fetch effectif
    url = (
        f"{WFS_GPU}?service=WFS&version=2.0.0&request=GetFeature"
        f"&typeNames={LAYER_ZONE_URBA}"
        f"&bbox={minx},{miny},{maxx},{maxy},EPSG:4326"
        f"&outputFormat=application/json"
    )
    try:
        gdf = gpd.read_file(url)
    except Exception as e:
        if verbose:
            print(f"{indent}  ⚠️ Erreur lecture : {e}")
        return []

    if gdf.empty:
        return []

    keep_cols = [c for c in ["id", "libelle", "libelong", "typezone", "insee", "geometry"] if c in gdf.columns]
    gdf = gdf[keep_cols].copy()

    # Dédoublonnage
    if "id" in gdf.columns:
        gdf = gdf[~gdf["id"].isin(seen_ids)]
        seen_ids.update(gdf["id"].tolist())

    if verbose and not gdf.empty:
        print(f"{indent}  ✅ {len(gdf)} nouvelles zones récupérées")

    return [gdf] if not gdf.empty else []


def fetch_zones_urba(parcelle_gdf: gpd.GeoDataFrame, verbose: bool = False) -> gpd.GeoDataFrame:
    """
    Récupère les zones urba autour d'une parcelle.
    Convertit la bbox Lambert93 → WGS84 pour le WFS GPU.
    """
    # Bbox en WGS84 pour le WFS GPU
    parcelle_wgs84 = parcelle_gdf.to_crs(SRS_WGS84)
    minx, miny, maxx, maxy = parcelle_wgs84.total_bounds
    # Léger buffer pour ne pas rater les zones aux bords
    buf = 0.001  # ~100m
    bbox_wgs84 = (minx - buf, miny - buf, maxx + buf, maxy + buf)

    if verbose:
        print(f"  📡 Fetch zones urba (bbox WGS84={bbox_wgs84})")

    gdfs = _fetch_zones_bbox(bbox_wgs84, verbose=verbose)

    if not gdfs:
        return gpd.GeoDataFrame()

    zones = pd.concat(gdfs, ignore_index=True)
    zones = gpd.GeoDataFrame(zones, geometry="geometry", crs=SRS_WGS84)
    zones = zones.to_crs(SRS)

    if verbose:
        print(f"  ✅ {len(zones)} zones urba récupérées au total")

    return zones


# ============================================================
# 3. INTERSECTION PARCELLE × ZONES
# ============================================================
def intersect_parcelle_zones(parcelle_gdf: gpd.GeoDataFrame,
                              zones_gdf: gpd.GeoDataFrame,
                              verbose: bool = False) -> pd.DataFrame:
    """
    Intersecte la géométrie de la parcelle avec les zones PLU.
    Retourne un DataFrame avec surface et proportion par typezone.
    """
    if zones_gdf.empty:
        return pd.DataFrame()

    geom_parcelle = parcelle_gdf.geometry.iloc[0]
    surface_totale = geom_parcelle.area

    rows = []
    for _, zone in zones_gdf.iterrows():
        try:
            inter = geom_parcelle.intersection(zone.geometry)
            surface_inter = inter.area
            if surface_inter < 1.0:  # < 1 m² → ignoré
                continue
            rows.append({
                "typezone": zone.get("typezone", ""),
                "libelle": zone.get("libelle", ""),
                "libelong": zone.get("libelong", ""),
                "insee": zone.get("insee", ""),
                "surface_m2": round(surface_inter, 1),
                "proportion": round(surface_inter / surface_totale * 100, 2),
            })
        except Exception:
            continue

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # Grouper par typezone (une parcelle peut chevaucher plusieurs polygones du même type)
    df = (
        df.groupby("typezone", as_index=False)
        .agg(
            libelle=("libelle", "first"),
            libelong=("libelong", "first"),
            insee=("insee", "first"),
            surface_m2=("surface_m2", "sum"),
            proportion=("proportion", "sum"),
        )
        .sort_values("proportion", ascending=False)
        .reset_index(drop=True)
    )

    if verbose:
        print(f"\n  📊 Zonage de la parcelle ({surface_totale:.0f} m²) :")
        for _, row in df.iterrows():
            print(f"     {row['typezone']:6s} | {row['proportion']:6.1f}% | {row['surface_m2']:.0f} m² | {row['libelong']}")

    return df


# ============================================================
# 4. SCORING URBANISTIQUE
# ============================================================
def compute_urba_score(zonage_df: pd.DataFrame, verbose: bool = False) -> dict:
    """
    Calcule un score PLU pondéré sur 10 points.

    Principe : moyenne pondérée des scores par typezone,
    avec les proportions comme poids.
    Une surtaxe est appliquée si une zone AU représente ≥ 20% de la parcelle
    (même minoritaire, la valeur spéculative dégrade fortement le score).
    """
    if zonage_df.empty:
        return {
            "score_urba": None,
            "score_label": "Non déterminé",
            "zonage_dominant": None,
            "zonage_detail": [],
            "alerte": "Aucun zonage PLU trouvé pour cette parcelle"
        }

    score_pondere = 0.0
    total_proportion = 0.0
    alerte = None
    detail = []

    for _, row in zonage_df.iterrows():
        typezone = row["typezone"]
        proportion = row["proportion"]
        score_zone = get_score_zone(typezone)

        score_pondere += score_zone * proportion
        total_proportion += proportion

        detail.append({
            "typezone": typezone,
            "libelong": row.get("libelong", ""),
            "proportion_pct": round(proportion, 1),
            "score_zone": score_zone,
        })

        # Alerte zone AU significative (≥ 20%)
        if typezone.startswith("AU") or typezone.startswith("1AU"):
            if proportion >= 20:
                alerte = f"Zone {typezone} représente {proportion:.1f}% de la parcelle — valeur spéculative forte"

    # Normalisation (au cas où total_proportion ≠ 100 à cause de gaps géométriques)
    if total_proportion > 0:
        score_final = round(score_pondere / total_proportion, 2)
    else:
        score_final = SCORE_DEFAULT

    # Libellé interprétatif
    if score_final >= 8:
        label = "Favorable — zonage naturel/agricole protégé, idéal compensation"
    elif score_final >= 6:
        label = "Correct — usage agricole ou naturel, peu de pression spéculative"
    elif score_final >= 4:
        label = "Modéré — zonage mixte ou naturel avec contraintes"
    elif score_final >= 2:
        label = "Défavorable — zonage urbain ou à urbaniser, valeur élevée"
    else:
        label = "Rédhibitoire — fort potentiel spéculatif"

    # Zone dominante
    zonage_dominant = zonage_df.iloc[0]["typezone"] if not zonage_df.empty else None

    if verbose:
        print(f"\n  🏙️  Score urbanistique : {score_final}/10 — {label}")
        if alerte:
            print(f"  ⚠️  Alerte : {alerte}")

    return {
        "score_urba": score_final,
        "score_label": label,
        "zonage_dominant": zonage_dominant,
        "zonage_detail": detail,
        "alerte": alerte,
    }


# ============================================================
# 5. FONCTION PRINCIPALE — IDU unique
# ============================================================
def run_urba(idu: str, verbose: bool = False) -> dict:
    """
    Analyse urbanistique d'une parcelle unique.
    Retourne un dict prêt à être injecté dans le contexte JSON du scoring.
    """
    try:
        parcelle_gdf = fetch_parcelle(idu, verbose=verbose)
        zones_gdf = fetch_zones_urba(parcelle_gdf, verbose=verbose)
        zonage_df = intersect_parcelle_zones(parcelle_gdf, zones_gdf, verbose=verbose)
        result = compute_urba_score(zonage_df, verbose=verbose)
        result["idu"] = idu
        result["surface_parcelle_m2"] = round(parcelle_gdf.geometry.iloc[0].area, 1)
        return result
    except Exception as e:
        return {
            "idu": idu,
            "score_urba": None,
            "score_label": "Erreur",
            "zonage_dominant": None,
            "zonage_detail": [],
            "alerte": str(e),
            "surface_parcelle_m2": None,
        }


# ============================================================
# 6. FONCTION MULTI-IDU (union géométrique des parcelles)
# ============================================================
def run_urba_multi(idus: list[str], verbose: bool = False) -> dict:
    """
    Analyse urbanistique pour un ensemble de parcelles (ex : toutes les parcelles d'un SIREN).

    Stratégie : on récupère chaque parcelle individuellement, on fusionne leurs
    géométries en une union, puis on fetch les zones PLU sur la bbox globale et on
    intersecte l'union — ce qui donne le zonage pondéré sur la surface totale.

    Le résultat inclut aussi le détail par parcelle individuelle pour diagnostic.
    """
    if not idus:
        return {"erreur": "Aucun IDU fourni"}

    if verbose:
        print(f"\n  🗂️  Multi-IDU : {len(idus)} parcelles à traiter")

    # --- 1. Fetch de toutes les parcelles ---
    parcelles = []
    erreurs = []
    for idu in idus:
        try:
            gdf = fetch_parcelle(idu, verbose=verbose)
            gdf["idu"] = idu
            parcelles.append(gdf)
        except Exception as e:
            erreurs.append({"idu": idu, "erreur": str(e)})
            if verbose:
                print(f"  ⚠️  IDU={idu} introuvable : {e}")

    if not parcelles:
        return {
            "idus": idus,
            "score_urba": None,
            "score_label": "Erreur",
            "zonage_dominant": None,
            "zonage_detail": [],
            "alerte": "Aucune parcelle récupérée",
            "erreurs": erreurs,
        }

    # --- 2. Union géométrique ---
    all_parcelles_gdf = pd.concat(parcelles, ignore_index=True)
    all_parcelles_gdf = gpd.GeoDataFrame(all_parcelles_gdf, crs=SRS)

    # Géométrie union (dissolve) : représente l'emprise totale
    union_gdf = all_parcelles_gdf.dissolve().reset_index(drop=True)
    surface_totale = union_gdf.geometry.iloc[0].area

    if verbose:
        print(f"\n  🔗 Union : {len(parcelles)} parcelles → {surface_totale:.0f} m² ({surface_totale/10000:.4f} ha)")

    # --- 3. Fetch zones PLU sur la bbox globale ---
    zones_gdf = fetch_zones_urba(union_gdf, verbose=verbose)

    # --- 4. Intersection union × zones (score global) ---
    zonage_df = intersect_parcelle_zones(union_gdf, zones_gdf, verbose=verbose)
    result_global = compute_urba_score(zonage_df, verbose=verbose)

    # --- 5. Détail par parcelle individuelle (pour diagnostic) ---
    detail_par_parcelle = []
    for gdf in parcelles:
        idu = gdf["idu"].iloc[0]
        try:
            # Réutilise les zones déjà fetchées si elles couvrent la parcelle
            # (la bbox globale les englobe toutes)
            zonage_ind = intersect_parcelle_zones(gdf, zones_gdf, verbose=False)
            score_ind = compute_urba_score(zonage_ind, verbose=False)
            detail_par_parcelle.append({
                "idu": idu,
                "surface_m2": round(gdf.geometry.iloc[0].area, 1),
                "zonage_dominant": score_ind["zonage_dominant"],
                "score_urba": score_ind["score_urba"],
                "zonage_detail": score_ind["zonage_detail"],
            })
        except Exception as e:
            detail_par_parcelle.append({"idu": idu, "erreur": str(e)})

    result_global["idus"] = idus
    result_global["nb_parcelles"] = len(parcelles)
    result_global["surface_totale_m2"] = round(surface_totale, 1)
    result_global["detail_par_parcelle"] = detail_par_parcelle
    if erreurs:
        result_global["erreurs"] = erreurs

    return result_global


# ============================================================
# MAIN (tests standalone)
# ============================================================
def _print_result(result: dict):
    """Affichage formaté d'un résultat urba."""
    idus = result.get("idus") or [result.get("idu", "?")]
    nb = result.get("nb_parcelles", 1)
    surface = result.get("surface_totale_m2") or result.get("surface_parcelle_m2", "N/A")

    print(f"\n{'='*60}")
    print(f"  RÉSULTAT SCORING URBANISTIQUE")
    print(f"{'='*60}")
    if nb > 1:
        print(f"  Parcelles        : {nb} IDUs")
        print(f"  Surface totale   : {surface} m²")
    else:
        print(f"  IDU              : {idus[0]}")
        print(f"  Surface          : {surface} m²")
    print(f"  Zone dominante   : {result.get('zonage_dominant', 'N/A')}")
    print(f"  Score urba       : {result.get('score_urba', 'N/A')} / 10")
    print(f"  Interprétation   : {result.get('score_label', 'N/A')}")
    if result.get("alerte"):
        print(f"  ⚠️  Alerte        : {result['alerte']}")

    print(f"\n  Zonage global (surface union) :")
    for z in result.get("zonage_detail", []):
        print(f"    {z['typezone']:8s} | {z['proportion_pct']:5.1f}% | score={z['score_zone']}/10 | {z['libelong']}")

    if result.get("detail_par_parcelle"):
        print(f"\n  Détail par parcelle :")
        for p in result["detail_par_parcelle"]:
            if "erreur" in p:
                print(f"    {p['idu']} → ⚠️  {p['erreur']}")
            else:
                zones_str = ", ".join(
                    f"{z['typezone']} {z['proportion_pct']}%"
                    for z in p.get("zonage_detail", [])
                )
                print(f"    {p['idu']} | {p['surface_m2']} m² | {p['zonage_dominant']} | score={p['score_urba']}/10 | {zones_str}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyse urbanistique par IDU")
    parser.add_argument(
        "--idu",
        nargs="+",
        required=True,
        help="Un ou plusieurs IDUs (ex: --idu 862750000D0319 862750000D0320)"
    )
    parser.add_argument("--verbose", action="store_true", help="Affichage détaillé")
    parser.add_argument(
        "--chart-out",
        metavar="FICHIER.png",
        help="Écrit le camembert de répartition zonage (PNG) vers ce fichier",
    )
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  MODULE URBA — {len(args.idu)} parcelle(s)")
    print(f"{'='*60}\n")

    if len(args.idu) == 1:
        result = run_urba(args.idu[0], verbose=args.verbose)
    else:
        result = run_urba_multi(args.idu, verbose=args.verbose)

    _print_result(result)

    if args.chart_out:
        zd = result.get("zonage_detail") or []
        png = generer_camembert_zonage_png(zd, width_px=640, height_px=420)
        if png:
            Path(args.chart_out).write_bytes(png)
            print(f"Camembert PNG écrit : {args.chart_out}")
        else:
            print("Aucun zonage exploitable : camembert non généré (zonage_detail vide ou proportions nulles).")