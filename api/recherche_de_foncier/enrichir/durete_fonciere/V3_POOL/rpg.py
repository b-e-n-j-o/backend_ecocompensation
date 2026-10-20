# -*- coding: utf-8 -*-
"""
rpg.py — Module RPG (Registre Parcellaire Graphique) — Kerelia Dureté Foncière
===============================================================================
Interroge les flux WFS IGN multi-millésimes (2010-2024) pour reconstituer
l'historique d'occupation agricole d'une parcelle cadastrale.

Expose :
    decompose_idu(idu)                            -> (insee, section, numero)
    fetch_rpg_parcelle(insee, section, numero)     -> dict  (summary + by_year)
    scorer_surcharge_bail_rural(rpg, ann, sirene)  -> (int, str)
    _load_nomenclature()                           -> dict

Structure de retour fetch_rpg_parcelle :
    {
        "summary": {
            "occupation_agricole":  bool,
            "nb_annees_agricoles":  int,
            "annees_agricoles":     list[int],
            "continuite_forte":     bool,   # ≥5 années consécutives
            "bail_rural_certain":   bool,   # ≥5 ans consécutifs déclarés PAC
            "bail_rural_probable":  bool,   # 3-4 ans consécutifs ou >50% années
            "codes_cultures":       list[str],
        },
        "by_year": {
            2020: {"status": "agricole", "total_m2": 12345, "cultures": {"BTH": 12345}},
            2021: {"status": "non_declare"},
            ...
        }
    }
"""

import io
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Optional

import requests
import geopandas as gpd
from shapely.ops import unary_union

log = logging.getLogger("rpg")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

WFS_URL      = "https://data.geopf.fr/wfs/ows"
SRS_CADASTRE = "EPSG:2154"
SRS_RPG      = "EPSG:3857"
BUFFER_M     = 150
WFS_COUNT    = 5000

# Millésimes disponibles + nom de couche + schéma de colonnes
RPG_LAYERS = {
    2010: ("RPG.2010:rpg_2010",              "old"),
    2013: ("RPG.2013:rpg_2013",              "old"),
    2014: ("RPG.2014:rpg_2014",              "old"),
    2015: ("RPG.2015:parcelles_graphiques",  "mid"),
    2016: ("RPG.2016:parcelles_graphiques",  "mid"),
    2017: ("RPG.2017:parcelles_graphiques",  "mid"),
    2018: ("RPG.2018:parcelles_graphiques",  "mid"),
    2019: ("RPG.2019:parcelles_graphiques",  "mid"),
    2020: ("RPG.2020:parcelles_graphiques",  "mid"),
    2021: ("RPG.2021:parcelles_graphiques",  "new"),
    2022: ("RPG.2022:parcelles_graphiques",  "new"),
    2023: ("RPG.2023:parcelles_graphiques",  "new"),
    2024: ("RPG.2024:parcelles_graphiques",  "new"),
}

# Chemin nomenclature (même répertoire que rpg.py)
NOMENCLATURE_PATH = Path(__file__).parent / "rpg_nomenclature.json"


# ---------------------------------------------------------------------------
# Nomenclature
# ---------------------------------------------------------------------------

_nomenclature_cache: Optional[dict] = None


def _load_nomenclature() -> dict:
    global _nomenclature_cache
    if _nomenclature_cache is not None:
        return _nomenclature_cache

    if not NOMENCLATURE_PATH.exists():
        log.warning(f"Nomenclature RPG introuvable : {NOMENCLATURE_PATH} — libellés dégradés")
        _nomenclature_cache = {
            "code_cultu": {}, "code_group": {}, "culture_d1": {}, "culture_d2": {}
        }
        return _nomenclature_cache

    with open(NOMENCLATURE_PATH, encoding="utf-8") as f:
        raw = json.load(f)

    def clean(d):
        return {k: v.replace("\n", " ").strip() for k, v in d.items()}

    _nomenclature_cache = {
        "code_cultu": clean(raw.get("code_cultu", {})),
        "code_group": clean(raw.get("code_group", {})),
        "culture_d1": clean(raw.get("culture_d1", {})),
        "culture_d2": clean(raw.get("culture_d2", {})),
    }
    return _nomenclature_cache


# ---------------------------------------------------------------------------
# Décomposition IDU
# ---------------------------------------------------------------------------

