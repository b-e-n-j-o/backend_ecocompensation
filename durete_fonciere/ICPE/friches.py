# -*- coding: utf-8 -*-
"""
friches.py — Module Cartofriches (via table ecocompensation.cartofriches en base)
Vérifie si un ou plusieurs IDUs sont répertoriés comme friches et retourne
les attributs utiles pour le scoring dureté foncière.

Jointure par :
  1. unite_fonciere_refcad (texte) — contient l'IDU dans le tableau postgres
  2. Fallback spatial — ST_Intersects sur la géométrie de la parcelle

Usage standalone :
    python friches.py --idu 321190000E0252
    python friches.py --idu 862750000D0319 --verbose
"""

import os
import argparse
import logging
from sqlalchemy import create_engine, text
from urllib.parse import quote_plus
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("friches")

# ============================================================
# CONFIG
# ============================================================
DB_HOST = os.getenv("SUPABASE_HOST")
DB_NAME = os.getenv("SUPABASE_DB", "postgres")
DB_USER = os.getenv("SUPABASE_USER", "postgres")
DB_PASS = os.getenv("SUPABASE_PASSWORD", "")
DB_PORT = os.getenv("SUPABASE_PORT", "5432")

TABLE = "ecocompensation.cartofriches"

# ── Normalisation sol_pollution_existe ──────────────────────
# Valeurs observées dans la données (casse incohérente)
POLLUTION_SCORES = {
    "pollution avérée":       3,   # surcharge forte
    "pollution probable":     2,   # surcharge modérée
    "pollution supposée":     1,   # signal d'alerte
    "pollution peu probable": 0,   # informatif seulement
    "pollution inexistante":  0,
    "pollution traitée":      0,   # traitée = neutre
    "oui":                    2,   # valeur ancienne non précisée
    "non":                    0,
    "inconnu":                0,
}

BATI_VACANCE_SCORES = {
    "vacant":                  1,
    "partiellement inoccupé":  1,
    "partiellement occupé":    0,
    "occupé":                  0,
    "sans objet":              0,
    "inconnu":                 0,
}

TAUX_ARTIF_SEUIL = 0.7  # signal faible potentiel écologique


def _get_engine():
    pwd = quote_plus(DB_PASS)
    url = f"postgresql+psycopg2://{DB_USER}:{pwd}@{DB_HOST}:{DB_PORT}/{DB_NAME}?sslmode=require"
    return create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 15})


# ============================================================
# REQUÊTE BASE
# ============================================================
def _query_by_refcad(engine, idu: str) -> list[dict]:
    """
    Recherche dans unite_fonciere_refcad via ANY sur le tableau postgres.
    Format stocké : {idu1,idu2,...} → on strip les accolades et on split par virgule.
    """
    sql = text(f"""
        SELECT
            site_id, site_nom, site_type, site_statut,
            sol_pollution_existe, sol_pollution_origine, sol_pollution_commentaire,
            bati_vacance,
            taux_artif_ff,
            proprio_type, proprio_nom,
            unite_fonciere_refcad,
            site_surface, comm_nom, comm_insee,
            urba_zone_type, urba_zone_lib,
            "P_renaturation",
            site_numero_basias, site_numero_basol,
            proprio_personne
        FROM {TABLE}
        WHERE :idu = ANY(
            string_to_array(
                replace(replace(unite_fonciere_refcad, '{{', ''), '}}', ''),
                ','
            )
        )
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {"idu": idu}).fetchall()
    return [dict(r._mapping) for r in rows]


def _query_spatial(engine, idu: str) -> list[dict]:
    """
    Fallback spatial : récupère la géométrie de la parcelle via WFS IGN
    puis intersecte avec la table cartofriches.
    Utilisé si la recherche textuelle ne trouve rien.
    """
    # Import local pour éviter dépendance circulaire
    import io, requests
    import geopandas as gpd

    try:
        from rpg import decompose_idu
        code_insee, section, numero = decompose_idu(idu)
        params = {
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeNames": "CADASTRALPARCELS.PARCELLAIRE_EXPRESS:parcelle",
            "srsName": "EPSG:2154", "outputFormat": "application/json",
            "CQL_FILTER": f"code_insee='{code_insee}' AND section='{section}' AND numero='{numero}'"
        }
        r = requests.get("https://data.geopf.fr/wfs/ows", params=params, timeout=30)
        r.raise_for_status()
        gdf = gpd.read_file(io.BytesIO(r.content)).to_crs("EPSG:2154")
        if gdf.empty:
            return []
        wkt = gdf.geometry.iloc[0].wkt
    except Exception as e:
        log.warning(f"Spatial fallback — récupération parcelle échouée : {e}")
        return []

    sql = text(f"""
        SELECT
            site_id, site_nom, site_type, site_statut,
            sol_pollution_existe, sol_pollution_origine, sol_pollution_commentaire,
            bati_vacance,
            taux_artif_ff,
            proprio_type, proprio_nom,
            unite_fonciere_refcad,
            site_surface, comm_nom, comm_insee,
            urba_zone_type, urba_zone_lib,
            "P_renaturation",
            site_numero_basias, site_numero_basol,
            proprio_personne
        FROM {TABLE}
        WHERE ST_Intersects(geometry, ST_Transform(ST_GeomFromText(:wkt, 2154), 4326))
    
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {"wkt": wkt}).fetchall()
    return [dict(r._mapping) for r in rows]


