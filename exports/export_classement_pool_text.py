#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Textes d’export pour les métriques pool (parcelles), alignés sur le détail RankingLine.
Un bloc texte par profiler — utilisé par export_classement_csv / export_classement_shp.
"""

from __future__ import annotations

import json
from typing import Any

MIN_ZONAGE_RATIO = 0.01


def _as_dict(v: Any) -> dict[str, Any]:
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            o = json.loads(v)
            return o if isinstance(o, dict) else {}
        except Exception:
            return {}
    return {}


def metrics_rows_to_map(rows: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """Liste [{metric_key, metric_value_jsonb}, …] → { metric_key: dict payload }."""
    out: dict[str, dict[str, Any]] = {}
    if not rows:
        return out
    for r in rows:
        k = r.get("metric_key")
        if not k:
            continue
        out[str(k)] = _as_dict(r.get("metric_value_jsonb"))
    return out


def _fmt_float(x: Any, nd: int = 1) -> str:
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        if not (x == x):  # NaN
            return "?"
        return f"{float(x):.{nd}f}"
    return "?"


def format_scoring_details_v1(val: dict[str, Any]) -> str:
    """score_eco (0..6) — distance projet + proximité espèce (métrique especes_faune)."""
    v = _as_dict(val)
    ts = v.get("total_score")
    ms = v.get("max_score")
    if not isinstance(ts, (int, float)) or not isinstance(ms, (int, float)):
        return ""
    lines: list[str] = [f"Score : {int(ts)} / {int(ms)}"]
    b = v.get("breakdown")
    if not isinstance(b, dict):
        return "\n".join(lines)

    def item(key: str) -> dict[str, Any]:
        x = b.get(key)
        return _as_dict(x) if isinstance(x, dict) else {}

    especes = item("especes")
    dist = item("distance")

    def es_reason() -> str:
        r = especes.get("reason")
        if r == "intersection":
            return "Observation dans la parcelle"
        if r == "within_half_buffer":
            nd = especes.get("nearest_observation_distance_m")
            half = especes.get("buffer_half_m")
            return (
                f"Observation la plus proche ≤ demi-buffer ({_fmt_float(nd, 0)} m / demi-buffer {_fmt_float(half, 0)} m)"
            )
        if r == "within_buffer":
            nd = especes.get("nearest_observation_distance_m")
            bm = especes.get("buffer_radius_max_m")
            return f"Observation la plus proche dans le buffer ({_fmt_float(nd, 0)} m ≤ {_fmt_float(bm, 0)} m)"
        if r == "beyond_buffer":
            return "Observation au-delà du buffer du filtre"
        if r == "no_faune_criteria":
            return "Aucune espèce ciblée dans le filtre (0 pt espèce)"
        if r == "no_buffer_in_filter":
            return "Buffer non défini dans le filtre (0 pt espèce hors intersection)"
        if r == "no_observation":
            return "Pas d'observation géolocalisée pour les espèces du filtre"
        return "Hors critères"

    lines.append(
        f"Espèces faune — {es_reason()}\n+{int(especes.get('points') or 0)}"
    )
    lines.append(
        f"Distance au centre — {_fmt_float(dist.get('distance_km'), 1)} km "
        f"({dist.get('bucket') or 'n/a'})\n+{int(dist.get('points') or 0)}"
    )

    return "\n".join(lines)


def format_composite_details(val: dict[str, Any]) -> str:
    v = _as_dict(val)
    sc = v.get("score_composite")
    status = v.get("composite_status")
    msg = v.get("message")
    lines: list[str] = []
    if isinstance(sc, (int, float)):
        lines.append(f"Score composite: {_fmt_float(sc, 1)}/100")
    elif isinstance(msg, str) and msg.strip():
        lines.append(msg.strip())
    elif status == "sans_foncier":
        lines.append(
            "Score composite: non calculé (dureté foncière non applicable ou indisponible).",
        )
    elif status == "incomplet_eco":
        lines.append("Score composite: non calculé (score écologique manquant).")
    elif status:
        lines.append("Score composite: non calculé.")
    else:
        lines.append("Score composite: non calculé.")
    redhib = v.get("foncier_redhibitoire") is True
    thr = v.get("redhibitoire_threshold")
    thr_i = int(thr) if isinstance(thr, (int, float)) else 20
    if isinstance(sc, (int, float)) and redhib:
        lines.append(f"Dureté rédhibitoire (attractivité foncière < {thr_i}/100)")
    eco_n = v.get("eco_score_norm")
    eco_r = v.get("eco_score_raw")
    eco_m = v.get("eco_score_max")
    lines.append(
        f"Score éco normalisé: {_fmt_float(eco_n, 1)}/100 "
        f"({eco_r if isinstance(eco_r, (int, float)) else '?'}/"
        f"{eco_m if isinstance(eco_m, (int, float)) else '?'})"
    )
    af = v.get("attractivite_fonciere")
    df = v.get("durete_fonciere")
    lines.append(
        f"Attractivité foncière: {_fmt_float(af, 1)}/100 "
        f"(dureté {_fmt_float(df, 1)}/100)"
    )
    return "\n".join(lines)


def format_durete_details(val: dict[str, Any]) -> str:
    v = _as_dict(val)
    if v.get("eligible") is not True:
        return f"Non concernée (raison: {v.get('reason', 'not_pm')})"
    lines: list[str] = []
    sf = v.get("score_final")
    if isinstance(sf, (int, float)):
        lines.append(f"Score dureté: {int(sf)}/100 — {v.get('niveau_durete') or ''}".strip())
    if v.get("siren"):
        lines.append(f"SIREN {v.get('siren')}")
    if v.get("denomination"):
        lines.append(str(v.get("denomination")))
    if v.get("forme_juridique"):
        lines.append(f"Forme juridique: {v.get('forme_juridique')}")
    axes = v.get("detail_axes")
    if isinstance(axes, dict):
        pairs = [
            ("Axe 1", axes.get("axe1"), axes.get("axe1_note")),
            ("Axe 2", axes.get("axe2"), axes.get("axe2_note")),
            ("Axe 3", axes.get("axe3"), axes.get("axe3_note")),
            ("Axe 4", axes.get("axe4"), axes.get("axe4_note")),
            ("Surcharges", axes.get("surcharges"), axes.get("surcharges_note")),
        ]
        for label, score, note in pairs:
            if note or isinstance(score, (int, float)):
                sc = int(score) if isinstance(score, (int, float)) else "?"
                lines.append(f"{label}: {sc} — {note or 'non renseigné'}")
    if v.get("explication"):
        lines.append(str(v["explication"]))
    return "\n".join(lines)


def format_especes_details(val: dict[str, Any]) -> str:
    v = _as_dict(val)
    if not isinstance(v.get("intersects_any"), bool):
        return ""
    inter = v.get("intersects_any") is True
    buf = v.get("within_buffer_any") is True
    if inter:
        status = "Intersection directe avec les espèces ciblées (+3 pts sur le score éco /6 si applicable)."
    elif buf:
        status = "Pas d'intersection directe ; observation dans le buffer du filtre (voir score éco /6)."
    else:
        status = "Aucune observation des espèces sélectionnées dans le périmètre attendu."
    lines = [status]
    nd = v.get("nearest_observation_distance_m")
    if isinstance(nd, (int, float)):
        ns = v.get("nearest_species")
        lines.append(
            f"Observation la plus proche: {int(nd)} m"
            + (f" ({ns})" if ns else "")
        )
    by_sp = v.get("intersections_by_species")
    if isinstance(by_sp, dict) and by_sp:
        parts = [f"{lab}: {int(c)} obs." for lab, c in sorted(by_sp.items(), key=lambda x: -x[1]) if c]
        if parts:
            lines.append("Observations intersectées: " + " ; ".join(parts[:20]))
    return "\n".join(lines)


def _format_zonage_ratios_lines(val: dict[str, Any], label_total: str) -> str:
    v = _as_dict(val)
    ratios_raw = v.get("ratios")
    if not isinstance(ratios_raw, dict):
        return ""
    ratios: list[tuple[str, float]] = []
    for k, rv in ratios_raw.items():
        if isinstance(rv, (int, float)) and rv > 0:
            ratios.append((str(k), float(rv)))
    ratios.sort(key=lambda x: -x[1])
    shown = [(a, r) for a, r in ratios if r >= MIN_ZONAGE_RATIO]
    if not shown and ratios:
        return "Toutes les parts < 1 % de l’intersection (négligeables)."
    lines: list[str] = []
    tot = v.get("total_intersection_area_m2")
    if isinstance(tot, (int, float)) and tot > 0:
        lines.append(f"{label_total} {int(tot):,} m²".replace(",", " "))
    for lab, r in shown:
        pct = r * 100
        pct_s = f"{pct:.0f}" if pct >= 10 else f"{pct:.1f}"
        if isinstance(tot, (int, float)) and tot > 0:
            m2 = r * tot
            lines.append(f"{lab} — {pct_s} % ({int(m2):,} m²)".replace(",", " "))
        else:
            lines.append(f"{lab} — {pct_s} %")
    return "\n".join(lines)


def format_vegetation_hybride_details(val: dict[str, Any]) -> str:
    return _format_zonage_ratios_lines(val, "Intersection couche / parcelle:")


def format_cosia_details(val: dict[str, Any]) -> str:
    return _format_zonage_ratios_lines(val, "Intersection COSIA / parcelle:")


def format_carhab_details(val: dict[str, Any]) -> str:
    return _format_zonage_ratios_lines(val, "Surface parcelle (référence des % CARHAB):")


def format_arrachage_vignes_details(val: dict[str, Any]) -> str:
    return _format_zonage_ratios_lines(val, "Surface parcelle (référence des % arrachage):")


def format_personnes_morales_details(val: dict[str, Any]) -> str:
    v = _as_dict(val)
    parts: list[str] = []
    hit = v.get("intersects_pm_database") is True
    if not hit:
        parts.append("Non répertoriée en base personnes morales (parcelles_personnes_morales).")
    else:
        parts.append("Répertoriée en base personnes morales.")
        if v.get("siren"):
            parts.append(f"SIREN: {v.get('siren')}")
        if v.get("denomination"):
            parts.append(str(v.get("denomination")))
        if v.get("forme_juridique"):
            parts.append(f"Forme juridique: {v.get('forme_juridique')}")
    if v.get("compensation_deja_realisee") is True:
        parts.append(
            "Propriétaire ayant déjà réalisé de la compensation sur d'autres fonciers "
            "(parcelles_prospects_filtered)."
        )
        if v.get("parcelle_deja_en_mc") is True:
            parts.append("Cette parcelle est déjà concernée par une mesure de compensation.")
        elif v.get("parcelle_deja_en_mc") is False:
            parts.append("Cette parcelle n'est pas encore en mesure de compensation.")
        nb_mc = v.get("nb_mc_distinctes")
        if isinstance(nb_mc, int):
            parts.append(f"Mesures compensatoires distinctes (propriétaire): {nb_mc}")
        nb = v.get("nb_parcelles_deja_en_mc")
        if isinstance(nb, int):
            parts.append(f"Parcelles du propriétaire déjà en MC: {nb}")
        surf = v.get("surface_deja_en_mc_m2")
        if isinstance(surf, (int, float)):
            parts.append(
                f"Surface totale concernée par les MC (propriétaire): {float(surf):,.0f} m²".replace(",", " ")
            )
    elif not parts:
        parts.append("Hors liste prospects compensation.")
    return "\n".join(parts)


def format_zone_humide_details(val: dict[str, Any]) -> str:
    v = _as_dict(val)
    if not v:
        return ""
    return json.dumps(v, ensure_ascii=False, indent=2, default=str)


def extract_table_scalars(mmap: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Valeurs numériques alignées sur les badges RankingTable."""
    out: dict[str, Any] = {
        "score_eco": "",
        "score_eco_max": "",
        "score_composite": "",
        "durete": "",
    }
    ps = mmap.get("score_eco") or {}
    ts, mx = ps.get("total_score"), ps.get("max_score")
    if isinstance(ts, (int, float)):
        out["score_eco"] = float(ts)
    if isinstance(mx, (int, float)) and mx > 0:
        out["score_eco_max"] = int(mx)
    elif isinstance(ts, (int, float)):
        out["score_eco_max"] = 6

    cs = mmap.get("composite_score_v1") or {}
    csc = cs.get("score_composite")
    if isinstance(csc, (int, float)) and not isinstance(csc, bool):
        fc = float(csc)
        if fc == fc and 0.0 <= fc <= 100.0:
            out["score_composite"] = round(fc, 4)

    du = mmap.get("durete_fonciere") or {}
    if du.get("eligible") is True:
        sf = du.get("score_final")
        if isinstance(sf, (int, float)) and not isinstance(sf, bool) and 0.0 <= float(sf) <= 100.0:
            out["durete"] = int(round(float(sf)))
    return out


