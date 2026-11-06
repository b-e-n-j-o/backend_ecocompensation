"""
uf_export_aggregate.py
======================

Agrégation parcelles → unité foncière pour l'export SIG, + dictionnaire des colonnes.

Principe : chaque parcelle d'une UF a exactement la même ligne d'attributs que la couche
`parcelles` (build_parcelle_export_row). La ligne de l'UF est déduite de ses parcelles avec
UNE règle par colonne (somme, minimum, oui-si-une, recalcul…). Les mêmes règles
alimentent la table `dictionnaire` livrée dans le GeoPackage → export et doc ne peuvent
pas diverger.

Module pur (pas de base) : testable sans PostGIS.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

from api.recherche_de_foncier.enrichir.pool.exports.classement_export_attrs import (
    EXPORT_ATTR_KEYS,
    _clip_text,
    format_occ_sol_compact,
    occ_sol_ratios,
)

# ──────────────────────────────────────────────────────────── schémas de couches
UF_HEADER: tuple[str, ...] = ("uf_id", "uf_rang", "uf_type", "nb_parc", "parcelles", "communes")
PARC_HEADER: tuple[str, ...] = ("uf_id", "uf_rang", "uf_type")
_IDENT_KEYS = {"rang", "idu", "cinsee", "code_insee", "section", "numero"}
UF_ENRICH_KEYS: tuple[str, ...] = tuple(k for k in EXPORT_ATTR_KEYS if k not in _IDENT_KEYS)
UF_LAYER_KEYS: tuple[str, ...] = UF_HEADER + UF_ENRICH_KEYS
UF_PARC_LAYER_KEYS: tuple[str, ...] = PARC_HEADER + EXPORT_ATTR_KEYS


# ──────────────────────────────────────────────────────────── règles + dictionnaire
# règle → libellé lisible (colonne regle_uf du dictionnaire)
RULE_LABEL = {
    "uf": "Valeur propre à l'UF (résultat du classement UF)",
    "ident": "Identifiant (parcelle uniquement)",
    "sum": "Somme des parcelles",
    "min_dist": "Minimum des parcelles (= distance de l'UF)",
    "any": "Oui si au moins une parcelle",
    "max": "Maximum des parcelles (parcelle la plus défavorable)",
    "min": "Minimum des parcelles",
    "wmean": "Moyenne pondérée par la surface des parcelles",
    "distinct": "Liste des valeurs distinctes des parcelles",
    "same": "Paramètre du filtrage (identique pour toutes les parcelles)",
    "recalc": "Recalculé sur l'UF à partir des surfaces des parcelles",
    "geom": "Recalculé sur la géométrie de l'UF",
    "concat": "Détails des parcelles, préfixés par leur IDU",
}

# colonne → (règle UF, description, unité)
COLUMNS: dict[str, tuple[str, str, str]] = {
    # en-têtes UF
    "uf_id": ("uf", "Identifiant de l'unité foncière", ""),
    "uf_rang": ("uf", "Rang de l'UF dans le classement des unités foncières", ""),
    "uf_type": ("uf", "Type d'unité foncière (PM = parcelles d'une même personne morale, ILOT = îlot RPG)", ""),
    "nb_parc": ("uf", "Nombre de parcelles cadastrales de l'UF", ""),
    "parcelles": ("uf", "Liste des IDU des parcelles de l'UF (détail dans la couche uf_parcelles)", ""),
    "communes": ("uf", "Codes INSEE des communes concernées", ""),
    # identification parcelle
    "rang": ("ident", "Rang : pour une parcelle d'UF, rang de son UF", ""),
    "idu": ("ident", "Identifiant cadastral de la parcelle (IDU)", ""),
    "cinsee": ("ident", "Code INSEE de la commune", ""),
    "code_insee": ("ident", "Code INSEE de la commune", ""),
    "section": ("ident", "Section cadastrale", ""),
    "numero": ("ident", "Numéro de parcelle", ""),
    # géométrie / localisation
    "surf_ha": ("sum", "Surface", "ha"),
    "miller": ("geom", "Indice de compacité de Miller (1 = disque, proche de 0 = forme allongée)", "0–1"),
    "dist_km": ("uf", "Distance au centre du projet", "km"),
    # hydrographie / zones humides
    "dist_hyd": ("min_dist", "Distance au tronçon hydrographique le plus proche (-1 = non calculé)", "m"),
    "zh_ha": ("sum", "Surface en zone humide", "ha"),
    "zh_int": ("any", "Intersecte une zone humide", "oui/non"),
    "zh_pct": ("recalc", "Part de la surface en zone humide", "%"),
    "zh_m2": ("sum", "Surface en zone humide", "m²"),
    "surf_hyha": ("sum", "Surface en eau (surfaces hydrographiques) intersectée", "ha"),
    "dist_shy": ("min_dist", "Distance à la surface hydrographique la plus proche", "m"),
    "zh": ("recalc", "Résumé zone humide (oui/non, %, m²)", ""),
    # scores
    "score_eco": ("uf", "Score écologique (points obtenus)", "points"),
    "eco_max": ("uf", "Score écologique maximum possible", "points"),
    "eco_esp": ("uf", "Points du score écologique liés aux espèces", "points"),
    "eco_dist": ("uf", "Points du score écologique liés à la distance au projet", "points"),
    "score_comp": ("wmean", "Score composite (synthèse écologie + foncier)", "0–100"),
    "cmp_attr": ("wmean", "Attractivité foncière utilisée dans le score composite", "0–100"),
    "cmp_redh": ("any", "Foncier rédhibitoire (au moins une parcelle)", "oui/non"),
    # dureté foncière
    "score_dur": ("max", "Dureté foncière (plus élevé = plus difficile à mobiliser)", "0–100"),
    "attr_fonc": ("min", "Attractivité foncière (100 − dureté)", "0–100"),
    "dur_niv": ("max", "Niveau de dureté (de la parcelle la plus dure)", ""),
    "dur_elig": ("any", "Dureté calculable (propriétaire personne morale identifié)", "oui/non"),
    "dur_axe1": ("max", "Dureté — axe 1", "0–100"),
    "dur_axe2": ("max", "Dureté — axe 2", "0–100"),
    "dur_axe3": ("max", "Dureté — axe 3", "0–100"),
    "dur_axe4": ("max", "Dureté — axe 4", "0–100"),
    "dur_sur": ("max", "Dureté — surcharges", "0–100"),
    "arr_vign": ("any", "Concerné par une aide à l'arrachage de vigne", "oui/non"),
    # faune
    "rayon_esp": ("min_dist", "Distance à l'observation d'espèce cible la plus proche (0 = intersection, -1 = aucune)", "m"),
    "espece_esp": ("recalc", "Espèce cible la plus proche", ""),
    "faune_txt": ("recalc", "Distance minimale par espèce cible", ""),
    # occupation du sol
    "cesbio": ("distinct", "Classes d'occupation du sol présentes", ""),
    "occ_sol": ("recalc", "Répartition de l'occupation du sol (BD TOPO végétation prioritaire, CESBIO ailleurs)", "%"),
    "occ_json": ("recalc", "Répartition de l'occupation du sol, format JSON {classe: part 0–1}", "0–1"),
    # paramètres du filtrage
    "rem_nappe": ("same", "Classes de remontée de nappe retenues dans le filtrage", ""),
    "ebc": ("same", "Mode de filtrage des espaces boisés classés", ""),
    "znieff": ("same", "Mode de filtrage ZNIEFF", ""),
    "zdv": ("same", "Natures de végétation BD TOPO retenues dans le filtrage", ""),
    "troncon": ("same", "Critère de distance aux tronçons hydrographiques", ""),
    "surf_hyd": ("same", "Critère de distance aux surfaces hydrographiques", ""),
    # personne morale
    "p_morale": ("any", "Propriétaire personne morale identifié", "oui/non"),
    "siren": ("distinct", "SIREN du propriétaire personne morale", ""),
    "pm_denom": ("distinct", "Dénomination du propriétaire personne morale", ""),
    "pm_forme": ("distinct", "Forme juridique du propriétaire", ""),
    "pm_prosp": ("any", "Le propriétaire a déjà porté de la compensation", "oui/non"),
    "pm_mc": ("any", "Parcelle déjà en mesure compensatoire", "oui/non"),
    "pm_nb_mc": ("max", "Nombre de mesures compensatoires distinctes du propriétaire", ""),
    "pm_nb_p": ("sum", "Nombre de parcelles déjà en mesure compensatoire", ""),
    "pm_s_mc": ("sum", "Surface déjà en mesure compensatoire", "m²"),
    # textes détaillés
    "txt_scor": ("concat", "Détail du score écologique", ""),
    "txt_comp": ("concat", "Détail du score composite", ""),
    "txt_dure": ("concat", "Détail de la dureté foncière", ""),
    "txt_espe": ("concat", "Détail des observations d'espèces", ""),
    "txt_enri": ("concat", "Détail de l'enrichissement du filtrage", ""),
    "txt_pm": ("concat", "Détail personne morale", ""),
    "txt_vege": ("concat", "Détail végétation", ""),
    "txt_cosi": ("concat", "Détail COSIA", ""),
    "txt_carb": ("concat", "Détail CarHab (habitats)", ""),
    "txt_arra": ("concat", "Détail arrachage de vigne", ""),
    "txt_zhum": ("concat", "Détail zones humides", ""),
}


def dictionary_rows() -> list[dict[str, str]]:
    """Une ligne par colonne exportée : couches, description, unité, règle d'agrégation UF."""
    rows = []
    for col in dict.fromkeys(UF_LAYER_KEYS + UF_PARC_LAYER_KEYS):
        rule, desc, unit = COLUMNS[col]
        layers = []
        if col in EXPORT_ATTR_KEYS:
            layers.append("parcelles")
        if col in UF_PARC_LAYER_KEYS:
            layers.append("uf_parcelles")
        if col in UF_LAYER_KEYS:
            layers.append("unites_foncieres")
        rows.append({
            "colonne": col,
            "couches": ", ".join(layers),
            "description": desc,
            "unite": unit,
            "regle_uf": RULE_LABEL[rule] if col in UF_LAYER_KEYS else "",
        })
    return rows