# ============================================================
# SCORING
# ============================================================
def scorer_friche(friche: dict) -> dict:
    """
    Retourne les signaux de scoring pour une friche donnée.

    Surcharges possibles sur le score de dureté :
      - domaine_public  : signal → score forcé 100 dans scoring.py
      - sol_pollution   : +0 à +3 pts
      - bati_vacance    : +0 à +1 pt
      - taux_artif      : signal faible potentiel écologique (pas de surcharge score)
      - basias/basol    : alerte documentaire (pas de surcharge automatique)
    """
    pol_raw  = (friche.get("sol_pollution_existe") or "inconnu").lower().strip()
    bati_raw = (friche.get("bati_vacance") or "inconnu").lower().strip()
    taux     = friche.get("taux_artif_ff")

    pol_score  = POLLUTION_SCORES.get(pol_raw, 0)
    bati_score = BATI_VACANCE_SCORES.get(bati_raw, 0)

    alertes = []

    # ── 1. Détection domaine public ──────────────────────────────────────
    # Codes proprio_type publics : P5a=commune, P5b=EPCI, P4=État, P3=région, P2=dept
    proprio_type_raw = friche.get("proprio_type") or ""
    CODES_PUBLICS = {"P1", "P2", "P3", "P4", "P5", "P5a", "P5b", "P5c", "P6"}
    # Extraire les codes du format postgres {P5a} ou {P5a,P5b}
    codes_proprio = set(
        c.strip("{} ")
        for c in proprio_type_raw.replace("{", "").replace("}", "").split(",")
        if c.strip("{} ")
    )
    est_domaine_public = bool(codes_proprio & CODES_PUBLICS)

    if est_domaine_public:
        alertes.append(
            f"Domaine public probable ({proprio_type_raw} — {friche.get('proprio_nom','?')}) "
            f"→ score forcé 100 recommandé"
        )

    # ── 2. Pollution ─────────────────────────────────────────────────────
    if pol_score >= 3:
        alertes.append("Pollution avérée — terrain probablement impropre à compensation écologique")
    elif pol_score == 2:
        alertes.append(f"Pollution probable ({friche.get('sol_pollution_origine','?')}) — vérification requise")
    elif pol_score == 1:
        alertes.append("Pollution supposée — signal d'alerte")
    elif pol_raw == "inconnu" and friche.get("site_type", "").lower() in (
        "friche industrielle", "friche d'équipement public", "friche minière"
    ):
        # Décharge, usine, mine → inconnu ne veut pas dire propre
        alertes.append(
            f"Pollution inconnue sur site à risque ({friche.get('site_type')}) "
            f"— croisement BASIAS/BASOL recommandé"
        )

    # ── 3. BASIAS / BASOL ────────────────────────────────────────────────
    num_basias = friche.get("site_numero_basias")
    num_basol  = friche.get("site_numero_basol")
    if num_basias and str(num_basias).strip() not in ("", "None", "null"):
        alertes.append(f"Référencé BASIAS n°{num_basias} — site industriel ou de service passé")
    if num_basol and str(num_basol).strip() not in ("", "None", "null"):
        alertes.append(f"Référencé BASOL n°{num_basol} — site pollué appelant action des pouvoirs publics")
    # ── 4. Bâti vacant ───────────────────────────────────────────────────
    if bati_score >= 1:
        alertes.append(f"Bâti vacant ({bati_raw}) — coût démolition potentiel")

    # ── 5. Taux artificialisation ─────────────────────────────────────────
    if taux is not None and taux >= TAUX_ARTIF_SEUIL:
        alertes.append(f"Taux d'artificialisation élevé ({taux:.0%}) — faible potentiel écologique")

    surcharge_pollution = pol_score   # 0 à 3
    surcharge_bati      = bati_score  # 0 ou 1

    return {
        "est_friche":            True,
        "site_id":               friche.get("site_id"),
        "site_nom":              friche.get("site_nom"),
        "site_type":             friche.get("site_type"),
        "site_statut":           friche.get("site_statut"),
        "est_domaine_public":    est_domaine_public,
        "proprio_type":          proprio_type_raw,
        "proprio_nom":           friche.get("proprio_nom"),
        "sol_pollution_existe":  friche.get("sol_pollution_existe"),
        "sol_pollution_score":   pol_score,
        "sol_pollution_origine": friche.get("sol_pollution_origine"),
        "site_numero_basias":    num_basias,
        "site_numero_basol":     num_basol,
        "bati_vacance":          friche.get("bati_vacance"),
        "bati_vacance_score":    bati_score,
        "taux_artif_ff":         taux,
        "faible_potentiel_eco":  taux is not None and taux >= TAUX_ARTIF_SEUIL,
        "surcharge_pollution":   surcharge_pollution,
        "surcharge_bati":        surcharge_bati,
        "surcharge_totale":      surcharge_pollution + surcharge_bati,
        "alertes":               alertes,
        "P_renaturation":        friche.get("P_renaturation"),
    }


