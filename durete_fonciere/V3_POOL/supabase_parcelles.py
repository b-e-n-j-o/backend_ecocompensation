"""
Module Supabase — Récupération parcelles par SIREN
===================================================
Interroge la table parcelles_personnes_morales en Supabase
et retourne la liste des IDUs + métadonnées agrégées.

Expose :
    fetch_parcelles_by_siren(siren) -> dict
"""

import os
import logging
from typing import Optional
from dotenv import load_dotenv
log = logging.getLogger("supabase_parcelles")
load_dotenv()

url = os.environ.get("SUPABASE_URL_PPM") or os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_KEY_PPM") or os.environ.get("SUPABASE_KEY")
def fetch_parcelles_by_siren(siren: str) -> dict:
    """
    Groupby SIREN sur parcelles_personnes_morales.
    Retourne :
        {
            siren, denomination, nb_parcelles,
            surface_totale_m2, surface_totale_ha,
            idus: [...],
            surfaces: [...],
            communes: [...],   # dédoublonnées depuis code_insee
            departements: [...],
        }
    """
    try:
        from supabase import create_client
    except ImportError:
        log.error("pip install supabase requis")
        return {}

    try:
        client = create_client(url, key)

        # Récupération de toutes les parcelles du SIREN
        response = (
            client.table("parcelles_personnes_morales")
            .select("idu, siren, denomination, contenance, code_insee, section, numero")
            .eq("siren", siren)
            .execute()
        )

        rows = response.data
        if not rows:
            log.warning(f"Aucune parcelle trouvée pour SIREN {siren}")
            return {}

        # Agrégation locale
        idus       = [r["idu"] for r in rows if r.get("idu")]
        surfaces   = [r.get("contenance") or 0 for r in rows]
        code_insee = list({r["code_insee"] for r in rows if r.get("code_insee")})
        depts      = list({ci[:2] for ci in code_insee})
        denom      = rows[0].get("denomination", "")
        surface_totale = sum(surfaces)

        result = {
            "siren":             siren,
            "denomination":      denom,
            "nb_parcelles":      len(idus),
            "surface_totale_m2": surface_totale,
            "surface_totale_ha": round(surface_totale / 10000, 2),
            "idus":              idus,
            "surfaces":          surfaces,
            "communes_insee":    sorted(code_insee),
            "departements":      sorted(depts),
            "parcelles_detail":  [
                {
                    "idu":        r.get("idu"),
                    "code_insee": r.get("code_insee"),
                    "section":    r.get("section"),
                    "numero":     r.get("numero"),
                    "surface_m2": r.get("contenance") or 0,
                }
                for r in rows
            ],
        }

        log.info(
            f"Supabase OK : {denom} | {len(idus)} parcelles | "
            f"{surface_totale/10000:.1f} ha | depts {depts}"
        )
        return result

    except Exception as e:
        log.error(f"Erreur Supabase : {e}")
        return {}


def fetch_parcelles_from_json(data: list) -> dict:
    """
    Alternative sans Supabase : parse le JSON brut qu'on a déjà
    (résultat du groupby manuel).
    Utile pour tests ou si Supabase non accessible.
    """
    if not data:
        return {}
    row = data[0]
    surface_totale = sum(row.get("surfaces", []))
    idus = row.get("idus", [])
    depts = list({idu[:2] for idu in idus})

    return {
        "siren":             row.get("siren"),
        "denomination":      row.get("denomination"),
        "nb_parcelles":      row.get("nb_parcelles", len(idus)),
        "surface_totale_m2": surface_totale,
        "surface_totale_ha": round(surface_totale / 10000, 2),
        "idus":              idus,
        "surfaces":          row.get("surfaces", []),
        "communes_insee":    [],
        "departements":      sorted(depts),
    }