# ──────────────────────────────────────────────────────────── helpers
def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)) and math.isfinite(float(v)):
        return float(v)
    return None


def _sum(vals) -> float | None:
    xs = [x for x in map(_num, vals) if x is not None]
    return sum(xs) if xs else None


def _min_dist(vals) -> float | None:
    """Distances : -1 / None = non calculé. Renvoie -1 si rien n'est calculé mais qu'il y a des -1."""
    xs = [_num(v) for v in vals]
    ok = [x for x in xs if x is not None and x >= 0]
    if ok:
        return min(ok)
    return -1 if any(x is not None for x in xs) else None


def _any(vals) -> bool | None:
    bs = [v for v in vals if isinstance(v, bool)]
    if not bs:
        return None
    return any(bs)


def _max(vals):
    xs = [x for x in map(_num, vals) if x is not None]
    return max(xs) if xs else None


def _min(vals):
    xs = [x for x in map(_num, vals) if x is not None]
    return min(xs) if xs else None


def _wmean(vals, weights) -> float | None:
    pairs = [(_num(v), _num(w)) for v, w in zip(vals, weights)]
    pairs = [(v, w) for v, w in pairs if v is not None and w is not None and w > 0]
    if not pairs:
        return None
    tw = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / tw


def _distinct(vals, sep: str = ", ") -> str:
    out: list[str] = []
    for v in vals:
        for part in str(v or "").split(sep):
            p = part.strip()
            if p and p != "—" and p not in out:
                out.append(p)
    return sep.join(out)