def decompose_idu(idu: str) -> tuple[str, str, str]:
    """
    Décompose un IDU en (insee, section, numero).

    Formats supportés :
      - MAJIC public   : 86275000D0319  → section '0D'
      - DVF Etalab     : 862750000E0452 → section '0E'
      - Section double : 86070000AN0063 → section 'AN'

    Règle IGN WFS :
      - 1 lettre  → préfixe '0'  (ex: D → '0D')
      - 2 lettres → sans préfixe (ex: AN → 'AN')
    """
    idu = idu.strip().upper().replace(" ", "")
    if len(idu) < 10:
        raise ValueError(f"IDU trop court : '{idu}'")
    insee       = idu[:5]
    numero      = idu[-4:]
    milieu      = idu[5:-4]
    section_raw = milieu.lstrip("0") or milieu[-1]
    section     = ("0" + section_raw) if len(section_raw) == 1 else section_raw
    log.debug("decompose_idu(%s) → insee=%s section=%s numero=%s", idu, insee, section, numero)
    return insee, section, numero


# ---------------------------------------------------------------------------
# Fetch géométrie parcelle cadastrale
# ---------------------------------------------------------------------------

def _fetch_geom_parcelle(insee: str, section: str, numero: str):
    """Retourne la géométrie Shapely (EPSG:3857) de la parcelle via WFS IGN."""
    cql = f"code_insee='{insee}' AND section='{section}' AND numero='{numero}'"
    params = {
        "service":      "WFS",
        "version":      "2.0.0",
        "request":      "GetFeature",
        "typeNames":    "CADASTRALPARCELS.PARCELLAIRE_EXPRESS:parcelle",
        "srsName":      SRS_CADASTRE,
        "outputFormat": "application/json",
        "CQL_FILTER":   cql,
    }
    r = requests.get(WFS_URL, params=params, timeout=30)
    r.raise_for_status()
    gdf = gpd.read_file(io.BytesIO(r.content))
    if gdf.empty:
        raise ValueError(f"Parcelle introuvable : insee={insee} section={section} numero={numero}")
    return unary_union(gdf.to_crs(3857).geometry)


# ---------------------------------------------------------------------------
# Fetch RPG pour une année
# ---------------------------------------------------------------------------

def _fetch_rpg_year(typename: str, bbox: tuple, year: int):
    """Télécharge les ilots RPG autour de la bbox. Retourne un GeoDataFrame ou None."""
    params = {
        "service":      "WFS",
        "version":      "2.0.0",
        "request":      "GetFeature",
        "typeName":     typename,
        "outputFormat": "application/json",
        "srsName":      SRS_RPG,
        "bbox":         f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]},{SRS_RPG}",
        "count":        WFS_COUNT,
    }
    try:
        r = requests.get(WFS_URL, params=params, timeout=120)
        r.raise_for_status()
        data = r.json()
        features = data.get("features", [])
        if not features or features[0].get("geometry") is None:
            return None
        gdf = gpd.GeoDataFrame.from_features(features)
        gdf.set_crs(SRS_RPG, inplace=True)
        return gdf
    except Exception as e:
        log.warning(f"RPG {year} indisponible : {e}")
        return None


# ---------------------------------------------------------------------------
# Analyse d'une année
# ---------------------------------------------------------------------------

def _analyze_year(year: int, gdf, parcelle, schema: str, nomenclature: dict) -> dict:
    """
    Intersecte les ilots RPG avec la parcelle et retourne le dict annuel.
    """
    if gdf is None or gdf.empty:
        return {"status": "non_agricole"}

    intersects = gdf[gdf.intersects(parcelle)].copy()
    if intersects.empty:
        return {"status": "non_declare"}

    intersects["geometry"] = intersects.geometry.intersection(parcelle)
    intersects["area"]     = intersects.geometry.area

    total_m2 = round(intersects["area"].sum(), 0)
    cultures: dict[str, float] = defaultdict(float)

    # Extraction du code culture selon le schéma de la couche
    col = None
    if "code_cultu" in intersects.columns:
        col = "code_cultu"
    elif "culture_d1" in intersects.columns:
        col = "culture_d1"

    if col:
        for _, row in intersects.iterrows():
            code = row.get(col)
            if code:
                cultures[code] += row["area"]

    if not cultures:
        return {"status": "agricole", "total_m2": total_m2, "cultures": {}}

    cultures_rounded = {code: round(area, 0) for code, area in cultures.items()}
    log.debug(f"RPG {year} : {total_m2} m² — {cultures_rounded}")
    return {"status": "agricole", "total_m2": total_m2, "cultures": cultures_rounded}