# ============================================================
# POINT D'ENTRÉE PRINCIPAL
# ============================================================
def run_friches(idus: list[str] | str, verbose: bool = False) -> dict:
    """
    Vérifie si les IDUs sont répertoriés dans Cartofriches.
    Retourne un dict prêt à injecter dans le contexte JSON du scoring.
    """
    if isinstance(idus, str):
        idus = [idus]

    try:
        engine = _get_engine()
    except Exception as e:
        return {"erreur": f"Connexion base impossible : {e}", "est_friche": False}

    friches_trouvees = []
    for idu in idus:
        rows = _query_by_refcad(engine, idu)
        if not rows and verbose:
            log.info(f"  IDU {idu} : non trouvé par refcad, tentative spatiale...")
            rows = _query_spatial(engine, idu)

        for row in rows:
            scored = scorer_friche(row)
            scored["idu_recherche"] = idu

            # Appel BASOL automatique si numéro présent
            num_basol = row.get("site_numero_basol")
            if num_basol and str(num_basol).strip() not in ("", "None", "null"):
                from basol import run_basol
                if verbose:
                    log.info(f"  → BASOL n°{num_basol} détecté, lookup en cours...")
                basol_data = run_basol(num_basol, verbose=verbose)
                scored["basol"] = basol_data
                # Répercuter la surcharge BASOL
                if basol_data.get("trouve"):
                    scored["surcharge_totale"] += basol_data.get("surcharge_basol", 0)
                    scored["alertes"] += basol_data.get("alertes", [])
            else:
                scored["basol"] = None

            friches_trouvees.append(scored)
            if verbose:
                log.info(f"  IDU {idu} → friche trouvée : {row.get('site_nom')} ({row.get('site_id')})")

    if not friches_trouvees:
        return {
            "idus":          idus,
            "est_friche":    False,
            "nb_friches":    0,
            "friches":       [],
            "surcharge_max": 0,
            "alertes":       [],
            "note":          "Parcelle(s) non répertoriée(s) dans Cartofriches",
        }

    surcharge_max = max(f["surcharge_totale"] for f in friches_trouvees)
    alertes_all   = [a for f in friches_trouvees for a in f["alertes"]]

    return {
        "idus":          idus,
        "est_friche":    True,
        "nb_friches":    len(friches_trouvees),
        "friches":       friches_trouvees,
        "surcharge_max": surcharge_max,
        "alertes":       alertes_all,
        "note":          None,
    }


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Lookup Cartofriches par IDU")
    parser.add_argument("--idu", nargs="+", required=True)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    import json
    result = run_friches(args.idu, verbose=args.verbose)

    print(f"\n{'='*60}")
    print(f"  CARTOFRICHES — {len(args.idu)} IDU(s)")
    print(f"{'='*60}")
    print(f"  Répertorié en friche : {'✅ OUI' if result['est_friche'] else '✅ NON'}")
    print(f"  Nb friches trouvées  : {result['nb_friches']}")

    if result["est_friche"]:
        print(f"  Surcharge max score  : +{result['surcharge_max']} pts")
        for f in result["friches"]:
            print(f"\n  [{f['idu_recherche']}] → {f['site_nom']} ({f['site_id']})")
            print(f"    Type             : {f['site_type']}")
            print(f"    Statut           : {f['site_statut']}")
            print(f"    Domaine public   : {'⚠️  OUI → score forcé 100' if f['est_domaine_public'] else 'non'}")
            print(f"    Proprio          : {f['proprio_type']} — {f['proprio_nom']}")
            print(f"    Pollution        : {f['sol_pollution_existe']} (score={f['sol_pollution_score']})")
            print(f"    BASIAS           : {f['site_numero_basias'] or '—'}")
            print(f"    BASOL            : {f['site_numero_basol'] or '—'}")
            print(f"    Bâti vacance     : {f['bati_vacance']} (score={f['bati_vacance_score']})")
            print(f"    Taux artif.      : {f['taux_artif_ff']}")
            print(f"    Potentiel renat. : {f['P_renaturation']}%")
            print(f"    Faible pot. éco  : {'⚠️  OUI' if f['faible_potentiel_eco'] else 'non'}")
            for a in f["alertes"]:
                print(f"    ⚠️  {a}")

    if result.get("note"):
        print(f"\n  ℹ️  {result['note']}")

    print(f"\n  JSON brut :")
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print()