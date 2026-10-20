"""
Attributs d'export classement (parcelles) — contrat unique SHP / CSV / rapport.

Colonnes alignées sur RankingTable + RankingLine (scores, dureté, PM, enrichissement).
Noms ≤ 10 caractères (limite DBF shapefile).
"""

from __future__ import annotations

import json
import math
from typing import Any

from api.recherche_de_foncier.enrichir.pool.exports.export_classement_pool_text import (
    build_detail_columns,
    extract_table_scalars,
    metrics_rows_to_map,
    shp_trunc,
)

_MAX_TXT_FULL = 31_000  # limite pratique hors troncature SHP (254 c.)

# Ordre stable SHP / GPKG / CSV (CSV ajoute pool_metrics_json à la fin).
EXPORT_ATTR_KEYS: tuple[str, ...] = (
    "rang",
    "idu",
    "cinsee",
    "code_insee",
    "section",
    "numero",
    "surf_ha",
    "miller",
    "dist_km",
    "dist_hyd",
    "zh_ha",
    "zh_int",
    "zh_pct",
    "zh_m2",
    "surf_hyha",
    "dist_shy",
    "score_eco",
    "eco_max",
    "eco_esp",
    "eco_dist",
    "score_comp",
    "cmp_attr",
    "cmp_redh",
    "score_dur",
    "attr_fonc",
    "dur_niv",
    "dur_elig",
    "dur_axe1",
    "dur_axe2",
    "dur_axe3",
    "dur_axe4",
    "dur_sur",
    "arr_vign",
    "rayon_esp",
    "espece_esp",
    "faune_txt",
    "cesbio",
    "occ_sol",
    "zh",
    "rem_nappe",
    "ebc",
    "znieff",
    "zdv",
    "troncon",
    "surf_hyd",
    "p_morale",
    "siren",
    "pm_denom",
    "pm_forme",
    "pm_prosp",
    "pm_mc",
    "pm_nb_mc",
    "pm_nb_p",
    "pm_s_mc",
    "txt_scor",
    "txt_comp",
    "txt_dure",
    "txt_espe",
    "txt_enri",
    "txt_pm",
    "txt_vege",
    "txt_cosi",
    "txt_carb",
    "txt_arra",
    "txt_zhum",
)


def num_eco(v: Any) -> int | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)):
        return int(round(float(v)))
    return None


def num_eco_max(v: Any) -> int | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)) and float(v) > 0:
        return int(round(float(v)))
    return None


def num_composite(v: Any) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)):
        return round(float(v), 1)
    return None


def num_durete(v: Any) -> int | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)):
        x = float(v)
        if 0.0 <= x <= 100.0:
            return int(round(x))
    return None


def _opt_int(v: Any, *, lo: float | None = None, hi: float | None = None) -> int | None:
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(float(v)):
        return None
    x = float(v)
    if lo is not None and x < lo:
        return None
    if hi is not None and x > hi:
        return None
    return int(round(x))


def _opt_float(v: Any, nd: int = 2) -> float | None:
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(float(v)):
        return None
    return round(float(v), nd)


def _opt_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
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


def _fauna_from_enrich(enrich: dict[str, Any]) -> tuple[str, int | None, str]:
    """Espèce la plus proche, distance (m), liste compacte — depuis filter_enrich."""
    fd = enrich.get("fauna_distances")
    if not isinstance(fd, dict) or not fd:
        return "", None, ""
    ranked: list[tuple[str, float]] = []
    for lab, dist in fd.items():
        name = str(lab or "").strip()
        if not name or not isinstance(dist, (int, float)) or isinstance(dist, bool):
            continue
        d = float(dist)
        if not math.isfinite(d) or d < 0:
            continue
        ranked.append((name, d))
    if not ranked:
        return "", None, ""
    ranked.sort(key=lambda x: (x[1], x[0].lower()))
    parts = [
        f"{name}: intersection" if d <= 0 else f"{name}: {int(round(d))} m"
        for name, d in ranked
    ]
    best_name, best_d = ranked[0]
    return best_name[:254], int(round(best_d)), " ; ".join(parts)


def _cesbio_labels(enrich: dict[str, Any]) -> str:
    veg = enrich.get("veg_libelles")
    if not isinstance(veg, list):
        return ""
    labels = [str(x).strip() for x in veg if str(x).strip()]
    return ", ".join(labels)


