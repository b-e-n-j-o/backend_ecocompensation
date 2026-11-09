"""
catalog.py — Traduction de `FilterConfig` en critères métier rédigés.

Pendant Python de `filterCatalog.ts` (front), enrichi des phrases de
méthodologie utilisées dans le rapport PDF. L'ordre des critères est
l'ordre métier du panneau `CreateAoiPage` (pas l'ordre SQL de l'entonnoir).

Usage CLI :
    python catalog.py --config filter_config.json [--buffer-km 5] [-v]
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

log = logging.getLogger("filter_report.catalog")

# ── Thèmes (ordre du panneau) ────────────────────────────────────────────────
THEMES: list[tuple[str, str]] = [
    ("zone", "Zone de recherche"),
    ("faune", "Présence d'espèces"),
    ("flore", "Présence de flore"),
    ("geometrie", "Filtre géométrique"),
    ("occupation", "Occupation du sol"),
    ("zones_humides", "Zones humides"),
    ("bassin_versant", "Bassin versant"),
    ("hydro", "Hydrographie"),
    ("vignes", "Arrachage de vignes"),
    ("exclusions", "Couches d'exclusion"),
]
THEME_LABELS = dict(THEMES)
THEME_ORDER = [k for k, _ in THEMES]

SEARCH_MODE_LABEL = {
    "exclude_source": "hors emprise du foncier projet",
    "exclude_source_buffer": "hors emprise du foncier projet (tampon autour du foncier)",
    "within_foncier": "dans le foncier projet (intra-périmètre)",
}
ZH_MODE_LABEL = {"ignore": "ignorées", "intersect": "doit intersecter", "exclude": "exclues"}

# Couches nationales d'exclusion — ordre de l'entonnoir.
EXCLUSION_LAYERS: list[dict[str, str]] = [
    {"key": "geomce", "label": "Hors mesures compensatoires existantes (GEOMCE)",
     "name": "Mesures compensatoires existantes (GEOMCE)",
     "rule": "toute parcelle intersectant une mesure compensatoire déjà déclarée (surface, linéaire ou ponctuelle, active) est écartée, afin de ne pas superposer deux engagements de compensation",
     "source": "GEOMCE (géolocalisation des mesures compensatoires)"},
    {"key": "preemption_ens", "label": "Hors zones de préemption ENS",
     "name": "Zones de préemption ENS",
     "rule": "toute parcelle située en zone de préemption au titre des Espaces Naturels Sensibles est écartée",
     "source": "Départements — zones de préemption ENS"},
    {"key": "ens", "label": "Hors Espaces Naturels Sensibles (ENS)",
     "name": "Espaces Naturels Sensibles (ENS)",
     "rule": "toute parcelle intersectant un site ENS est écartée",
     "source": "Départements — Espaces Naturels Sensibles"},
    {"key": "natura_2000", "label": "Hors sites Natura 2000",
     "name": "Sites Natura 2000",
     "rule": "toute parcelle intersectant un site Natura 2000 (ZPS ou ZSC) est écartée",
     "source": "INPN — Natura 2000"},
    {"key": "znieff_1", "label": "Hors ZNIEFF de type I",
     "name": "ZNIEFF de type I",
     "rule": "toute parcelle intersectant une ZNIEFF de type I est écartée",
     "source": "INPN — ZNIEFF"},
    {"key": "znieff_2", "label": "Hors ZNIEFF de type II",
     "name": "ZNIEFF de type II",
     "rule": "toute parcelle intersectant une ZNIEFF de type II est écartée",
     "source": "INPN — ZNIEFF"},
    {"key": "ebc", "label": "Hors Espaces Boisés Classés (EBC)",
     "name": "Espaces Boisés Classés (EBC)",
     "rule": "toute parcelle entièrement couverte (100 %) par un EBC existant est écartée ; les EBC à créer ne sont pas pris en compte",
     "source": "Documents d'urbanisme (PLU) — EBC"},
]


# Effet de chaque critère s'il avait été activé (section « non activés » du rapport).
NOT_APPLIED_EFFECT: dict[str, str] = {
    "fauna_criteria": "Conserver les parcelles proches d'observations d'espèces cibles",
    "flora_criteria": "Conserver les parcelles proches d'observations de flore",
    "miller_thresh": "Écarter les parcelles de forme trop allongée ou découpée",
    "cesbio_libelles": "Conserver les parcelles comportant certains milieux",
    "zone_humide_mode": "Conserver ou écarter les parcelles en zone humide établie",
    "zones_humides_probables_mode": "Conserver ou écarter les parcelles en zone humide probable",
    "same_bassin_versant": "Limiter aux bassins versants touchant le foncier du projet",
    "troncons_hydros_max_dist_m": "Conserver les parcelles proches d'un cours d'eau",
    "surfaces_hydros_max_dist_m": "Conserver les parcelles proches d'un plan d'eau",
    "arrachage_vignes": "Conserver les parcelles en arrachage de vignes",
    "geomce": "Écarter les parcelles déjà engagées en compensation",
    "preemption_ens": "Écarter les zones de préemption ENS",
    "ens": "Écarter les Espaces Naturels Sensibles",
    "natura_2000": "Écarter les sites Natura 2000",
    "znieff_1": "Écarter les ZNIEFF de type I",
    "znieff_2": "Écarter les ZNIEFF de type II",
    "ebc": "Écarter les parcelles entièrement en EBC",
}


@dataclass
class Criterion:
    key: str
    theme: str
    name: str                 # nom neutre (ex. « ZNIEFF de type II »)
    label: str                # libellé court avec paramètres
    rule: str                 # phrase de méthodologie (rapport)
    applied: bool | None      # None = config inconnue
    params: list[tuple[str, str]] = field(default_factory=list)
    source: str | None = None
    in_funnel: bool = True    # False = paramètre de contexte (zone de recherche)


# ── Formatage FR ─────────────────────────────────────────────────────────────
def fmt_num(x: float, decimals: int = 2) -> str:
    s = f"{x:,.{decimals}f}".replace(",", " ").replace(".", ",")
    if "," in s:
        s = s.rstrip("0").rstrip(",")
    return s.replace(" ", " ")


def fmt_dist(m: float) -> str:
    return f"{fmt_num(m / 1000, 1)}\u00a0km" if m >= 1000 else f"{fmt_num(m, 0)}\u00a0m"


def _join_fr(items: list[str], last: str = "et") -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} {last} {items[-1]}"


# ── Construction ─────────────────────────────────────────────────────────────
def build_criteria(config: dict[str, Any] | None, buffer_km: float | None = None) -> list[Criterion]:
    """Liste ordonnée (ordre panneau) de tous les critères, appliqués ou non."""
    c = config or {}
    known = config is not None

    def ap(cond: bool) -> bool | None:
        return cond if known else None

    out: list[Criterion] = []

    # 0. Zone de recherche + mode spatial
    mode = c.get("search_mode") or "exclude_source_buffer"
    within = mode == "within_foncier"
    if within:
        zone_rule = ("La recherche est limitée au périmètre du foncier déposé par le projet "
                     "(étude intra-périmètre, sans tampon).")
    else:
        zone_rule = ("La recherche porte sur les parcelles cadastrales situées "
                     + (f"dans un rayon de {fmt_num(buffer_km)} km " if buffer_km else "dans la zone tampon ")
                     + "autour du foncier déposé par le projet.")
    out.append(Criterion(
        key="zone_recherche", theme="zone", name="Zone de recherche",
        label=("Périmètre du foncier projet" if within
               else f"Rayon de {fmt_num(buffer_km)} km autour du foncier projet" if buffer_km
               else "Tampon autour du foncier projet"),
        rule=zone_rule, applied=ap(True), in_funnel=False,
        params=[("Rayon", "0 (intra-périmètre)" if within else (f"{fmt_num(buffer_km)} km" if buffer_km else "non renseigné"))],
        source="Cadastre (PCI Etalab)",
    ))
    out.append(Criterion(
        key="search_mode", theme="zone", name="Position par rapport au projet",
        label="Dans le foncier du projet" if within else "Hors emprise du projet",
        rule=("Seules les parcelles intersectant le foncier du projet sont conservées." if within else
              "Les parcelles composant le foncier déposé par le projet (emprise impactée) sont écartées : "
              "la compensation est recherchée en dehors de l'emprise du projet."),
        applied=ap(True), params=[("Mode", SEARCH_MODE_LABEL.get(mode, mode))],
    ))

    # 1. Faune
    fauna = [f for f in (c.get("fauna_criteria") or []) if isinstance(f, dict)]
    combine = (c.get("fauna_combine") or "or").lower()

    def _species(f: dict[str, Any]) -> str:
        return str(f.get("species") or f.get("label") or f.get("taxon_key") or "espèce")

    def _dist_label(f: dict[str, Any]) -> str:
        d = f.get("dist_m")
        try:
            return fmt_dist(float(d)) if d is not None else "—"
        except (TypeError, ValueError):
            return "—"

    if fauna:
        sp = [f"{_species(f)} (≤ {_dist_label(f)})" for f in fauna]
        if len(fauna) == 1:
            label = f"Présence : {_species(fauna[0])} (≤ {_dist_label(fauna[0])})"
            rule = (f"Une parcelle est conservée si au moins une observation de {_species(fauna[0])} "
                    f"est située à moins de {_dist_label(fauna[0])} de la parcelle.")
        elif combine == "and":
            label = f"Présence de toutes les espèces cibles ({len(fauna)})"
            rule = ("Une parcelle est conservée si, pour chacune des espèces suivantes, au moins une "
                    f"observation est située à moins de la distance indiquée : {_join_fr(sp)}.")
        else:
            label = f"Présence d'au moins une espèce cible ({len(fauna)})"
            rule = ("Une parcelle est conservée si au moins une observation de l'une des espèces suivantes "
                    f"est située à moins de la distance indiquée : {_join_fr(sp, 'ou')}.")
    else:
        label, rule = "Présence d'espèces", "Aucun critère de présence d'espèce n'a été appliqué."
    out.append(Criterion(
        key="fauna_criteria", theme="faune", name="Présence d'espèces", label=label, rule=rule,
        applied=ap(bool(fauna)),
        params=([(_species(f), f"observation à ≤ {_dist_label(f)}") for f in fauna]
                + ([("Combinaison", "toutes les espèces (ET)" if combine == "and" else "au moins une espèce (OU)")]
                   if len(fauna) > 1 else [])),
        source="Observations naturalistes",
    ))

    # 1 bis. Flore (jumeau de la faune)
    flora = [f for f in (c.get("flora_criteria") or []) if isinstance(f, dict)]
    fcomb = (c.get("flora_combine") or "or").lower()
    fl = [f"{f.get('label') or f.get('taxon_key')} (≤ {_dist_label(f)})" for f in flora]
    if len(flora) == 1:
        f0 = flora[0]
        flabel = f"Présence : {f0.get('label') or f0.get('taxon_key')} (≤ {_dist_label(f0)})"
        frule = (f"Une parcelle est conservée si au moins une observation de {f0.get('label') or f0.get('taxon_key')} "
                 f"est située à moins de {_dist_label(f0)} de la parcelle.")
    elif flora:
        flabel = (f"Présence de tous les taxons cibles ({len(flora)})" if fcomb == "and"
                  else f"Présence d'au moins un taxon cible ({len(flora)})")
        frule = ("Une parcelle est conservée si " + ("chacun des taxons suivants est observé" if fcomb == "and"
                 else "au moins un des taxons suivants est observé") + f" à moins de la distance indiquée : {_join_fr(fl, 'et' if fcomb == 'and' else 'ou')}.")
    else:
        flabel, frule = "Présence de flore", "Aucun critère de flore n'a été appliqué."
    out.append(Criterion(
        key="flora_criteria", theme="flore", name="Présence de flore", label=flabel, rule=frule,
        applied=ap(bool(flora)),
        params=([(f.get("label") or f.get("taxon_key"), f"observation à ≤ {_dist_label(f)}") for f in flora]
                + ([("Combinaison", "tous les taxons (ET)" if fcomb == "and" else "au moins un taxon (OU)")]
                   if len(flora) > 1 else [])),
        source="Observations flore (bureau d'études / OBV-NA)",
    ))

    # 2. Géométrie
    min_area = c.get("min_area_ha", 7.0 if not known else None)
    uf = c.get("include_unites_foncieres", True)
    out.append(Criterion(
        key="min_area_ha", theme="geometrie", name="Surface minimale",
        label=f"Surface ≥ {fmt_num(min_area)} ha" if min_area is not None else "Surface minimale",
        rule=(f"Seul le foncier d'une surface supérieure ou égale à {fmt_num(min_area)} ha est conservé"
              + (" ; le seuil s'apprécie à l'échelle de la parcelle ou de l'unité foncière "
                 "(ensemble de parcelles contiguës appartenant à une même personne morale)." if uf else
                 " ; le seuil s'apprécie à l'échelle de la parcelle cadastrale.")) if min_area is not None
             else "Surface minimale du foncier recherché.",
        applied=ap(True),
        params=[("Seuil", f"{fmt_num(min_area)} ha" if min_area is not None else "—"),
                ("Unités foncières", "incluses" if uf else "non (parcelles seules)")],
        source="Cadastre (PCI Etalab)",
    ))
    miller = c.get("miller_thresh")
    out.append(Criterion(
        key="miller_thresh", theme="geometrie", name="Compacité (indice de Miller)",
        label=f"Forme compacte (Miller ≥ {fmt_num(miller)})" if miller is not None else "Compacité (indice de Miller)",
        rule=(f"Les parcelles de forme trop allongée ou découpée sont écartées : l'indice de Miller "
              f"(4π × aire / périmètre², égal à 1 pour un cercle) doit être supérieur ou égal à {fmt_num(miller)}.")
             if miller is not None else "Aucun critère de compacité n'a été appliqué.",
        applied=ap(miller is not None),
        params=[("Seuil", fmt_num(miller)), ("Formule", "4π × aire / périmètre²")] if miller is not None else [],
    ))

    # 3. Occupation du sol
    libs = c.get("cesbio_libelles") or []
    out.append(Criterion(
        key="cesbio_libelles", theme="occupation", name="Occupation du sol (CESBIO)",
        label=(f"Occupation du sol : {', '.join(libs)}" if 0 < len(libs) <= 2
               else f"Occupation du sol : {len(libs)} milieux acceptés" if libs
               else "Occupation du sol"),
        rule=(f"Une parcelle est conservée si au moins un des milieux suivants l'intersecte : {_join_fr(libs, 'ou')}."
              if libs else "Aucun critère d'occupation du sol n'a été appliqué."),
        applied=ap(bool(libs)),
        params=[("Milieux acceptés", ", ".join(libs))] if libs else [],
        source="CESBIO — carte d'occupation des sols (OSO) / BD TOPO",
    ))

    # 4. Zones humides
    zh = c.get("zone_humide_mode") or "ignore"
    zh_min = c.get("min_zone_humide_ha") or 0
    if zh == "intersect":
        zl = f"Intersecte une zone humide établie{f' (≥ {fmt_num(zh_min)} ha)' if zh_min > 0 else ''}"
        zr = ("Une parcelle est conservée si elle intersecte une zone humide établie"
              + (f" sur au moins {fmt_num(zh_min)} ha." if zh_min > 0 else "."))
    elif zh == "exclude":
        zl, zr = "Hors zones humides établies", "Toute parcelle intersectant une zone humide établie est écartée."
    else:
        zl, zr = "Zones humides établies", "Les zones humides établies n'ont pas été prises en compte."
    out.append(Criterion(
        key="zone_humide_mode", theme="zones_humides", name="Zones humides établies", label=zl, rule=zr,
        applied=ap(zh != "ignore"),
        params=[("Mode", ZH_MODE_LABEL.get(zh, zh))] + ([("Surface min.", f"{fmt_num(zh_min)} ha")] if zh == "intersect" and zh_min > 0 else []),
        source="Inventaires des zones humides",
    ))
    zhp = c.get("zones_humides_probables_mode") or "ignore"
    out.append(Criterion(
        key="zones_humides_probables_mode", theme="zones_humides", name="Zones humides probables",
        label={"intersect": "Intersecte une zone humide probable",
               "exclude": "Hors zones humides probables"}.get(zhp, "Zones humides probables"),
        rule={"intersect": "Une parcelle est conservée si elle intersecte une zone humide probable (prélocalisation).",
              "exclude": "Toute parcelle intersectant une zone humide probable (prélocalisation) est écartée."}
             .get(zhp, "Les zones humides probables n'ont pas été prises en compte."),
        applied=ap(zhp != "ignore"), params=[("Mode", ZH_MODE_LABEL.get(zhp, zhp))],
        source="Prélocalisation des zones humides probables",
    ))

    # 5. Bassin versant
    bv = bool(c.get("same_bassin_versant"))
    out.append(Criterion(
        key="same_bassin_versant", theme="bassin_versant", name="Même bassin versant",
        label="Même bassin versant que le projet",
        rule=("Une parcelle est conservée si elle est située dans un bassin versant de masse d'eau "
              "qui touche le foncier déposé par le projet." if bv else "Le bassin versant n'a pas été pris en compte."),
        applied=ap(bv), source="Bassins versants des masses d'eau (SPE)",
    ))

    # 6. Hydrographie
    for key, name, obj, src in [
        ("troncons_hydros_max_dist_m", "Proximité d'un cours d'eau", "cours d'eau", "BD TOPO — tronçons hydrographiques"),
        ("surfaces_hydros_max_dist_m", "Proximité d'un plan d'eau", "plan d'eau", "BD TOPO — surfaces hydrographiques"),
    ]:
        d = c.get(key)
        if d is None:
            lab, rule = name, f"La proximité d'un {obj} n'a pas été prise en compte."
        elif d <= 0:
            lab, rule = f"Intersecte un {obj}", f"Une parcelle est conservée si elle intersecte un {obj}."
        else:
            lab = f"À ≤ {fmt_dist(d)} d'un {obj}"
            rule = f"Une parcelle est conservée si elle est située à moins de {fmt_dist(d)} d'un {obj}."
        out.append(Criterion(
            key=key, theme="hydro", name=name, label=lab, rule=rule, applied=ap(d is not None),
            params=[("Distance max.", "intersection" if d <= 0 else fmt_dist(d))] if d is not None else [],
            source=src,
        ))

    # 7. Vignes
    vig = bool(c.get("arrachage_vignes"))
    vtypes = c.get("arrachage_vignes_types") or []
    out.append(Criterion(
        key="arrachage_vignes", theme="vignes", name="Arrachage de vignes",
        label="Arrachage de vignes" + (f" : {', '.join(vtypes)}" if vtypes else " (tous types)"),
        rule=(("Une parcelle est conservée si elle intersecte une déclaration d'arrachage de vignes"
               + (f" de type {_join_fr(vtypes, 'ou')}." if vtypes else ", quel qu'en soit le type."))
              if vig else "L'arrachage de vignes n'a pas été pris en compte."),
        applied=ap(vig), params=[("Types", ", ".join(vtypes) if vtypes else "tous")] if vig else [],
        source="Déclarations d'arrachage de vignes",
    ))

    # 8. Exclusions
    layers = c.get("excluded_layers")
    for layer in EXCLUSION_LAYERS:
        on = (layer["key"] in layers) if layers is not None else None
        name = layer["name"]
        out.append(Criterion(
            key=layer["key"], theme="exclusions", name=name,
            label=layer["label"],
            rule=layer["rule"][0].upper() + layer["rule"][1:] + ".",
            applied=on if known else None, source=layer["source"],
        ))

    log.debug("build_criteria : %d critères, %d appliqués",
              len(out), sum(1 for x in out if x.applied))
    return out


# ── Rattachement étape d'entonnoir → critère ─────────────────────────────────
# Du plus spécifique au plus générique (préemption avant ENS, etc.).
STEP_MATCHERS: list[tuple[str, re.Pattern[str]]] = [(k, re.compile(p, re.I)) for k, p in [
    ("preemption_ens", r"pr[ée]emption"),
    ("zones_humides_probables_mode", r"humides?\s+probables?"),
    ("zone_humide_mode", r"zones?\s+humides?"),
    ("surfaces_hydros_max_dist_m", r"surfaces?\s+hydro|plans?\s+d['’]eau"),
    ("troncons_hydros_max_dist_m", r"tron[çc]ons?|cours\s+d['’]eau"),
    ("same_bassin_versant", r"bassin"),
    ("arrachage_vignes", r"vigne"),
    ("geomce", r"geomce|mesures?\s+compensatoires?"),
    ("znieff_2", r"znieff\D*(2|ii)\b"),
    ("znieff_1", r"znieff"),
    ("natura_2000", r"natura"),
    ("ebc", r"\bebc\b|bois[ée]s?\s+class"),
    ("ens", r"\bens\b|espaces?\s+naturels?\s+sensibles"),
    ("miller_thresh", r"miller|compacit"),
    ("cesbio_libelles", r"cesbio|occupation"),
    ("flora_criteria", r"flore"),
    ("fauna_criteria", r"faune|esp[èe]ces?|pr[ée]sence"),
    ("search_mode", r"emprise|source|within"),
    ("min_area_ha", r"candidats|surface"),
]]


def match_step_key(step: dict[str, Any]) -> str | None:
    if step.get("key"):
        return step["key"]
    label = step.get("label", "")
    for key, rx in STEP_MATCHERS:
        if rx.search(label):
            return key
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Affiche les critères métier issus d'un filter_config.")
    ap.add_argument("--config", required=True, help="JSON filter_config, ou JSON d'entrée complet (clé filter_config)")
    ap.add_argument("--buffer-km", type=float, default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(levelname)s %(name)s — %(message)s")
    with open(a.config, encoding="utf-8") as f:
        data = json.load(f)
    cfg = data.get("filter_config", data)
    buf = a.buffer_km if a.buffer_km is not None else data.get("buffer_km")
    for cr in build_criteria(cfg, buf):
        mark = {True: "✓", False: "–", None: "?"}[cr.applied]
        log.info("%s [%s] %s", mark, THEME_LABELS[cr.theme], cr.label)
        log.debug("    règle : %s", cr.rule)
        for k, v in cr.params:
            log.debug("    %s : %s", k, v)
    if a.verbose:
        print(json.dumps([asdict(x) for x in build_criteria(cfg, buf)], ensure_ascii=False, indent=2)[:2000])


if __name__ == "__main__":
    main()
