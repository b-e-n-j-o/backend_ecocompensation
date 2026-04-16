# -*- coding: utf-8 -*-
"""
sup.py — Module servitudes d'utilité publique (SUP) par IDU(s)
Pipeline : IDU(s) → géométrie parcelle(s) (WFS IGN) → SUP assiettes (WFS GPU) → intersection → restitution

Usage standalone :
    python sup.py --idu 862750000D0319
    python sup.py --idu 862750000D0319 862750000D0320 --verbose
"""

import io
import time
import argparse
import requests
import geopandas as gpd
import pandas as pd
from shapely.geometry import box
from shapely.ops import unary_union

from rpg import decompose_idu
from wfs_utils import harvest_adaptive_with_owslib, dedup_on_id_or_geom

# ============================================================
# CONFIG
# ============================================================
WFS_IGN  = "https://data.geopf.fr/wfs/ows"
WFS_GPU  = "https://data.geopf.fr/annexes/ressources/wfs/gpu.xml"

LAYER_PARCELLE  = "CADASTRALPARCELS.PARCELLAIRE_EXPRESS:parcelle"
LAYER_ASSIETTE  = "wfs_sup:assiette_sup_s"   # polygones (principale couche utile)
LAYER_ASSIETTE_L = "wfs_sup:assiette_sup_l"  # linéaires (ex : lignes HT)
LAYER_ASSIETTE_P = "wfs_sup:assiette_sup_p"  # ponctuels

SRS      = "EPSG:2154"
SRS_WGS84 = "EPSG:4326"
CAP      = 5000   # seuil carroyage adaptatif
BUF_DEG  = 0.002  # ~200m de buffer autour de la bbox pour ne pas rater les SUP aux bords

# Libellés des familles de SUP (source : code de l'urbanisme, annexe)
FAMILLES_SUP = {
    "ac1": "Monuments historiques",
    "ac2": "Sites inscrits / classés",
    "ac4": "Sites patrimoniaux remarquables",
    "as1": "Zones de protection eau potable",
    "as2": "Zones de protection eau thermale",
    "as3": "Salubrité publique",
    "az1": "Zones inondables (PPRi)",
    "az2": "Plans de prévention risques naturels",
    "az3": "Plans de prévention risques technologiques",
    "az7": "Risques miniers",
    "az13": "Risques sismiques",
    "b1":  "Forêts de protection",
    "b2":  "Forêts domaniales",
    "e1":  "Réseaux de distribution énergie",
    "e3":  "Transport gaz",
    "e4":  "Transport électricité (lignes HT)",
    "e7":  "Canalisations hydrocarbures",
    "ht2": "Lignes haute tension",
    "i4":  "Voies ferrées",
    "pt1": "Voies de télécommunications",
    "pt2": "Câbles sous-marins",
    "t1":  "Voies nationales",
    "t5":  "Voies navigables",
    "t7":  "Aérodromes",
    "t8":  "Servitudes aéronautiques",
    "pm1": "Plans de prévention submersion marine",
    "pm2": "Plans de prévention tsunamis",
    "int": "Servitudes militaires",
}


def get_famille_label(suptype: str) -> str:
    return FAMILLES_SUP.get(suptype.lower(), f"Servitude {suptype.upper()}")


# ============================================================
# 1. GÉOMÉTRIE DE LA/DES PARCELLE(S)
# ============================================================
def fetch_parcelle(idu: str, verbose: bool = False) -> gpd.GeoDataFrame:
    code_insee, section, numero = decompose_idu(idu)
    params = {
        "service":      "WFS",
        "version":      "2.0.0",
        "request":      "GetFeature",
        "typeNames":    LAYER_PARCELLE,
        "srsName":      SRS,
        "outputFormat": "application/json",
        "CQL_FILTER":   f"code_insee='{code_insee}' AND section='{section}' AND numero='{numero}'",
    }
    if verbose:
        print(f"  📡 Parcelle IDU={idu} → code_insee={code_insee}, section={section}, numero={numero}")

    r = requests.get(WFS_IGN, params=params, timeout=60)
    r.raise_for_status()
    gdf = gpd.read_file(io.BytesIO(r.content))

    if gdf.empty:
        raise ValueError(f"Parcelle introuvable : IDU={idu}")

    gdf = gdf.to_crs(SRS)
    gdf["idu"] = idu
    if verbose:
        print(f"  ✅ {idu} : {gdf.geometry.iloc[0].area:.0f} m²")
    return gdf