def ratios_from_cesbio_profile(profile: dict[str, Any]) -> dict[str, float]:
    """
    Convertit la métrique `cesbio_profile` ({libelle: {area_m2, pct}})
    en parts 0–1 (même contrat que vegetation_hybride_ratio.ratios / occ_sol).
    """
    if not isinstance(profile, dict):
        return {}
    out: dict[str, float] = {}
    for label, item in profile.items():
        name = str(label or "").strip()
        if not name:
            continue
        pct: float | None = None
        if isinstance(item, dict):
            raw = item.get("pct")
            if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(float(raw)):
                pct = float(raw)
        elif isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(float(item)):
            pct = float(item)
        if pct is None or pct <= 0:
            continue
        # pct CESBIO = % parcelle (0–100) ; déjà 0–1 si valeur ≤ 1.
        ratio = pct / 100.0 if pct > 1.0 else pct
        if ratio > 0:
            out[name] = round(ratio, 6)
    return out


def occ_sol_ratios(mmap: dict[str, Any]) -> dict[str, float]:
    """Parts d'occupation du sol 0–1 : hybride ZH en priorité, sinon CESBIO faune."""
    veg_h = mmap.get("vegetation_hybride_ratio") or {}
    hyb = veg_h.get("ratios") if isinstance(veg_h, dict) else None
    if isinstance(hyb, dict) and hyb:
        out: dict[str, float] = {}
        for label, raw in hyb.items():
            name = str(label or "").strip()
            if not name or not isinstance(raw, (int, float)) or isinstance(raw, bool):
                continue
            fv = float(raw)
            if math.isfinite(fv) and fv > 0:
                out[name] = round(fv, 6)
        if out:
            return out
    profile = mmap.get("cesbio_profile") or {}
    if isinstance(profile, dict):
        return ratios_from_cesbio_profile(profile)
    return {}


def _clip_text(text: str, *, clip: bool) -> str:
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if clip:
        return shp_trunc(raw)
    if len(raw) <= _MAX_TXT_FULL:
        return raw
    return raw[: _MAX_TXT_FULL - 30] + "\n…(tronqué)"


def _axis_score(axes: dict[str, Any] | None, key: str) -> int | None:
    if not isinstance(axes, dict):
        return None
    return num_durete(axes.get(key))