def _same(vals) -> str:
    for v in vals:
        if v not in (None, ""):
            return v
    return ""


def _round(v, nd):
    return round(v, nd) if isinstance(v, float) else v


@dataclass
class ParcelItem:
    """Une parcelle d'UF : sa ligne d'export (build_parcelle_export_row) + ses métriques brutes."""
    row: dict[str, Any]
    mmap: dict[str, Any] = field(default_factory=dict)

    @property
    def idu(self) -> str:
        return str(self.row.get("idu") or "")

    @property
    def surf_ha(self) -> float:
        return float(_num(self.row.get("surf_ha")) or 0.0)


# ──────────────────────────────────────────────────────────── recalculs
def merge_occ_sol(items: list[ParcelItem]) -> dict[str, float]:
    """Parts 0–1 de l'UF = Σ(part × surface parcelle) / surface UF."""
    total = sum(it.surf_ha for it in items)
    if total <= 0:
        return {}
    acc: dict[str, float] = {}
    for it in items:
        for label, ratio in occ_sol_ratios(it.mmap).items():
            acc[label] = acc.get(label, 0.0) + float(ratio) * it.surf_ha
    return {k: round(v / total, 6) for k, v in sorted(acc.items(), key=lambda kv: -kv[1]) if v > 0}


def merge_fauna(items: list[ParcelItem]) -> dict[str, float]:
    """Distance minimale par espèce sur l'ensemble des parcelles."""
    out: dict[str, float] = {}
    for it in items:
        fd = (it.mmap.get("filter_enrich") or {}).get("fauna_distances") or {}
        if not isinstance(fd, dict):
            continue
        for sp, d in fd.items():
            x = _num(d)
            name = str(sp or "").strip()
            if not name or x is None or x < 0:
                continue
            out[name] = min(out.get(name, x), x)
    return out