def fetch_parcelles(idus: list[str], verbose: bool = False) -> gpd.GeoDataFrame:
    """Récupère et concatène plusieurs parcelles."""
    parts = []
    for idu in idus:
        try:
            parts.append(fetch_parcelle(idu, verbose=verbose))
        except Exception as e:
            print(f"  ⚠️  IDU={idu} introuvable : {e}")
    if not parts:
        raise ValueError("Aucune parcelle récupérée.")
    return pd.concat(parts, ignore_index=True)


# ============================================================
# 2. FETCH ASSIETTES SUP (toutes familles) via wfs_utils
# ============================================================
def _bbox_wgs84_from_gdf(gdf: gpd.GeoDataFrame, buf: float = BUF_DEG) -> tuple:
    """Retourne la bbox WGS84 étendue d'un GeoDataFrame Lambert93."""
    w = gdf.to_crs(SRS_WGS84)
    minx, miny, maxx, maxy = w.total_bounds
    return (minx - buf, miny - buf, maxx + buf, maxy + buf)


def fetch_sup_assiettes(parcelles_gdf: gpd.GeoDataFrame,
                         verbose: bool = False) -> gpd.GeoDataFrame:
    """
    Récupère toutes les assiettes SUP surfaciques sur la bbox des parcelles.
    Utilise harvest_adaptive_with_owslib (carroyage adaptatif, dédup intégré).
    """
    bbox = _bbox_wgs84_from_gdf(parcelles_gdf)
    if verbose:
        print(f"\n  📡 Fetch SUP assiettes surfaciques (bbox WGS84={tuple(round(v,4) for v in bbox)})")

    gdf, stats = harvest_adaptive_with_owslib(
        WFS_GPU, LAYER_ASSIETTE,
        bbox=bbox, srs=SRS_WGS84,
        cap=CAP, max_level=6, sleep_s=0.3
    )

    if gdf.empty:
        if verbose:
            print("  ℹ️  Aucune assiette SUP surfacique dans cette zone.")
        return gpd.GeoDataFrame()

    gdf = dedup_on_id_or_geom(gdf)
    gdf = gdf.to_crs(SRS)

    if verbose:
        suptypes = gdf["suptype"].unique() if "suptype" in gdf.columns else []
        print(f"  ✅ {len(gdf)} assiettes SUP récupérées — familles : {sorted(suptypes)}")

    return gdf


# ============================================================
# 3. INTERSECTION PARCELLE(S) × ASSIETTES SUP
# ============================================================
def intersect_sup(parcelles_gdf: gpd.GeoDataFrame,
                  sup_gdf: gpd.GeoDataFrame,
                  verbose: bool = False) -> pd.DataFrame:
    """
    Intersecte chaque assiette SUP avec l'union des parcelles.
    Retourne un DataFrame avec les SUP qui touchent effectivement les parcelles,
    leur surface d'intersection, et leur proportion de couverture.
    """
    if sup_gdf.empty:
        return pd.DataFrame()

    # Union géométrique des parcelles (évite les doubles comptes)
    union_geom = parcelles_gdf.dissolve().geometry.iloc[0]
    surface_totale = union_geom.area

    # Regroupe par (suptype, famille, nomsuite) avant d'intersecter
    # afin d'éviter l'addition double quand plusieurs polygones SUP se recouvrent.
    sup = sup_gdf.copy()
    sup["famille"] = sup.get("suptype", "").apply(get_famille_label) if "suptype" in sup.columns else ""
    if "nomsuplitt" not in sup.columns:
        # fallback sur libelle si disponible (comme dans l'ancien code)
        sup["nomsuplitt"] = sup.get("libelle", "") if "libelle" in sup.columns else ""
    else:
        sup["nomsuplitt"] = sup["nomsuplitt"].fillna("")

    rows = []
    if "suptype" not in sup.columns:
        return pd.DataFrame()

    for (suptype, famille, nomsuite), grp in sup.groupby(["suptype", "famille", "nomsuplitt"], dropna=False):
        try:
            geom_union = unary_union(list(grp.geometry))
            inter = union_geom.intersection(geom_union)
            surface_inter = inter.area
            if surface_inter < 1.0:  # < 1 m² ignoré
                continue

            rows.append({
                "suptype": suptype,
                "famille": famille,
                "nomsuplitt": nomsuite,
                "nb_assiettes": len(grp),
                "surface_m2": round(surface_inter, 1),
                "couverture_pct": round(surface_inter / surface_totale * 100, 2) if surface_totale else 0.0,
            })
        except Exception:
            continue

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows).sort_values("couverture_pct", ascending=False).reset_index(drop=True)

    # Clamp à 100% (artefacts géométriques possibles, mais on évite surtout les >100 dus au double-count)
    df["couverture_pct"] = df["couverture_pct"].clip(upper=100.0)

    if verbose:
        print(f"\n  📋 SUP intersectant la/les parcelle(s) ({surface_totale:.0f} m² totaux) :")
        for _, row in df.iterrows():
            print(
                f"     [{row['suptype']:6s}] {row['famille']:<40s} | "
                f"{row['couverture_pct']:5.1f}% | {row['surface_m2']:.0f} m² | "
                f"{row['nomsuplitt']}"
            )

    return df


