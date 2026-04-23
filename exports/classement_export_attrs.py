"""
Attributs d'export classement (parcelles) — contrat unique SHP / CSV / rapport.

Colonnes alignées sur export_classement_shp (référence).
"""

from __future__ import annotations

import json
import math
from typing import Any

from exports.export_classement_pool_text import (
    build_detail_columns,
    extract_table_scalars,
    metrics_rows_to_map,
    shp_trunc,
)


def num_eco(v: Any) -> int | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)):
        return int(round(float(v)))
    return None


def num_eco_max(v: Any) -> int | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)) and float(v) > 0:
        return int(round(float(v)))
    return None


def num_composite(v: Any) -> float:
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)):
        return round(float(v), 1)
    return float("nan")


def num_durete(v: Any) -> int | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)):
        x = float(v)
        if 0.0 <= x <= 100.0:
            return int(round(x))
    return None


def espece_cible_especes_faune(especes_payload: dict[str, Any]) -> str:
    """
    Espèce à afficher pour le classement/export :
    - si intersection, espèce la plus représentée dans `intersections_by_species`;
    - sinon, espèce la plus proche (`nearest_species`) si disponible.
    """
    if not isinstance(especes_payload, dict):
        return ""
    by_species = especes_payload.get("intersections_by_species")
    if especes_payload.get("intersects_any") is True and isinstance(by_species, dict):
        ranked: list[tuple[str, int]] = []
        for label, cnt in by_species.items():
            if label is None:
                continue
            lab = str(label).strip()
            if not lab:
                continue
            try:
                n = int(cnt)
            except Exception:
                continue
            if n > 0:
                ranked.append((lab, n))
        if ranked:
            ranked.sort(key=lambda x: (-x[1], x[0].lower()))
            return ranked[0][0][:254]
    nearest = especes_payload.get("nearest_species")
    if nearest is None:
        return ""
    return str(nearest).strip()[:254]


_MAX_TXT_DURE_FULL = 31_000  # limite pratique hors troncature SHP (254 c.)