def build_parcelle_export_row(
    parcelle: dict[str, Any],
    mmap: dict[str, Any],
    options: Any,
    *,
    clip_for_shapefile: bool = True,
) -> dict[str, Any]:
    """
    Une ligne d'attributs (sans géométrie), même schéma que le shapefile de sortie.

    clip_for_shapefile : si True, textes tronqués via ``shp_trunc`` (254 c.,
    limite DBF). Si False, textes conservés (plafonnés à ~31k c.) — GPKG / CSV / PDF.
    """
    scalars = extract_table_scalars(mmap)
    details = build_detail_columns(mmap)
    especes = mmap.get("especes_faune") or {}
    pm = mmap.get("parcelles_personnes_morales") or {}
    enrich = mmap.get("filter_enrich") or {}
    durete = mmap.get("durete_fonciere") or {}
    composite = mmap.get("composite_score_v1") or {}
    score_eco = mmap.get("score_eco") or {}

    idu = parcelle.get("idu")
    dist_km = parcelle.get("distance_km")
    if dist_km is None:
        dist_km = parcelle.get("distance_k", 0)

    dist_hyd = parcelle.get("dist_hydro_m")
    if dist_hyd is None:
        dist_hyd = enrich.get("dist_hydro_m")

    # Preferer l'enrich (critere actif). La colonne SQL zone_humide_ha DEFAULT 0
    # ne doit pas etre lue comme un calcul si le mode etait « ignorer ».
    zh_ha = enrich.get("zone_humide_ha")
    if zh_ha is None:
        pzh = parcelle.get("zone_humide_ha")
        try:
            if pzh is not None and float(pzh or 0) > 0:
                zh_ha = pzh
        except (TypeError, ValueError):
            zh_ha = None
    surf_hyha = parcelle.get("surface_hydro_ha")
    if surf_hyha is None:
        surf_hyha = enrich.get("surface_hydro_ha")
    dist_shy = parcelle.get("dist_surface_hydro_m")
    if dist_shy is None:
        dist_shy = enrich.get("dist_surface_hydro_m")

    espece_enri, dist_enri, faune_txt = _fauna_from_enrich(enrich)
    if especes.get("intersects_any") is True:
        rayon_esp = 0
    elif dist_enri is not None:
        rayon_esp = dist_enri
    else:
        nd = especes.get("nearest_observation_distance_m")
        rayon_esp = int(round(float(nd), 0)) if isinstance(nd, (int, float)) else -1
    espece_esp = espece_enri or espece_cible_especes_faune(especes)

    cesbio = _cesbio_labels(enrich)
    occ_ratios = occ_sol_ratios(mmap)
    if occ_ratios:
        occ_sol = json.dumps(occ_ratios, ensure_ascii=False, separators=(",", ":"))
    else:
        occ_sol = cesbio

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

    axes = durete.get("detail_axes") if isinstance(durete.get("detail_axes"), dict) else None
    durete_ok = durete.get("eligible") is True
    score_dur = num_durete(scalars.get("durete"))
    attr_fonc = _opt_float(durete.get("attractivite_fonciere"), 1)
    if attr_fonc is None and score_dur is not None:
        attr_fonc = round(100.0 - float(score_dur), 1)
    cmp_attr = _opt_float(composite.get("attractivite_fonciere"), 1)
    if cmp_attr is None:
        cmp_attr = attr_fonc

    breakdown = score_eco.get("breakdown") if isinstance(score_eco.get("breakdown"), dict) else {}
    especes_bd = breakdown.get("especes") if isinstance(breakdown.get("especes"), dict) else {}
    dist_bd = breakdown.get("distance") if isinstance(breakdown.get("distance"), dict) else {}

    txt_espe = details["especes_details"] or details["filter_enrich_details"]
    txt_zhum = details["zone_humide_details"]
    if not txt_zhum and zh_ha is not None:
        txt_zhum = details["filter_enrich_details"]

    clip = clip_for_shapefile
    zh_ha_n = _opt_float(zh_ha, 2)
    surf_ha_n = round(float(parcelle.get("surface_ha") or 0), 2)
    zh_int: bool | None = _opt_bool(enrich.get("zone_humide_intersects"))
    zh_pct_n = _opt_float(enrich.get("zone_humide_pct"), 2)
    zh_m2_n = _opt_float(enrich.get("zone_humide_m2"), 1)
    if zh_ha_n is not None:
        if zh_int is None:
            zh_int = zh_ha_n > 0
        if zh_m2_n is None:
            zh_m2_n = round(float(zh_ha_n) * 10_000.0, 1)
        if zh_pct_n is None and surf_ha_n > 0:
            zh_pct_n = round(min(100.0, float(zh_ha_n) / surf_ha_n * 100.0), 2)
    if zh_int is True and zh_pct_n is not None and zh_m2_n is not None:
        zh_txt = f"Oui · {zh_pct_n:.1f} % · {zh_m2_n:,.0f} m²".replace(",", " ")
    elif zh_int is False:
        zh_txt = "Non"
    elif zh_ha_n is not None:
        zh_txt = f"{zh_ha_n:.2f} ha"
    else:
        zh_txt = ""

    return {
        "rang": int(parcelle.get("rank", 0) or 0),
        "idu": str(idu or "")[:254],
        "cinsee": str(parcelle.get("code_insee") or "")[:10],
        "code_insee": str(parcelle.get("code_insee") or "")[:10],
        "section": str(parcelle.get("section") or "")[:10],
        "numero": str(parcelle.get("numero") or "")[:10],
        "surf_ha": surf_ha_n,
        "miller": round(float(parcelle.get("miller") or 0), 4),
        "dist_km": round(float(dist_km or 0), 2),
        "dist_hyd": round(float(dist_hyd), 0) if dist_hyd is not None else -1,
        "zh_ha": zh_ha_n,
        "zh_int": zh_int,
        "zh_pct": zh_pct_n,
        "zh_m2": zh_m2_n,
        "surf_hyha": _opt_float(surf_hyha, 2),
        "dist_shy": _opt_int(dist_shy, lo=0),
        "score_eco": num_eco(scalars.get("score_eco")),
        "eco_max": num_eco_max(scalars.get("score_eco_max")),
        "eco_esp": _opt_int(especes_bd.get("points")),
        "eco_dist": _opt_int(dist_bd.get("points")),
        "score_comp": num_composite(scalars.get("score_composite")),
        "cmp_attr": cmp_attr,
        "cmp_redh": composite.get("foncier_redhibitoire") is True,
        "score_dur": score_dur,
        "attr_fonc": attr_fonc,
        "dur_niv": _clip_text(str(durete.get("niveau_durete") or ""), clip=clip) if durete_ok else "",
        "dur_elig": durete_ok,
        "dur_axe1": _axis_score(axes, "axe1"),
        "dur_axe2": _axis_score(axes, "axe2"),
        "dur_axe3": _axis_score(axes, "axe3"),
        "dur_axe4": _axis_score(axes, "axe4"),
        "dur_sur": _axis_score(axes, "surcharges"),
        "arr_vign": durete.get("intersects_arrachage_vigne") is True,
        "rayon_esp": rayon_esp,
        "espece_esp": espece_esp,
        "faune_txt": _clip_text(faune_txt, clip=clip),
        "cesbio": _clip_text(cesbio, clip=clip),
        "occ_sol": _clip_text(occ_sol, clip=clip),
        "zh": zh_txt,
        "rem_nappe": str(remontee_str)[:254],
        "ebc": ebc_mode[:254],
        "znieff": znieff_mode[:254],
        "zdv": (zdv_str or "—")[:254],
        "troncon": str(troncon_str)[:254],
        "surf_hyd": str(surf_hyd_str)[:254],
        "p_morale": pm_hit,
        "siren": pm_siren[:20],
        "pm_denom": _clip_text(pm_denom, clip=clip),
        "pm_forme": _clip_text(pm_forme, clip=clip),
        "pm_prosp": pm.get("compensation_deja_realisee") is True,
        "pm_mc": _opt_bool(pm.get("parcelle_deja_en_mc")),
        "pm_nb_mc": _opt_int(pm.get("nb_mc_distinctes"), lo=0),
        "pm_nb_p": _opt_int(pm.get("nb_parcelles_deja_en_mc"), lo=0),
        "pm_s_mc": _opt_float(pm.get("surface_deja_en_mc_m2"), 0),
        "txt_scor": _clip_text(details["scoring_details"], clip=clip),
        "txt_comp": _clip_text(details["composite_details"], clip=clip),
        "txt_dure": _clip_text(details["durete_details"], clip=clip),
        "txt_espe": _clip_text(txt_espe, clip=clip),
        "txt_enri": _clip_text(details["filter_enrich_details"], clip=clip),
        "txt_pm": _clip_text(details["personnes_morales_details"], clip=clip),
        "txt_vege": _clip_text(details["vegetation_hybride_details"], clip=clip),
        "txt_cosi": _clip_text(details["cosia_details"], clip=clip),
        "txt_carb": _clip_text(details["carhab_details"], clip=clip),
        "txt_arra": _clip_text(details["arrachage_vignes_details"], clip=clip),
        "txt_zhum": _clip_text(txt_zhum, clip=clip),
    }


