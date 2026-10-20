"""
engine.py — Moteur de scoring urbanistique pour la compensation écologique.

    score_cible(cible, profil="generique") -> ScoreUrba

Le moteur ne contient AUCUNE valeur métier : tout vient de regles_compensation.yaml.
Il applique, dans l'ordre :

1. Zonage   : base de pérennité pondérée par le % de surface de chaque zone
              (type déduit de `typezone`, contrôlé par le libellé), vetos de zone,
              sous-secteurs (suffixes / libellé long), destinations v2024.
2. Éléments : chaque prescription / information / servitude est INTERPRÉTÉE
              (code CNIG + libellé saisi -> une règle + une fiabilité), puis
              pondérée par sa couverture. Les contributions d'une même règle
              sont cumulées puis plafonnées.
3. Voisinage: pression urbaine et continuité naturelle dans l'anneau.
4. Vetos    : plafonnement du score global (bloquant / majeur).
5. Confiance: indice séparé, qui qualifie la fiabilité du score sans le modifier.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Tuple, Union

from ..schemas import Cible, Couche, Element
from .loader import SOUS_SCORES, Rule, RuleSet, SecteurRule, get_rules, normalize_text
from .schemas import (
    Confiance,
    Contribution,
    ElementInterprete,
    PointAVerifier,
    ScoreUrba,
    SousScore,
    VetoOut,
)

FIAB_ORDER = {"haute": 2, "moyenne": 1, "basse": 0}


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _min_fiab(a: str, b: str) -> str:
    return a if FIAB_ORDER[a] <= FIAB_ORDER[b] else b


# =============================================================================
# 1. Interprétation d'un élément (code CNIG x libellé saisi)
# =============================================================================


@dataclass
class Interp:
    rule: Optional[Rule]
    fiabilite: str = "haute"
    source: str = "code"  # code | mots_cles | raffinement | aucun
    conflit: Optional[str] = None
    note: Optional[str] = None


def _element_text(el: Element, famille: str) -> str:
    a = el.attributs
    keys = ("nomsuplitt", "nomass", "typeass", "nomreg") if famille == "servitude" else ("libelle", "txt", "nature")
    return normalize_text(" | ".join(str(a[k]) for k in keys if a.get(k)))


def _kw_scope(rule: Rule, famille: str) -> bool:
    fams = set(rule.familles)
    if fams & {"prescription", "information"}:
        fams |= {"prescription", "information"}
    return famille in fams


def _keyword_rules(rs: RuleSet, famille: str, text: str) -> List[Rule]:
    if not text:
        return []
    return [r for r in rs.regles if r.mots_cles and _kw_scope(r, famille) and any(p.search(text) for p in r.mots_cles)]


def _refine(rs: RuleSet, rule: Rule, text: str) -> Tuple[Rule, bool]:
    for r2 in rs.raffinements.get(rule.id, []):
        if any(p.search(text) for p in r2.mots_cles_raffinement):
            # raffinement récursif (ex. ER -> ER ouvrage -> ER hydraulique)
            r3, _ = _refine(rs, r2, text)
            return r3, True
    return rule, False


def _code_rule(rs: RuleSet, famille: str, el: Element) -> Tuple[Optional[Rule], bool]:
    """(règle, approximative) — approximative = repli sur TT-* / TT-00 / préfixe SUP."""
    if famille == "servitude":
        st = re.sub(r"[^a-z0-9]", "", str(el.attributs.get("suptype") or "").lower())
        if not st:
            return None, False
        if st in rs.by_suptype:
            return rs.by_suptype[st], False
        for pfx, r in rs.by_suptype_prefix:
            if st.startswith(pfx):
                return r, True
        return None, False
    code = el.code
    if not code or "code_absent" in el.flags:
        return None, False
    r = rs.by_code.get((famille, code))
    if r:
        return r, False
    tt = code[:2]
    r = rs.by_code.get((famille, f"{tt}-*")) or rs.by_code.get((famille, f"{tt}-00"))
    return (r, True) if r else (None, False)


def interpret(rs: RuleSet, famille: str, el: Element) -> Interp:
    text = _element_text(el, famille)
    cr, approx = _code_rule(rs, famille, el)
    kws = _keyword_rules(rs, famille, text)

    if cr is None and not kws:
        return Interp(None, source="aucun")

    if cr is None:
        kr, refined = _refine(rs, kws[0], text)
        return Interp(kr, "basse", "raffinement" if refined else "mots_cles",
                      note="code absent, générique ou hors nomenclature : interprétation par le libellé")

    cr_ref, refined = _refine(rs, cr, text)
    base_fiab = "haute" if (text or famille == "servitude") else "moyenne"
    if approx:
        base_fiab = _min_fiab(base_fiab, "moyenne")

    if not kws:
        return Interp(cr_ref, base_fiab, "raffinement" if refined else "code")

    others = [k for k in kws if k.id not in (cr.id, cr_ref.id)]

    # 1. Code "fourre-tout" (99, 05-00, 18-00, 07-00/02...) : un libellé explicite
    #    le précise. Pas un conflit ; fiabilité haute si le libellé reste dans un
    #    groupe compatible avec le code, moyenne sinon.
    if cr_ref.generique:
        spec = [k for k in others if k.groupe != "neutre"]
        if not spec or refined:
            return Interp(cr_ref, base_fiab, "raffinement" if refined else "code")
        kr, _ = _refine(rs, spec[0], text)
        fiab = base_fiab if rs.compatibles(kr.groupe, cr_ref.groupe) and cr_ref.groupe != "neutre" else "moyenne"
        return Interp(kr, fiab, "mots_cles", note=f"code générique {el.code or ''} précisé par le libellé")

    # 2. Le libellé confirme-t-il le code ? (même règle, ou groupe compatible ;
    #    un mot-clé "neutre" ne contredit rien)
    if len(others) < len(kws) or any(
        k.groupe == "neutre" or (cr_ref.groupe != "neutre" and rs.compatibles(k.groupe, cr_ref.groupe)) for k in others
    ):
        return Interp(cr_ref, base_fiab, "raffinement" if refined else "code")

    kr, krefined = _refine(rs, others[0], text)

    # Vrai conflit : le libellé saisi l'emporte (il est recopié de la légende du
    # plan, alors que le code est choisi dans une liste par l'opérateur de saisie).
    msg = (f"Code {el.code or el.attributs.get('suptype')} ({cr_ref.label}) incohérent avec le libellé "
           f"« {el.attributs.get('libelle') or el.attributs.get('nomsuplitt') or ''} » "
           f"interprété comme : {kr.label}")
    return Interp(kr, "moyenne", "mots_cles", conflit=msg)


# =============================================================================
# 2. Zonage
# =============================================================================

_LIB_TYPE = [
    (re.compile(r"^\s*1\s*AU", re.I), "AUc"),
    (re.compile(r"^\s*(2|0)\s*AU", re.I), "AUs"),
    (re.compile(r"^\s*AU\s*(s|0|f|x?s)\b", re.I), "AUs"),
    (re.compile(r"^\s*AU", re.I), "AU?"),
    (re.compile(r"^\s*U", re.I), "U"),
    (re.compile(r"^\s*A", re.I), "A"),
    (re.compile(r"^\s*N", re.I), "N"),
]
_TZ = {"U": "U", "AUC": "AUc", "AUS": "AUs", "AU": "AUc", "A": "A", "N": "N", "AH": "Ah", "NH": "Nh"}
_FAMILY = {"U": "U", "AUc": "AU", "AUs": "AU", "AU?": "AU", "A": "A", "Ah": "A", "N": "N", "Nh": "N"}


def resolve_zone_type(typezone: Any, libelle: Any) -> Tuple[str, str, Optional[str]]:
    """
    (type, fiabilité, conflit). Le type saisi (typezone) est contrôlé par le préfixe
    du libellé (1AU, 2AU, U, A, N). En cas de désaccord entre grandes familles
    (U / AU / A / N), le libellé l'emporte ; entre AUc et AUs, on retient l'option
    la plus pessimiste (AUc).
    """
    tz = _TZ.get(str(typezone or "").strip().upper())
    lib = str(libelle or "").strip()
    lt = None
    for rx, t in _LIB_TYPE:
        if lib and rx.match(lib):
            lt = t
            break
    if tz and not lt:
        return tz, ("haute" if lib else "moyenne"), None
    if not tz and lt:
        return ("AUc" if lt == "AU?" else lt), "basse", None
    if not tz and not lt:
        return "inconnu", "basse", None
    if _FAMILY[tz] == _FAMILY[lt]:
        if lt in ("AUc", "AUs") and tz in ("AUc", "AUs") and lt != tz:
            return "AUc", "moyenne", f"typezone {tz} / libellé « {lib} » : AUc retenu (hypothèse prudente)"
        return tz, "haute", None
    final = "AUc" if lt == "AU?" else lt
    return final, "moyenne", f"typezone {tz} incohérent avec le libellé « {lib} » : {final} retenu"


@dataclass
class Acc:
    """Accumulateur des contributions d'une règle."""

    rule_id: str
    label: str
    source: str
    effets: Dict[str, float]
    justification: str = ""
    a_confirmer: bool = False
    veto: list = field(default_factory=list)
    point_a_verifier: Optional[str] = None
    cap: float = 1.0
    facteur: float = 0.0
    couverture: float = 0.0
    has_surface: bool = False
    element_ids: List[str] = field(default_factory=list)
    fiab: str = "haute"
    conflits: List[str] = field(default_factory=list)
    details: List[str] = field(default_factory=list)