# ============================================================
# 4. FONCTION PRINCIPALE
# ============================================================
def run_sup(idus: list[str] | str, verbose: bool = False) -> dict:
    """
    Point d'entrée principal.
    Accepte un IDU unique (str) ou une liste d'IDUs.
    Retourne un dict prêt à injecter dans le contexte JSON du scoring.
    """
    if isinstance(idus, str):
        idus = [idus]

    try:
        parcelles_gdf = fetch_parcelles(idus, verbose=verbose)
        sup_gdf = fetch_sup_assiettes(parcelles_gdf, verbose=verbose)
        sup_df = intersect_sup(parcelles_gdf, sup_gdf, verbose=verbose)

        surface_totale = parcelles_gdf.dissolve().geometry.iloc[0].area

        if sup_df.empty:
            servitudes = []
            familles_presentes = []
            note = "Aucune servitude d'utilité publique détectée sur ces parcelles."
        else:
            servitudes = sup_df.to_dict(orient="records")
            familles_presentes = sorted(sup_df["suptype"].unique().tolist())
            note = None

        return {
            "idus":               idus,
            "nb_parcelles":       len(parcelles_gdf),
            "surface_totale_m2":  round(surface_totale, 1),
            "nb_sup":             len(sup_df),
            "familles_presentes": familles_presentes,
            "servitudes":         servitudes,
            "note":               note,
        }

    except Exception as e:
        return {
            "idus":               idus,
            "nb_parcelles":       0,
            "surface_totale_m2":  None,
            "nb_sup":             0,
            "familles_presentes": [],
            "servitudes":         [],
            "note":               f"Erreur : {e}",
        }


# ============================================================
# MAIN
# ============================================================
def _print_result(result: dict):
    nb = result.get("nb_parcelles", 0)
    surface = result.get("surface_totale_m2", "N/A")
    nb_sup = result.get("nb_sup", 0)

    print(f"\n{'='*65}")
    print(f"  RÉSULTAT SUP")
    print(f"{'='*65}")
    print(f"  Parcelles        : {nb} IDU(s) — {surface} m²")
    print(f"  SUP détectées    : {nb_sup}")
    if result.get("familles_presentes"):
        print(f"  Familles         : {', '.join(result['familles_presentes'])}")
    if result.get("note"):
        print(f"  ℹ️   {result['note']}")

    servitudes = result.get("servitudes", [])
    if servitudes:
        print(f"\n  {'SUPTYPE':<8} {'FAMILLE':<40} {'COUV m²':>9}  NOM")
        print(f"  {'-'*8} {'-'*40} {'-'*9}  {'-'*20}")
        for s in servitudes:
            print(
                f"  {s['suptype']:<8} {s['famille']:<40} {s['surface_m2']:>9.1f}  {s['nomsuplitt']}"
            )
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyse SUP par IDU(s)")
    parser.add_argument(
        "--idu", nargs="+", required=True,
        help="Un ou plusieurs IDUs (ex: --idu 862750000D0319 862750000D0320)"
    )
    parser.add_argument("--verbose", action="store_true", help="Affichage détaillé")
    args = parser.parse_args()

    print(f"\n{'='*65}")
    print(f"  MODULE SUP — {len(args.idu)} parcelle(s)")
    print(f"{'='*65}\n")

    result = run_sup(args.idu, verbose=args.verbose)
    _print_result(result)