def _faune_txt(fauna: dict[str, float]) -> str:
    ranked = sorted(fauna.items(), key=lambda kv: (kv[1], kv[0].lower()))
    return " ; ".join(f"{n}: intersection" if d <= 0 else f"{n}: {int(round(d))} m" for n, d in ranked)


def _zh_txt(zh_int, zh_pct, zh_m2, zh_ha) -> str:
    if zh_int is True and zh_pct is not None and zh_m2 is not None:
        return f"Oui · {zh_pct:.1f} % · {zh_m2:,.0f} m²".replace(",", " ")
    if zh_int is False:
        return "Non"
    if zh_ha is not None:
        return f"{zh_ha:.2f} ha"
    return ""


def _concat(items: list[ParcelItem], key: str, clip: bool) -> str:
    parts = [f"[{it.idu}] {str(it.row.get(key) or '').strip()}" for it in items if str(it.row.get(key) or "").strip()]
    return _clip_text("\n".join(parts), clip=clip)


# ──────────────────────────────────────────────────────────── agrégation UF
def aggregate_uf_row(
    items: list[ParcelItem],
    uf_info: dict[str, Any],
    *,
    uf_area_m2: float | None = None,
    uf_perimeter_m: float | None = None,
    clip_for_shapefile: bool = False,
) -> dict[str, Any]:
    """
    Ligne d'attributs d'une UF.

    uf_info : uf_id, uf_rang, uf_type, dist_centre_km, score_eco, score_eco_max,
              (optionnel) eco_esp, eco_dist, siren, denomination, forme_juridique, deja_compense.
    uf_area_m2 / uf_perimeter_m : de la géométrie union (pour Miller).
    """
    if not items:
        raise ValueError(f"UF {uf_info.get('uf_id')} sans parcelle")
    clip = clip_for_shapefile
    rows = [it.row for it in items]
    col = lambda k: [r.get(k) for r in rows]  # noqa: E731
    surf = [it.surf_ha for it in items]
    out: dict[str, Any] = {}

    # en-têtes
    out["uf_id"] = uf_info.get("uf_id")
    out["uf_rang"] = uf_info.get("uf_rang")
    out["uf_type"] = uf_info.get("uf_type") or "PM"
    out["nb_parc"] = len(items)
    out["parcelles"] = _clip_text("; ".join(it.idu for it in items), clip=clip)
    out["communes"] = _distinct(col("code_insee"))

    for k in UF_ENRICH_KEYS:
        rule = COLUMNS[k][0]
        if rule == "sum":
            out[k] = _sum(col(k))
        elif rule == "min_dist":
            out[k] = _min_dist(col(k))
        elif rule == "any":
            out[k] = _any(col(k))
        elif rule == "max":
            out[k] = _max(col(k)) if k != "dur_niv" else None
        elif rule == "min":
            out[k] = _min(col(k))
        elif rule == "wmean":
            out[k] = _wmean(col(k), surf)
        elif rule == "distinct":
            out[k] = _clip_text(_distinct(col(k)), clip=clip)
        elif rule == "same":
            out[k] = _same(col(k))
        elif rule == "concat":
            out[k] = _concat(items, k, clip)
        else:
            out[k] = None  # uf / recalc / geom : remplis plus bas

    # surfaces & zone humide
    surf_ha = out["surf_ha"] or 0.0
    out["surf_ha"] = round(surf_ha, 2)
    if out["zh_m2"] is None and out["zh_ha"] is not None:
        out["zh_m2"] = out["zh_ha"] * 10_000.0
    if out["zh_m2"] is not None and surf_ha > 0:
        out["zh_pct"] = round(min(100.0, out["zh_m2"] / (surf_ha * 10_000.0) * 100.0), 2)
    if out["zh_int"] is None and out["zh_ha"] is not None:
        out["zh_int"] = out["zh_ha"] > 0
    out["zh_ha"] = _round(out["zh_ha"], 2)
    out["zh_m2"] = _round(out["zh_m2"], 1)
    out["surf_hyha"] = _round(out["surf_hyha"], 2)
    out["zh"] = _zh_txt(out["zh_int"], out["zh_pct"], out["zh_m2"], out["zh_ha"])
    for k in ("dist_hyd", "dist_shy", "rayon_esp"):
        if out[k] is not None:
            out[k] = int(round(out[k]))

    # compacité sur la géométrie de l'UF
    if uf_area_m2 and uf_perimeter_m:
        out["miller"] = round(4 * math.pi * uf_area_m2 / (uf_perimeter_m ** 2), 4)

    # valeurs UF
    out["dist_km"] = _round(_num(uf_info.get("dist_centre_km")), 2)
    out["score_eco"] = uf_info.get("score_eco")
    out["eco_max"] = uf_info.get("score_eco_max")
    out["eco_esp"] = uf_info.get("eco_esp")
    out["eco_dist"] = uf_info.get("eco_dist")
    for k in ("score_comp", "cmp_attr"):
        out[k] = _round(out[k], 1)

    # dureté : niveau de la parcelle la plus dure
    worst = max(items, key=lambda it: _num(it.row.get("score_dur")) if _num(it.row.get("score_dur")) is not None else -1)
    out["dur_niv"] = worst.row.get("dur_niv") or "" if _num(worst.row.get("score_dur")) is not None else ""

    # faune : minimum par espèce
    fauna = merge_fauna(items)
    if fauna:
        name, dist = min(fauna.items(), key=lambda kv: (kv[1], kv[0].lower()))
        out["espece_esp"] = name[:254]
        out["faune_txt"] = _clip_text(_faune_txt(fauna), clip=clip)
    else:
        best = min(
            (it for it in items if (_num(it.row.get("rayon_esp")) or -1) >= 0),
            key=lambda it: _num(it.row.get("rayon_esp")),
            default=None,
        )
        out["espece_esp"] = (best.row.get("espece_esp") if best else "") or ""
        out["faune_txt"] = _concat(items, "faune_txt", clip)

    # occupation du sol : surfaces sommées puis % sur l'UF
    occ = merge_occ_sol(items)
    out["occ_json"] = _clip_text(json.dumps(occ, ensure_ascii=False, separators=(",", ":")) if occ else "", clip=clip)
    out["occ_sol"] = _clip_text(format_occ_sol_compact(occ, max_len=254 if clip else None), clip=clip) or out["cesbio"]

    # personne morale : valeurs de l'UF si connues
    if uf_info.get("siren"):
        out["siren"] = str(uf_info["siren"])[:20]
        out["p_morale"] = True
    if uf_info.get("denomination"):
        out["pm_denom"] = _clip_text(str(uf_info["denomination"]), clip=clip)
    if uf_info.get("forme_juridique"):
        out["pm_forme"] = _clip_text(str(uf_info["forme_juridique"]), clip=clip)
    if uf_info.get("deja_compense") is True:
        out["pm_prosp"] = True
    for k in ("pm_nb_mc", "pm_nb_p"):
        if out[k] is not None:
            out[k] = int(out[k])
    out["pm_s_mc"] = _round(out["pm_s_mc"], 0)

    return {k: out.get(k) for k in UF_LAYER_KEYS}


def uf_parcel_row(item: ParcelItem, uf_info: dict[str, Any]) -> dict[str, Any]:
    """Ligne de la couche uf_parcelles : en-tête UF + ligne parcelle inchangée."""
    row = dict(item.row)
    row["rang"] = uf_info.get("uf_rang") or 0
    head = {"uf_id": uf_info.get("uf_id"), "uf_rang": uf_info.get("uf_rang"), "uf_type": uf_info.get("uf_type") or "PM"}
    return {k: (head[k] if k in head else row.get(k)) for k in UF_PARC_LAYER_KEYS}