# ---------------------------------------------------------------------------
# Analyse de continuité / bail rural
# ---------------------------------------------------------------------------

def _analyser_continuite(by_year: dict) -> dict:
    """
    Calcule les métriques de continuité agricole sur l'ensemble des millésimes.

    Règles :
      - bail_rural_certain  : ≥5 années consécutives déclarées PAC
      - bail_rural_probable : 3-4 ans consécutifs OU >50% des années disponibles
      - continuite_forte    : alias bail_rural_certain
    """
    annees_agricoles = sorted([
        y for y, d in by_year.items()
        if d.get("status") == "agricole"
    ])
    nb_total       = len(by_year)
    nb_agricoles   = len(annees_agricoles)
    all_years      = sorted(by_year.keys())

    # Calcul des séquences consécutives
    max_consecutif = 0
    consecutif_courant = 0
    for i, y in enumerate(all_years):
        if by_year[y].get("status") == "agricole":
            consecutif_courant += 1
            max_consecutif = max(max_consecutif, consecutif_courant)
        else:
            consecutif_courant = 0

    bail_certain  = max_consecutif >= 5
    bail_probable = (
        not bail_certain and (
            max_consecutif >= 3
            or (nb_total > 0 and nb_agricoles / nb_total > 0.5)
        )
    )

    # Codes cultures uniques sur toutes les années
    codes = set()
    for d in by_year.values():
        codes.update(d.get("cultures", {}).keys())

    return {
        "occupation_agricole":  nb_agricoles > 0,
        "nb_annees_agricoles":  nb_agricoles,
        "annees_agricoles":     annees_agricoles,
        "max_consecutif":       max_consecutif,
        "continuite_forte":     bail_certain,
        "bail_rural_certain":   bail_certain,
        "bail_rural_probable":  bail_probable,
        "codes_cultures":       sorted(codes),
    }


# ---------------------------------------------------------------------------
# Point d'entrée principal
# ---------------------------------------------------------------------------

def fetch_rpg_parcelle(
    insee:   str,
    section: str,
    numero:  str,
    years:   Optional[list] = None,
) -> dict:
    """
    Analyse RPG complète pour une parcelle cadastrale.

    Args:
        insee, section, numero : décomposés depuis l'IDU via decompose_idu()
        years                  : liste d'années à analyser (défaut = toutes)

    Returns:
        {"summary": {...}, "by_year": {2020: {...}, ...}}
    """
    nomenclature = _load_nomenclature()

    # 1. Géométrie parcelle
    try:
        parcelle = _fetch_geom_parcelle(insee, section, numero)
    except Exception as e:
        log.error(f"RPG : géométrie introuvable pour {insee}/{section}/{numero} : {e}")
        return {
            "summary": {
                "occupation_agricole": False,
                "nb_annees_agricoles": 0,
                "annees_agricoles":    [],
                "continuite_forte":    False,
                "bail_rural_certain":  False,
                "bail_rural_probable": False,
                "codes_cultures":      [],
                "erreur":              str(e),
            },
            "by_year": {},
        }

    buffered = parcelle.buffer(BUFFER_M)
    bbox     = buffered.bounds  # (minx, miny, maxx, maxy)

    # 2. Itération sur les millésimes
    layers_to_fetch = {
        y: v for y, v in RPG_LAYERS.items()
        if years is None or y in years
    }

    by_year = {}
    for year, (typename, schema) in sorted(layers_to_fetch.items()):
        log.info(f"RPG {insee}/{section}/{numero} — millésime {year}")
        gdf              = _fetch_rpg_year(typename, bbox, year)
        by_year[year]    = _analyze_year(year, gdf, parcelle, schema, nomenclature)

    # 3. Analyse de continuité
    summary = _analyser_continuite(by_year)

    return {"summary": summary, "by_year": by_year}


# ---------------------------------------------------------------------------
# Scorer surcharge bail rural
# ---------------------------------------------------------------------------