# =============================================================================
# 3. Facteurs
# =============================================================================


def _factor(rs: RuleSet, el: Element, geom_kind: str, portee: str, cible_area: float) -> Tuple[float, Optional[float]]:
    """(facteur de couverture, couverture_pct si surfacique)."""
    f = rs.facteurs
    m = el.mesure
    lim = f["multiplicateur_limite"] if m.position == "limite" else 1.0
    if geom_kind == "surf":
        pct = m.pct_cible or 0.0
        if portee == "presence":
            return 1.0, pct
        return _clamp(pct / 100.0, f["surface_min"], 1.0), pct
    if portee == "presence":
        return lim, None
    if geom_kind == "lin":
        L = m.longueur_m or 0.0
        ref = math.sqrt(cible_area) if cible_area > 0 else 1.0
        return _clamp(L / ref, f["lineaire_min"], 1.0) * lim, None
    return (f["ponctuel_limite"] if m.position == "limite" else f["ponctuel_interieur"]), None


# =============================================================================
# 4. Scoring
# =============================================================================


def score_cible(
    cible: Union[Cible, Dict[str, Any]],
    profil: str = "generique",
    rules: Optional[RuleSet] = None,
    today: Optional[date] = None,
) -> ScoreUrba:
    rs = rules or get_rules()
    if profil not in rs.profils:
        raise ValueError(f"Profil inconnu {profil!r} ; disponibles : {list(rs.profils)}")
    if isinstance(cible, dict):
        cible = Cible.model_validate(cible)
    today = today or date.today()
    mult = rs.fiab_mult

    accs: Dict[str, Acc] = {}
    contributions: List[Contribution] = []
    points: List[PointAVerifier] = []
    interpretations: List[ElementInterprete] = []
    vetos: List[VetoOut] = []
    conf_motifs: List[str] = []
    conf = float(rs.confiance["depart"])
    pen = rs.confiance["penalites"]

    def add_point(rule_id: str, texte: Optional[str], ids: List[str]):
        if not texte:
            return
        for p in points:
            if p.rule_id == rule_id and p.texte == texte:
                p.element_ids = sorted(set(p.element_ids) | set(ids))
                return
        points.append(PointAVerifier(rule_id=rule_id, texte=texte, element_ids=ids))

    def get_acc(key: str, label: str, source: str, effets: Dict[str, float], **kw) -> Acc:
        if key not in accs:
            accs[key] = Acc(rule_id=key, label=label, source=source, effets=effets, **kw)
        return accs[key]

    # ---------------------------------------------------------------- Zonage
    zonage_couches = [c for c in cible.couches if c.famille == "zonage"]
    zonage_error = any(c.status == "error" for c in zonage_couches)
    zones: List[Tuple[str, float, Element, str]] = []  # (type, pct, element, layer_key)
    for c in zonage_couches:
        if c.status == "error":
            continue
        for el in c.elements:
            pct = el.mesure.pct_cible or 0.0
            if c.layer_key == "secteur_cc":
                t = f"CC_{el.code}" if el.code and f"CC_{el.code}" in rs.zonage_base else "CC_99"
                fiab, conflit = ("haute" if el.code else "basse"), None
            else:
                t, fiab, conflit = resolve_zone_type(el.attributs.get("typezone"), el.attributs.get("libelle"))
            if conflit:
                conf -= pen["zonage_conflit_type"]
                conf_motifs.append(f"Zonage : {conflit}")
            interpretations.append(ElementInterprete(
                element_id=el.element_id, layer_key=c.layer_key, code=el.code,
                libelle=el.attributs.get("libelle"), rule_id=f"zonage:{t}", fiabilite=fiab,
                source="code" if not conflit else "mots_cles", conflit=conflit))
            zones.append((t, pct, el, c.layer_key, fiab))

    zb = rs.zonage_base
    if zones:
        covered = sum(z[1] for z in zones)
        num = sum(z[1] * zb.get(z[0], zb["inconnu"])["valeur"] for z in zones)
        if covered < 100.0:
            num += (100.0 - covered) * zb["inconnu"]["valeur"]
            denom = 100.0
        else:
            denom = covered
        base_per = num / denom
        if covered < 90.0 or covered > 110.0:
            conf -= pen["zonage_couverture_anormale"]
            conf_motifs.append(f"Couverture du zonage anormale ({covered:.0f} % de la parcelle)")
        detail = " + ".join(f"{z[0]} {z[2].attributs.get('libelle') or ''} {z[1]:.0f} %" for z in zones)
        contributions.append(Contribution(
            rule_id="zonage_base", label="Base de pérennité du zonage", source="zonage",
            points={"perennite": round(base_per, 1)}, facteur=1.0, couverture_pct=round(covered, 1),
            element_ids=[z[2].element_id for z in zones], fiabilite=min((z[4] for z in zones), key=FIAB_ORDER.get),
            detail=detail, justification="Moyenne des bases par type de zone, pondérée par la surface."))
        for t in {z[0] for z in zones}:
            add_point(f"zonage:{t}", zb.get(t, {}).get("point_a_verifier"), [z[2].element_id for z in zones if z[0] == t])
        # vetos de zonage
        for v in rs.zonage_veto:
            sel = [z for z in zones if z[0] in v["types"]]
            pct = sum(z[1] for z in sel)
            if sel and pct >= v["seuil_pct"]:
                f_ = min((z[4] for z in sel), key=FIAB_ORDER.get)
                if f_ == "basse" and not rs.veto_si_fiabilite_basse:
                    add_point("zonage_veto", f"Veto non appliqué (donnée de zonage peu fiable) : {v['motif']}",
                              [z[2].element_id for z in sel])
                else:
                    vetos.append(VetoOut(rule_id=f"zonage:{'/'.join(v['types'])}", niveau=v["niveau"], motif=v["motif"],
                                         couverture_pct=round(pct, 1), element_ids=[z[2].element_id for z in sel], fiabilite=f_))
    else:
        key = "inconnu" if zonage_error else "RNU"
        base_per = zb[key]["valeur"]
        if not zonage_error:
            conf -= pen["aucun_zonage_gpu"]
            conf_motifs.append("Aucun zonage GPU (RNU ou document non versé)")
        contributions.append(Contribution(
            rule_id=f"zonage_base:{key}", label="Base de pérennité du zonage", source="zonage",
            points={"perennite": float(base_per)}, facteur=1.0, fiabilite="basse",
            detail=zb[key]["justification"], justification=zb[key]["justification"]))
        add_point(f"zonage:{key}", zb[key].get("point_a_verifier"), [])

    # Sous-secteurs
    for t, pct, el, lk, zfiab in zones:
        lib = str(el.attributs.get("libelle") or "").strip()
        libelong = normalize_text(el.attributs.get("libelong"))
        for sr in rs.secteurs:
            if t not in sr.types:
                continue
            rx_hit = bool(sr.libelle_regex and lib and sr.libelle_regex.search(lib))
            kw_hit = bool(libelong and any(p.search(libelong) for p in sr.mots_cles))
            if not (rx_hit or kw_hit):
                continue
            fiab = "haute" if kw_hit else "moyenne"
            fiab = _min_fiab(fiab, zfiab)
            a = get_acc(sr.id, sr.label, "secteur", sr.effets_pour(profil), justification=sr.justification,
                        a_confirmer=sr.a_confirmer, veto=sr.veto, point_a_verifier=sr.point_a_verifier)
            fac = _clamp(pct / 100.0, rs.facteurs["surface_min"], 1.0)
            a.facteur += fac * mult[fiab]
            a.couverture += pct
            a.has_surface = True
            a.element_ids.append(el.element_id)
            a.fiab = _min_fiab(a.fiab, fiab)
            a.details.append(f"{lib} ({'libellé long' if kw_hit else 'suffixe'})")

        # Destinations v2024
        dest = str(el.attributs.get("destoui") or "")
        if dest:
            codes = [c.strip() for c in re.split(r"[-,;\s]+", dest) if c.strip()]
            for name, cfg in rs.zonage_destinations.items():
                if t in cfg.get("types", []) and any(c.startswith(tuple(cfg["prefixes"])) for c in codes):
                    a = get_acc(f"destination_{name}", f"Destination {name} autorisée en zone {t}", "destination",
                                {"perennite": cfg["perennite"]},
                                justification="Champ DESTOUI (standard v2024) : destinations autorisées dans la zone.")
                    a.facteur += _clamp(pct / 100.0, rs.facteurs["surface_min"], 1.0)
                    a.couverture += pct
                    a.element_ids.append(el.element_id)

    # -------------------------------------------------------------- Éléments
    n_conflits = n_basse = n_non_interp = 0
    for c in cible.couches:
        if c.famille == "zonage":
            continue
        if c.status == "error":
            continue
        for el in c.elements:
            it = interpret(rs, c.famille, el)
            interpretations.append(ElementInterprete(
                element_id=el.element_id, layer_key=c.layer_key, code=el.code or el.attributs.get("suptype"),
                libelle=el.attributs.get("libelle") or el.attributs.get("nomsuplitt") or el.attributs.get("nomass"),
                rule_id=it.rule.id if it.rule else None, fiabilite=it.fiabilite if it.rule else None,
                source=it.source, conflit=it.conflit))
            if it.rule is None:
                n_non_interp += 1
                continue
            if it.conflit:
                n_conflits += 1
                add_point("conflit_code_libelle", it.conflit + " — vérifier sur le plan de zonage.", [el.element_id])
            if it.fiabilite == "basse":
                n_basse += 1
            r = it.rule
            if r.neutre:
                continue
            a = get_acc(r.id, r.label, "element", r.effets_pour(profil), justification=r.justification,
                        a_confirmer=r.a_confirmer, veto=r.veto, point_a_verifier=r.point_a_verifier,
                        cap=float(r.raw.get("cap", 1.0)))
            fac, cov = _factor(rs, el, c.geom_kind, r.portee, cible.surface_m2)
            a.facteur += fac * mult[it.fiabilite]
            if cov is not None:
                a.couverture += cov
                a.has_surface = True
            a.element_ids.append(el.element_id)
            a.fiab = _min_fiab(a.fiab, it.fiabilite)
            if it.conflit:
                a.conflits.append(it.conflit)

    # ------------------------------------------------------------- Voisinage
    an = cible.synthese.anneau_zonage
    if an and an.pct:
        p = an.pct
        vcfg = rs.voisinage
        pu = vcfg.get("pression_urbaine")
        if pu:
            pression = p.get("U", 0) + p.get("AUc", 0) + p.get("CC_01", 0) + p.get("CC_02", 0) + pu["coef_AUs"] * p.get("AUs", 0)
            pts = max(pu["plafond"], pression * pu["points_par_pct"])
            if pts:
                contributions.append(Contribution(
                    rule_id="voisinage_pression_urbaine", label=f"Pression urbaine dans un rayon de {an.rayon_m:.0f} m",
                    source="voisinage", points={"perennite": round(pts, 1)}, facteur=1.0,
                    detail=f"U {p.get('U', 0)} %, AUc {p.get('AUc', 0)} %, AUs {p.get('AUs', 0)} %",
                    justification=pu["justification"]))
        cn = vcfg.get("continuite_naturelle")
        if cn:
            cont = p.get("N", 0) + p.get("Nh", 0) * 0.5 + cn["coef_A"] * (p.get("A", 0) + p.get("CC_03", 0))
            pts = min(cn["plafond"], cont * cn["points_par_pct"])
            if pts:
                contributions.append(Contribution(
                    rule_id="voisinage_continuite_naturelle", label=f"Continuité naturelle dans un rayon de {an.rayon_m:.0f} m",
                    source="voisinage", points={"synergie": round(pts, 1)}, facteur=1.0,
                    detail=f"N {p.get('N', 0)} %, A {p.get('A', 0)} %", justification=cn["justification"]))

    # --------------------------------------------- Accumulateurs -> contributions
    for key, a in accs.items():
        fac = min(a.cap, a.facteur)
        pts = {k: round(v * fac, 2) for k, v in a.effets.items() if v}
        if pts.get("confiance"):
            conf += pts["confiance"]
            conf_motifs.append(f"{a.label} ({pts['confiance']:+.0f})")
        contributions.append(Contribution(
            rule_id=key, label=a.label, source=a.source, points=pts, facteur=round(fac, 3),
            couverture_pct=round(a.couverture, 1) if a.has_surface else None, element_ids=a.element_ids,
            fiabilite=a.fiab, conflits=a.conflits, detail="; ".join(a.details) or None,
            justification=a.justification, a_confirmer=a.a_confirmer))
        add_point(key, a.point_a_verifier, a.element_ids)
        # vetos
        for v in a.veto:
            if v.profils and profil not in v.profils:
                continue
            if not a.has_surface or a.couverture < v.seuil_pct:
                continue
            if a.fiab == "basse" and not rs.veto_si_fiabilite_basse:
                add_point(key, f"Veto non appliqué (donnée peu fiable) : {v.motif}", a.element_ids)
                continue
            vetos.append(VetoOut(rule_id=key, niveau=v.niveau, motif=v.motif, couverture_pct=round(a.couverture, 1),
                                 element_ids=a.element_ids, fiabilite=a.fiab))

    # ---------------------------------------------------------- Sous-scores
    deltas = {k: 0.0 for k in SOUS_SCORES}
    for ctb in contributions:
        if ctb.source == "zonage":
            continue
        for k, v in ctb.points.items():
            if k in deltas:
                deltas[k] += v
    bases = {"perennite": base_per, **{k: float(rs.base[k]) for k in ("compatibilite", "synergie", "maitrise")}}
    sous = {
        k: SousScore(valeur=round(_clamp(bases[k] + deltas[k], 0, 100), 1), base=round(bases[k], 1),
                     delta=round(deltas[k], 1), definition=rs.sous_scores.get(k, ""))
        for k in SOUS_SCORES
    }
    g_avant = sum(rs.ponderation[k] * sous[k].valeur for k in SOUS_SCORES)
    g = g_avant
    if any(v.niveau == "bloquant" for v in vetos):
        g = min(g, rs.plafonds_veto["bloquant"])
    elif any(v.niveau == "majeur" for v in vetos):
        g = min(g, rs.plafonds_veto["majeur"])
    g = round(g, 1)
    cls = next(c for c in rs.classes if g >= c["min"])

    # ------------------------------------------------------------ Confiance
    errs = [c.layer_key for c in cible.couches if c.status == "error"]
    if errs:
        conf -= min(pen["couche_en_erreur_max"], pen["couche_en_erreur"] * len(errs))
        conf_motifs.append(f"Couches indisponibles : {', '.join(errs)}")
    if n_conflits:
        conf -= min(pen["element_conflit_max"], pen["element_conflit"] * n_conflits)
        conf_motifs.append(f"{n_conflits} incohérence(s) code / libellé")
    if n_basse:
        conf -= min(pen["element_fiabilite_basse_max"], pen["element_fiabilite_basse"] * n_basse)
        conf_motifs.append(f"{n_basse} élément(s) interprété(s) sur le seul libellé")
    if n_non_interp:
        conf -= min(pen["element_non_interprete_max"], pen["element_non_interprete"] * n_non_interp)
        conf_motifs.append(f"{n_non_interp} élément(s) non interprété(s)")
    for d in cible.documents:
        if (d.typedoc or "").upper() == "POS":
            conf -= pen["document_pos"]
            conf_motifs.append(f"Document de type POS ({d.idurba}) : caduc depuis 2020 sauf exception")
            break
        if d.date:
            try:
                y = int(d.date[:4])
                if today.year - y > 15:
                    conf -= pen["document_ancien_15ans"]
                    conf_motifs.append(f"Document ancien ({d.idurba}) : révision probable")
                    break
            except ValueError:
                pass
    conf = round(_clamp(conf, 0, 100), 1)
    niv = "haute" if conf >= rs.confiance["niveaux"]["haute"] else ("moyenne" if conf >= rs.confiance["niveaux"]["moyenne"] else "faible")

    # --------------------------------------------------------------- Résumé
    resume: List[str] = []
    if zones:
        resume.append("Zonage : " + ", ".join(f"{z[2].attributs.get('libelle') or z[0]} ({z[0]}, {z[1]:.0f} %)" for z in
                                               sorted(zones, key=lambda z: -z[1])))
    else:
        resume.append("Zonage : aucun zonage GPU (RNU ou document non versé)" if not zonage_error else "Zonage : couche indisponible")
    signed = [(sum(v for k, v in c.points.items() if k in SOUS_SCORES), c) for c in contributions if c.source != "zonage"]
    neg = sorted([s for s in signed if s[0] < 0], key=lambda s: s[0])[:3]
    pos = sorted([s for s in signed if s[0] > 0], key=lambda s: -s[0])[:3]
    if neg:
        resume.append("Points défavorables : " + " ; ".join(f"{c.label} ({v:+.0f})" for v, c in neg))
    if pos:
        resume.append("Points favorables : " + " ; ".join(f"{c.label} ({v:+.0f})" for v, c in pos))
    for v in vetos:
        resume.append(f"Veto {v.niveau} : {v.motif}")
    resume.append(f"Score {g:.0f}/100 ({cls['code']} — {cls['libelle']}), confiance {niv} ({conf:.0f}/100)")

    contributions.sort(key=lambda c: (c.source != "zonage", -abs(sum(v for k, v in c.points.items() if k in SOUS_SCORES))))
    return ScoreUrba(
        profil=profil, version_regles=rs.version, score_global=g, score_avant_veto=round(g_avant, 1),
        classe=cls["code"], libelle_classe=cls["libelle"], sous_scores=sous,
        confiance=Confiance(valeur=conf, niveau=niv, motifs=conf_motifs),
        vetos=vetos, contributions=contributions, points_a_verifier=points,
        interpretations=interpretations, resume=resume,
    )