def build_parcelle_export_row(
    parcelle: dict[str, Any],
    mmap: dict[str, Any],
    options: Any,
    *,
    clip_for_shapefile: bool = True,
) -> dict[str, Any]:
    """
    Une ligne d'attributs (sans géométrie), même schéma que le shapefile de sortie.

    clip_for_shapefile : si True, ``txt_dure`` est tronqué via ``shp_trunc`` (254 c.,
    limite DBF du Shapefile). Si False, ``txt_dure`` est conservé en entier
    (plafonné à ~31k c.) — pour CSV, GeoPackage et rapport PDF. Les autres champs
    longs restent tronqués comme pour le SHP.
    """
    scalars = extract_table_scalars(mmap)
    details = build_detail_columns(mmap)
    especes = mmap.get("especes_faune") or {}
    pm = mmap.get("parcelles_personnes_morales") or {}
    veg_h = mmap.get("vegetation_hybride_ratio") or {}

    idu = parcelle.get("idu")
    dist_km = parcelle.get("distance_km")
    if dist_km is None:
        dist_km = parcelle.get("distance_k", 0)
    dist_hyd = parcelle.get("dist_hydro_m")

    if especes.get("intersects_any") is True:
        rayon_esp = 0
    else:
        nd = especes.get("nearest_observation_distance_m")
        rayon_esp = int(round(float(nd), 0)) if isinstance(nd, (int, float)) else -1
    espece_esp = espece_cible_especes_faune(especes)

    hyb_ratios = veg_h.get("ratios") if isinstance(veg_h.get("ratios"), dict) else {}
    hyb_json = shp_trunc(json.dumps(hyb_ratios, ensure_ascii=False, separators=(",", ":")))

    pm_hit = pm.get("intersects_pm_database") is True
    pm_siren = str(pm.get("siren") or "").strip() if pm_hit else ""
    pm_denom = str(pm.get("denomination") or "").strip() if pm_hit else ""
    pm_forme = str(pm.get("forme_juridique") or "").strip() if pm_hit else ""

    zdv_vals = getattr(options, "zdv_natures", None) or []
    remontee_vals = getattr(options, "remontee_nappes_classefiab", None) or []
    zdv_str = ", ".join(zdv_vals) if zdv_vals else "—"
    remontee_str = ", ".join(str(v) for v in remontee_vals if str(v).strip()) if remontee_vals else "—"
    ebc_mode = str(getattr(options, "ebc_mode", "—") or "—")
    znieff_mode = str(getattr(options, "znieff_mode", "—") or "—")
    troncon_str = (
        "Intersection"
        if options.troncon_hydro_mode == "intersect"
        else f"<{options.troncon_hydro_radius_m:.0f}m"
        if options.troncon_hydro_mode == "within_radius"
        else "—"
    )
    surf_hyd_str = (
        "Intersection"
        if options.surface_hydro_mode == "intersect"
        else f"<{options.surface_hydro_radius_m:.0f}m"
        if options.surface_hydro_mode == "within_radius"
        else "—"
    )

    dure_raw = (details.get("durete_details") or "").replace("\r\n", "\n").replace("\r", "\n")
    if clip_for_shapefile:
        txt_dure_out = shp_trunc(dure_raw)
    else:
        txt_dure_out = (
            dure_raw
            if len(dure_raw) <= _MAX_TXT_DURE_FULL
            else dure_raw[: _MAX_TXT_DURE_FULL - 30] + "\n…(tronqué)"
        )

    return {
        "rang": int(parcelle.get("rank", 0) or 0),
        "idu": str(idu or "")[:254],
        "cinsee": str(parcelle.get("code_insee") or "")[:10],
        "code_insee": str(parcelle.get("code_insee") or "")[:10],
        "section": str(parcelle.get("section") or "")[:10],
        "numero": str(parcelle.get("numero") or "")[:10],
        "surf_ha": round(float(parcelle.get("surface_ha") or 0), 2),
        "miller": round(float(parcelle.get("miller") or 0), 4),
        "dist_km": round(float(dist_km or 0), 2),
        "dist_hyd": round(float(dist_hyd), 0) if dist_hyd is not None else -1,
        "score_eco": num_eco(scalars.get("score_eco")),
        "eco_max": num_eco_max(scalars.get("score_eco_max")),
        "score_comp": num_composite(scalars.get("score_composite")),
        "score_dur": num_durete(scalars.get("durete")),
        "rayon_esp": rayon_esp,
        "espece_esp": espece_esp,
        "occ_sol": hyb_json,
        "zh": shp_trunc(details["zone_humide_details"]),
        "rem_nappe": str(remontee_str)[:254],
        "ebc": ebc_mode[:254],
        "znieff": znieff_mode[:254],
        "zdv": (zdv_str or "—")[:254],
        "troncon": str(troncon_str)[:254],
        "surf_hyd": str(surf_hyd_str)[:254],
        "txt_scor": shp_trunc(details["scoring_details"]),
        "txt_comp": shp_trunc(details["composite_details"]),
        "txt_dure": txt_dure_out,
        "txt_espe": shp_trunc(details["especes_details"]),
        "txt_vege": shp_trunc(details["vegetation_hybride_details"]),
        "txt_cosi": shp_trunc(details["cosia_details"]),
        "txt_carb": shp_trunc(details["carhab_details"]),
        "txt_arra": shp_trunc(details["arrachage_vignes_details"]),
        "p_morale": pm_hit,
        "siren": pm_siren[:20],
        "pm_denom": shp_trunc(pm_denom),
        "pm_forme": shp_trunc(pm_forme),
        "txt_zhum": shp_trunc(details["zone_humide_details"]),
    }


def mmap_for_parcelle(
    parcelle: dict[str, Any],
    metrics_by_idu: dict[str, list[dict[str, Any]]] | None,
) -> dict[str, Any]:
    idu = str(parcelle.get("idu") or "")
    by_idu = metrics_by_idu or {}
    return metrics_rows_to_map(by_idu.get(idu) or [])