def scorer_surcharge_bail_rural(
    rpg:    dict,
    ann:    dict,
    sirene: dict = None,
) -> tuple[int, str]:
    """
    Calcule la surcharge bail rural (+0 à +15 pts) selon le guide méthodologique.

    Règle de proportionnalité (multi-parcelles) :
      Cette fonction est appelée par parcelle — l'agrégation proportionnelle
      est faite dans scoring.py (surcharge_bail_max pondérée).

    Retourne (surcharge_pts, note_explicative).
    """
    summary = rpg.get("summary", {})

    bail_certain  = summary.get("bail_rural_certain",  False)
    bail_probable = summary.get("bail_rural_probable", False)
    nb_agricoles  = summary.get("nb_annees_agricoles", 0)
    max_consec    = summary.get("max_consecutif",      0)

    # Pas de signal agricole du tout
    if nb_agricoles == 0:
        return 0, "Aucune déclaration PAC détectée sur les millésimes disponibles"

    # Bail certain : ≥5 ans consécutifs
    if bail_certain:
        return 15, (
            f"Bail rural CERTAIN — {max_consec} années consécutives de déclaration PAC "
            f"({nb_agricoles} années agricoles au total) — surcharge maximale +15 pts"
        )

    # Bail probable : 3-4 ans consécutifs ou >50% années
    if bail_probable:
        return 8, (
            f"Bail rural PROBABLE — {max_consec} années consécutives / "
            f"{nb_agricoles} années agricoles au total — surcharge modérée +8 pts"
        )

    # Occupation agricole ponctuelle (< 3 ans consécutifs)
    if nb_agricoles >= 1:
        return 3, (
            f"Occupation agricole ponctuelle ({nb_agricoles} année(s) non consécutives) "
            f"— risque bail faible, surcharge +3 pts"
        )

    return 0, "Aucune surcharge bail rural applicable"


# ---------------------------------------------------------------------------
# CLI standalone
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    parser = argparse.ArgumentParser(description="Analyse RPG par IDU ou par insee/section/numero")
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--idu",    help="IDU (ex: 33213000BP0018)")
    grp.add_argument("--insee",  help="Code INSEE (avec --section et --numero)")
    parser.add_argument("--section", default=None)
    parser.add_argument("--numero",  default=None)
    parser.add_argument("--years",   nargs="+", type=int, default=None,
                        help="Années à analyser (ex: 2020 2021 2022)")
    parser.add_argument("--json",    action="store_true", help="Sortie JSON brute")
    args = parser.parse_args()

    if args.idu:
        insee, section, numero = decompose_idu(args.idu)
    else:
        if not args.section or not args.numero:
            print("--insee requiert aussi --section et --numero", file=sys.stderr)
            sys.exit(1)
        insee, section, numero = args.insee, args.section, args.numero

    print(f"\nAnalyse RPG : insee={insee} section={section} numero={numero}")
    if args.years:
        print(f"Années filtrées : {args.years}")

    result = fetch_rpg_parcelle(insee, section, numero, years=args.years)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        sys.exit(0)

    # Affichage lisible
    s = result["summary"]
    print(f"\n{'='*55}")
    print(f"  RÉSUMÉ RPG — {insee}/{section}/{numero}")
    print(f"{'='*55}")
    print(f"  Occupation agricole    : {'OUI' if s['occupation_agricole'] else 'NON'}")
    print(f"  Années agricoles       : {s['nb_annees_agricoles']}  {s['annees_agricoles']}")
    print(f"  Max consécutif         : {s.get('max_consecutif', '?')} ans")
    print(f"  Bail rural certain     : {'✅ OUI' if s['bail_rural_certain'] else '❌ non'}")
    print(f"  Bail rural probable    : {'⚠️  OUI' if s['bail_rural_probable'] else '❌ non'}")
    print(f"  Cultures détectées     : {', '.join(s['codes_cultures']) or 'aucune'}")

    print(f"\n  Détail par année :")
    nom = _load_nomenclature()
    for year, d in sorted(result["by_year"].items()):
        status = d.get("status", "?")
        if status == "agricole":
            total = d.get("total_m2", 0)
            cultures_str = ", ".join(
                f"{nom['code_cultu'].get(c, c)} ({round(a/total*100)}%)"
                for c, a in d.get("cultures", {}).items()
            ) or "culture inconnue"
            print(f"    {year} : AGRICOLE {total:.0f} m² — {cultures_str}")
        else:
            print(f"    {year} : {status.upper()}")

    # Surcharge
    surcharge, note = scorer_surcharge_bail_rural(result, {})
    print(f"\n  Surcharge bail         : +{surcharge} pts — {note}")
    print()