def build_detail_columns(mmap: dict[str, dict[str, Any]]) -> dict[str, str]:
    return {
        "scoring_details": format_scoring_details_v1(mmap.get("score_eco") or {}),
        "composite_details": format_composite_details(mmap.get("composite_score_v1") or {}),
        "durete_details": format_durete_details(mmap.get("durete_fonciere") or {}),
        "especes_details": format_especes_details(mmap.get("especes_faune") or {}),
        "vegetation_hybride_details": format_vegetation_hybride_details(
            mmap.get("vegetation_hybride_ratio") or {}
        ),
        "cosia_details": format_cosia_details(mmap.get("cosia_zonage_ratio") or {}),
        "carhab_details": format_carhab_details(mmap.get("carhab_eunis_ratio") or {}),
        "arrachage_vignes_details": format_arrachage_vignes_details(
            mmap.get("arrachage_vignes_ratio") or {}
        ),
        "personnes_morales_details": format_personnes_morales_details(
            mmap.get("parcelles_personnes_morales") or {}
        ),
        "zone_humide_details": format_zone_humide_details(mmap.get("zone_humide") or {}),
    }


def pool_metrics_json_compact(mmap: dict[str, dict[str, Any]]) -> str:
    """JSON minifié de toutes les métriques (CSV uniquement, exhaustif)."""
    try:
        return json.dumps(mmap, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        return "{}"


def shp_trunc(text: str, max_len: int = 254) -> str:
    if not text:
        return ""
    import unicodedata

    t = unicodedata.normalize("NFC", str(text).replace("\r\n", "\n").replace("\r", "\n"))
    if len(t) <= max_len:
        return t
    return t[: max_len - 20] + "\n…(tronqué, voir CSV)"