def mmap_for_parcelle(
    parcelle: dict[str, Any],
    metrics_by_idu: dict[str, list[dict[str, Any]]] | None,
) -> dict[str, Any]:
    idu = str(parcelle.get("idu") or "")
    by_idu = metrics_by_idu or {}
    mmap = metrics_rows_to_map(by_idu.get(idu) or [])
    # Fallback si le run n'a pas (encore) de métrique filter_enrich : champs déjà sur la parcelle.
    if not mmap.get("filter_enrich"):
        enrich: dict[str, Any] = {}
        if parcelle.get("veg_libelles"):
            enrich["veg_libelles"] = list(parcelle.get("veg_libelles") or [])
        if parcelle.get("fauna_distances"):
            enrich["fauna_distances"] = dict(parcelle.get("fauna_distances") or {})
        if parcelle.get("zone_humide_ha") is not None:
            try:
                zh_ha = float(parcelle.get("zone_humide_ha") or 0)
            except (TypeError, ValueError):
                zh_ha = 0.0
            if zh_ha > 0:
                enrich["zone_humide_ha"] = zh_ha
                enrich["zone_humide_m2"] = round(zh_ha * 10_000.0, 1)
                enrich["zone_humide_intersects"] = True
                surf = float(parcelle.get("surface_ha") or 0)
                if surf > 0:
                    enrich["zone_humide_pct"] = round(min(100.0, zh_ha / surf * 100.0), 2)
        if parcelle.get("dist_hydro_m") is not None:
            enrich["dist_hydro_m"] = parcelle.get("dist_hydro_m")
        if parcelle.get("troncons_hydro_info"):
            enrich["troncons_hydro_info"] = list(parcelle.get("troncons_hydro_info") or [])
        if parcelle.get("dist_surface_hydro_m") is not None:
            enrich["dist_surface_hydro_m"] = parcelle.get("dist_surface_hydro_m")
        if parcelle.get("surface_hydro_ha") is not None:
            enrich["surface_hydro_ha"] = parcelle.get("surface_hydro_ha")
        if parcelle.get("surfaces_hydro_info"):
            enrich["surfaces_hydro_info"] = list(parcelle.get("surfaces_hydro_info") or [])
        if enrich:
            mmap["filter_enrich"] = enrich
    return mmap
