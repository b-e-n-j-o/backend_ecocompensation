"""
builder.py — Assemble le contexte du rapport de filtrage à partir des données
d'une étude (snapshot filter_config + entonnoir + pool retenu).

Entrée (dict / JSON), toutes les clés sauf filter_config, funnel, total sont optionnelles :
{
  "etude":  {"nom", "reference", "projet", "commune", "departement", "auteur", "organisme"},
  "computed_at": "2026-11-04T10:12:00+01:00",
  "buffer_km": 5,
  "filter_config": {...},                 # snapshot du calcul (last_results.filter_config)
  "funnel": [{"key"?, "label", "count"}], # ordre d'exécution SQL
  "total": 36,
  "aoi_total": 1243,                      # parcelles dans la zone avant tout filtre
  "data_versions": {"geomce": "export du 01/10/2026", ...},
  "parcelles": [{"idu", "commune", "surface_ha", "veg_libelles", "fauna_distances", ...}],
  "geo": {"crs": "EPSG:2154", "foncier": <GeoJSON geom>, "aoi": <geom>,
          "parcelles": <FeatureCollection, properties.idu>}
}

Usage CLI :
    python builder.py --input examples/example_complet.json [-v]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
from datetime import datetime
from typing import Any

from catalog import THEME_LABELS, THEME_ORDER, Criterion, build_criteria, fmt_dist, fmt_num, match_step_key

log = logging.getLogger("filter_report.builder")


def fmt_int(n: int | None) -> str:
    return "—" if n is None else f"{n:,}".replace(",", " ")


def fmt_pct(p: float | None) -> str:
    return "—" if p is None else f"{fmt_num(p, 1)} %"


def config_hash(cfg: dict[str, Any] | None) -> str:
    raw = json.dumps(cfg or {}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _parse_dt(v: Any) -> datetime | None:
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        log.warning("Date illisible : %r", v)
        return None


def fmt_dt(d: datetime | None) -> str:
    return d.strftime("%d/%m/%Y à %H:%M") if d else "—"


# ── Entonnoir ────────────────────────────────────────────────────────────────
def normalize_funnel(funnel: list[dict[str, Any]], total: int, aoi_total: int | None) -> list[dict[str, Any]]:
    """Rattache chaque étape à un critère, dédoublonne, calcule entrée/retirées."""
    keyed = [(s, match_step_key(s) or f"step_{i}") for i, s in enumerate(funnel)]
    dedup = [(s, k) for i, (s, k) in enumerate(keyed)
             if not (i > 0 and keyed[i - 1][1] == k and keyed[i - 1][0].get("count") == s.get("count"))]
    if len(dedup) < len(keyed):
        log.info("Entonnoir : %d doublon(s) consécutif(s) supprimé(s)", len(keyed) - len(dedup))

    out, last = [], aoi_total
    for rank, (s, key) in enumerate(dedup, start=1):
        cnt = s.get("count")
        has = isinstance(cnt, (int, float)) and cnt >= 0
        if not has and rank == len(dedup):
            log.info("Dernière étape « %s » sans count → total (%d)", s.get("label"), total)
            cnt, has = total, True
        c_in, c_out = last, (int(cnt) if has else None)
        removed = c_in - c_out if (c_in is not None and c_out is not None) else None
        out.append({
            "rank": rank, "key": key, "raw_label": s.get("label", key),
            "count_in": c_in, "count_out": c_out, "removed": removed,
            "pct_removed": (removed / c_in * 100) if (removed is not None and c_in) else None,
        })
        if has:
            last = c_out
        log.debug("  #%d %-30s in=%s out=%s removed=%s", rank, key, c_in, c_out, removed)
    return out


# ── Synthèse rédigée (déterministe, pas de LLM → reproductible) ──────────────
def build_summary(ctx: dict[str, Any]) -> list[str]:
    p: list[str] = []
    start, total = ctx["start_count"], ctx["total"]
    zone = ctx["zone_label"]
    min_area = next((c for c in ctx["criteria"] if c["key"] == "min_area_ha"), None)
    if start is not None:
        # Si le point de départ est l'effectif APRÈS le filtre de surface, on le précise.
        first = ctx["funnel"][0] if ctx["funnel"] else None
        after_surface = bool(first and first["key"] == "min_area_ha" and first["count_in"] is None)
        surf = (f" d'au moins {min_area['params'][0][1]}"
                if after_surface and min_area and min_area["params"] else "")
        verb = "ont été identifiées" if after_surface else "ont été examinées"
        p.append(f"{zone[0].upper()}{zone[1:]}, {fmt_int(start)} parcelles ou unités foncières{surf} {verb}. "
                 f"Après application successive de {ctx['n_applied']} critères, "
                 f"{fmt_int(total)} sont retenues, soit {fmt_pct(total / start * 100) if start else '—'} du point de départ.")
    else:
        p.append(f"Après application successive de {ctx['n_applied']} critères, {fmt_int(total)} parcelles sont retenues.")

    top = ctx["top_selective"]
    if top:
        t0 = top[0]
        s = (f"Le critère le plus discriminant est « {t0['label']} », qui écarte {fmt_int(t0['removed'])} parcelles, "
             f"soit {fmt_pct(t0['pct_removed'])} de celles encore en lice à ce stade.")
        if len(top) > 1:
            others = [f"« {t['label']} » (−{fmt_int(t['removed'])})" for t in top[1:]]
            s += f" Viennent ensuite {' et '.join(others)}."
        p.append(s)

    zero = [c["label"] for c in ctx["funnel"] if c["removed"] == 0 and c["key"] in ctx["criteria_by_key"]]
    if zero:
        p.append(f"{len(zero)} critère{'s' if len(zero) > 1 else ''} appliqué{'s' if len(zero) > 1 else ''} "
                 f"n'{'ont' if len(zero) > 1 else 'a'} écarté aucune parcelle supplémentaire "
                 f"({', '.join(zero)}) : les parcelles concernées avaient déjà été écartées par un critère précédent, "
                 f"ou aucune n'était concernée.")
    if ctx["not_applied"]:
        p.append(f"{len(ctx['not_applied'])} critères disponibles n'ont pas été activés pour cette étude "
                 f"(voir section 3).")
    return p


# ── Contexte complet ─────────────────────────────────────────────────────────
PARCEL_COLUMNS = [
    # (clé, titre, condition d'affichage (clé critère) | None, formateur)
    ("idu", "Parcelle (IDU)", None, str),
    ("commune", "Commune", None, str),
    ("surface_ha", "Surface", None, lambda v: f"{fmt_num(v)} ha"),
    ("fauna_distances", "Espèce(s) — distance", "fauna_criteria",
     lambda v: "\n".join(f"{k} — {fmt_dist(d)}" for k, d in v.items()) if isinstance(v, dict) else str(v)),
    ("veg_libelles", "Milieux", "cesbio_libelles", lambda v: ", ".join(v) if isinstance(v, list) else str(v)),
    ("zone_humide_ha", "ZH établie", "zone_humide_mode", lambda v: f"{fmt_num(v)} ha"),
    ("dist_hydro_m", "Cours d'eau", "troncons_hydros_max_dist_m", lambda v: fmt_dist(v)),
    ("dist_surface_hydro_m", "Plan d'eau", "surfaces_hydros_max_dist_m", lambda v: fmt_dist(v)),
]


def build_report(data: dict[str, Any]) -> dict[str, Any]:
    cfg = data.get("filter_config")
    if cfg is None:
        log.warning("Pas de filter_config : le rapport ne pourra pas détailler les paramètres.")
    total = int(data["total"])
    buffer_km = data.get("buffer_km")
    versions = data.get("data_versions") or {}

    crits: list[Criterion] = build_criteria(cfg, buffer_km)
    funnel = normalize_funnel(data.get("funnel") or [], total, data.get("aoi_total"))
    fby = {}
    for f in funnel:
        fby.setdefault(f["key"], f)

    criteria = []
    for cr in crits:
        f = fby.get(cr.key)
        src = cr.source
        if cr.key in versions:
            src = f"{src} — {versions[cr.key]}" if src else versions[cr.key]
        criteria.append({
            "key": cr.key, "theme": cr.theme, "name": cr.name, "label": cr.label, "rule": cr.rule,
            "applied": cr.applied, "params": cr.params, "source": src, "in_funnel": cr.in_funnel,
            "stats": f,
        })
    cby = {c["key"]: c for c in criteria}
    for f in funnel:
        c = cby.get(f["key"])
        f["label"] = c["label"] if c else f["raw_label"]
        f["theme_label"] = THEME_LABELS.get(c["theme"], "—") if c else "—"

    applied = [c for c in criteria if c["applied"] is not False]
    not_applied = [c for c in criteria if c["applied"] is False]
    groups = [{"theme": t, "label": THEME_LABELS[t], "criteria": [c for c in applied if c["theme"] == t]}
              for t in THEME_ORDER]
    groups = [g for g in groups if g["criteria"]]

    # Contrôles de cohérence
    warnings: list[str] = []
    for c in applied:
        if c["applied"] and c["in_funnel"] and c["stats"] is None and funnel:
            warnings.append(f"Le critère « {c['label']} » est actif dans la configuration mais n'apparaît pas "
                            "dans l'entonnoir enregistré.")
    for f in funnel:
        c = cby.get(f["key"])
        if c is None:
            warnings.append(f"Étape d'entonnoir non reconnue : « {f['raw_label']} ».")
        elif c["applied"] is False:
            warnings.append(f"L'étape « {f['raw_label']} » figure dans l'entonnoir alors que le critère "
                            "est désactivé dans la configuration.")
    if funnel and funnel[-1]["count_out"] is not None and funnel[-1]["count_out"] != total:
        warnings.append(f"Le dernier effectif de l'entonnoir ({fmt_int(funnel[-1]['count_out'])}) diffère "
                        f"du nombre de parcelles retenues ({fmt_int(total)}).")
    for w in warnings:
        log.warning(w)

    start = data.get("aoi_total")
    if start is None and funnel:
        start = funnel[0]["count_out"] if funnel[0]["count_in"] is None else funnel[0]["count_in"]

    top = sorted([f for f in funnel if (f["removed"] or 0) > 0 and f["key"] != "min_area_ha"],
                 key=lambda f: -f["removed"])[:3]

    # Tableau des parcelles retenues (colonnes selon critères actifs)
    parcelles = data.get("parcelles") or []
    cols = [col for col in PARCEL_COLUMNS
            if (col[2] is None or (cby.get(col[2]) or {}).get("applied"))
            and any(p.get(col[0]) not in (None, "", [], {}) for p in parcelles)]
    rows = []
    for i, p in enumerate(parcelles, start=1):
        row = [str(i)]
        for key, _, _, f in cols:
            v = p.get(key)
            row.append("—" if v in (None, "", [], {}) else f(v))
        rows.append(row)
    surf_total = sum(p.get("surface_ha") or 0 for p in parcelles) if parcelles else None

    etude = data.get("etude") or {}
    ctx: dict[str, Any] = {
        "etude": etude,
        "computed_at": fmt_dt(_parse_dt(data.get("computed_at"))),
        "generated_at": fmt_dt(datetime.now()),
        "config_hash": config_hash(cfg),
        "config_json": ("{\n" + ",\n".join(f"  {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}"
                                             for k, v in cfg.items()) + "\n}") if cfg else None,
        "buffer_km": buffer_km,
        "zone_label": ("dans le périmètre du foncier déposé par le projet"
                       if (cfg or {}).get("search_mode") == "within_foncier"
                       else f"dans un rayon de {fmt_num(buffer_km)} km autour du foncier déposé par le projet"
                       if buffer_km else "dans la zone de recherche"),
        "total": total,
        "total_fmt": fmt_int(total),
        "start_count": start,
        "start_fmt": fmt_int(start),
        "surface_total": f"{fmt_num(surf_total, 1)} ha" if surf_total else None,
        "criteria": criteria,
        "criteria_by_key": cby,
        "groups": groups,
        "not_applied": not_applied,
        "n_applied": sum(1 for c in criteria if c["applied"] and c["in_funnel"]),
        "funnel": funnel,
        "top_selective": [dict(f, label=f["label"]) for f in top],
        "warnings": warnings,
        "parcel_header": ["N°"] + [c[1] for c in cols],
        "parcel_rows": rows,
        "sources": [(c["name"], c["source"]) for c in criteria if c["applied"] and c["source"]],
        "geo": data.get("geo"),
    }
    ctx["summary"] = build_summary(ctx)
    log.info("Rapport : %s → %s parcelles, %d critères appliqués, %d non appliqués, %d avertissement(s)",
             ctx["start_fmt"], ctx["total_fmt"], ctx["n_applied"], len(not_applied), len(warnings))
    return ctx


def main() -> None:
    ap = argparse.ArgumentParser(description="Construit le contexte du rapport de filtrage (sans PDF).")
    ap.add_argument("--input", required=True)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(levelname)s %(name)s — %(message)s")
    with open(a.input, encoding="utf-8") as f:
        ctx = build_report(json.load(f))
    print("\n".join(ctx["summary"]))
    print()
    for f in ctx["funnel"]:
        print(f"  {f['rank']:>2}. {f['label']:<55} {fmt_int(f['count_in']):>8} → {fmt_int(f['count_out']):>8}  "
              f"(−{fmt_int(f['removed'])})")


if __name__ == "__main__":
    main()
