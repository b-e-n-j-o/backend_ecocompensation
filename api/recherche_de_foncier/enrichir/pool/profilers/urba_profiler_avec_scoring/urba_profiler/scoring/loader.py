"""
loader.py — Chargement et validation stricte du référentiel de règles YAML.

Toute erreur (id dupliqué, regex invalide, sous-score inconnu, raffinement vers
une règle inexistante...) lève une RulesError explicite : un urbaniste qui
modifie le YAML voit immédiatement ce qui ne va pas.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Pattern, Tuple

import yaml

DEFAULT_RULES_PATH = Path(__file__).with_name("regles_compensation.yaml")

SOUS_SCORES = ("perennite", "compatibilite", "synergie", "maitrise")
EFFET_KEYS = SOUS_SCORES + ("confiance",)
FAMILLES = ("prescription", "information", "servitude")
NIVEAUX_VETO = ("bloquant", "majeur")


class RulesError(ValueError):
    pass


def normalize_text(s: Any) -> str:
    """minuscules, sans accents, apostrophes unifiées, espaces compactés."""
    if s is None:
        return ""
    s = str(s).replace("’", "'").replace("`", "'")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.lower()).strip()


def _compile(patterns: List[str], where: str) -> List[Pattern]:
    out = []
    for p in patterns or []:
        try:
            out.append(re.compile(p, re.IGNORECASE))
        except re.error as e:
            raise RulesError(f"{where} : regex invalide {p!r} ({e})")
    return out


@dataclass
class Veto:
    seuil_pct: float
    niveau: str
    motif: str
    profils: Optional[List[str]] = None  # None = tous


@dataclass
class Rule:
    id: str
    label: str
    familles: List[str]
    groupe: str
    effets: Dict[str, float]
    profils: Dict[str, Dict[str, float]]
    codes: List[str] = field(default_factory=list)
    suptypes: List[str] = field(default_factory=list)
    suptypes_prefixe: List[str] = field(default_factory=list)
    mots_cles: List[Pattern] = field(default_factory=list)
    raffine: List[str] = field(default_factory=list)
    mots_cles_raffinement: List[Pattern] = field(default_factory=list)
    generique: bool = False
    portee: str = "surface"
    veto: List[Veto] = field(default_factory=list)
    point_a_verifier: Optional[str] = None
    justification: str = ""
    references: List[str] = field(default_factory=list)
    a_confirmer: bool = False
    question_expert: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def effets_pour(self, profil: str) -> Dict[str, float]:
        return dict(self.profils.get(profil, self.effets))

    @property
    def neutre(self) -> bool:
        return not self.effets and not any(self.profils.values())


@dataclass
class SecteurRule:
    id: str
    label: str
    types: List[str]
    libelle_regex: Optional[Pattern]
    mots_cles: List[Pattern]
    effets: Dict[str, float]
    profils: Dict[str, Dict[str, float]]
    veto: List[Veto]
    point_a_verifier: Optional[str]
    justification: str
    a_confirmer: bool = False
    question_expert: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def effets_pour(self, profil: str) -> Dict[str, float]:
        return dict(self.profils.get(profil, self.effets))


@dataclass
class RuleSet:
    version: str
    statut: str
    profils: Dict[str, str]
    sous_scores: Dict[str, str]
    ponderation: Dict[str, float]
    base: Dict[str, float]
    fiab_mult: Dict[str, float]
    veto_si_fiabilite_basse: bool
    facteurs: Dict[str, float]
    plafonds_veto: Dict[str, float]
    classes: List[Dict[str, Any]]
    confiance: Dict[str, Any]
    zonage_base: Dict[str, Dict[str, Any]]
    zonage_veto: List[Dict[str, Any]]
    zonage_destinations: Dict[str, Dict[str, Any]]
    secteurs: List[SecteurRule]
    voisinage: Dict[str, Dict[str, Any]]
    regles: List[Rule]
    by_id: Dict[str, Rule] = field(default_factory=dict)
    # index (famille, code exact) et (famille, "TT-*")
    by_code: Dict[Tuple[str, str], Rule] = field(default_factory=dict)
    by_suptype: Dict[str, Rule] = field(default_factory=dict)
    by_suptype_prefix: List[Tuple[str, Rule]] = field(default_factory=list)
    raffinements: Dict[str, List[Rule]] = field(default_factory=dict)
    groupes_compatibles: set = field(default_factory=set)
    source: str = ""

    def compatibles(self, g1: str, g2: str) -> bool:
        return g1 == g2 or "neutre" in (g1, g2) or frozenset((g1, g2)) in self.groupes_compatibles


def _check_effets(effets: Dict[str, Any], where: str) -> Dict[str, float]:
    if effets is None:
        return {}
    if not isinstance(effets, dict):
        raise RulesError(f"{where} : 'effets' doit être un dictionnaire")
    out = {}
    for k, v in effets.items():
        if k not in EFFET_KEYS:
            raise RulesError(f"{where} : sous-score inconnu {k!r} (attendus : {EFFET_KEYS})")
        if not isinstance(v, (int, float)):
            raise RulesError(f"{where} : effet {k} non numérique")
        out[k] = float(v)
    return out


def _vetos(raw: Any, where: str, profils: Dict[str, str]) -> List[Veto]:
    out = []
    for v in raw or []:
        if v.get("niveau") not in NIVEAUX_VETO:
            raise RulesError(f"{where} : niveau de veto invalide {v.get('niveau')!r}")
        pf = v.get("profils")
        for p in pf or []:
            if p not in profils:
                raise RulesError(f"{where} : profil de veto inconnu {p!r}")
        out.append(Veto(seuil_pct=float(v["seuil_pct"]), niveau=v["niveau"], motif=v.get("motif", ""), profils=pf))
    return out


def _profils_effets(raw: Any, where: str, profils: Dict[str, str]) -> Dict[str, Dict[str, float]]:
    out = {}
    for p, eff in (raw or {}).items():
        if p not in profils:
            raise RulesError(f"{where} : profil inconnu {p!r}")
        out[p] = _check_effets(eff, f"{where}.profils.{p}")
    return out


def load_rules(path: Optional[str] = None) -> RuleSet:
    p = Path(path) if path else DEFAULT_RULES_PATH
    with open(p, encoding="utf-8") as f:
        d = yaml.safe_load(f)

    profils = d["profils"]
    if "generique" not in profils:
        raise RulesError("Le profil 'generique' est obligatoire")
    pond = d["ponderation_globale"]
    if set(pond) != set(SOUS_SCORES):
        raise RulesError(f"ponderation_globale doit couvrir exactement {SOUS_SCORES}")
    if abs(sum(pond.values()) - 1.0) > 1e-6:
        raise RulesError(f"ponderation_globale doit sommer à 1 (actuel {sum(pond.values())})")

    z = d["zonage"]
    secteurs = []
    seen_sect = set()
    for s in z.get("secteurs", []):
        w = f"secteur {s.get('id')}"
        if s["id"] in seen_sect:
            raise RulesError(f"{w} : id dupliqué")
        seen_sect.add(s["id"])
        rx = s.get("libelle_regex")
        secteurs.append(
            SecteurRule(
                id=s["id"], label=s["label"], types=s.get("types", []),
                libelle_regex=_compile([rx], w)[0] if rx else None,
                mots_cles=_compile(s.get("mots_cles", []), w),
                effets=_check_effets(s.get("effets"), w),
                profils=_profils_effets(s.get("profils"), w, profils),
                veto=_vetos(s.get("veto"), w, profils),
                point_a_verifier=s.get("point_a_verifier"),
                justification=s.get("justification", ""),
                a_confirmer=bool(s.get("a_confirmer")),
                question_expert=s.get("question_expert"),
                raw=s,
            )
        )

    rules: List[Rule] = []
    by_id: Dict[str, Rule] = {}
    for r in d["regles"]:
        w = f"règle {r.get('id')}"
        if not r.get("id"):
            raise RulesError("règle sans id")
        if r["id"] in by_id:
            raise RulesError(f"{w} : id dupliqué")
        fam = r.get("familles") or []
        for f_ in fam:
            if f_ not in FAMILLES:
                raise RulesError(f"{w} : famille inconnue {f_!r}")
        if r.get("portee", "surface") not in ("surface", "presence"):
            raise RulesError(f"{w} : portee invalide")
        rule = Rule(
            id=r["id"], label=r["label"], familles=fam, groupe=r.get("groupe", "neutre"),
            effets=_check_effets(r.get("effets"), w),
            profils=_profils_effets(r.get("profils"), w, profils),
            codes=[str(c) for c in r.get("codes", [])],
            suptypes=[str(c).lower() for c in r.get("suptypes", [])],
            suptypes_prefixe=[str(c).lower() for c in r.get("suptypes_prefixe", [])],
            mots_cles=_compile(r.get("mots_cles", []), w),
            raffine=r.get("raffine", []),
            mots_cles_raffinement=_compile(r.get("mots_cles_raffinement", []), w),
            generique=bool(r.get("generique")),
            portee=r.get("portee", "surface"),
            veto=_vetos(r.get("veto"), w, profils),
            point_a_verifier=r.get("point_a_verifier"),
            justification=r.get("justification", ""),
            references=r.get("references", []),
            a_confirmer=bool(r.get("a_confirmer")),
            question_expert=r.get("question_expert"),
            raw=r,
        )
        if rule.raffine and not rule.mots_cles_raffinement:
            raise RulesError(f"{w} : 'raffine' exige 'mots_cles_raffinement'")
        for c in rule.codes:
            if not re.fullmatch(r"\d{2}-(\d{2}|\*)", c):
                raise RulesError(f"{w} : code mal formé {c!r} (attendu 'TT-SS' ou 'TT-*')")
        rules.append(rule)
        by_id[rule.id] = rule

    rs = RuleSet(
        version=str(d.get("version")), statut=d.get("statut", ""), profils=profils,
        sous_scores=d.get("sous_scores", {}), ponderation=pond, base=d["base"],
        fiab_mult=d["fiabilite"]["multiplicateur"],
        veto_si_fiabilite_basse=bool(d["fiabilite"].get("veto_si_fiabilite_basse", False)),
        facteurs=d["facteurs"], plafonds_veto=d["plafonds_veto"], classes=sorted(d["classes"], key=lambda c: -c["min"]),
        confiance=d["confiance"], zonage_base=z["base_perennite"], zonage_veto=z.get("veto", []),
        zonage_destinations=z.get("destinations", {}), secteurs=secteurs, voisinage=d.get("voisinage", {}),
        regles=rules, by_id=by_id, source=str(p),
        groupes_compatibles={frozenset(pair) for pair in d.get("groupes_compatibles", [])},
    )

    for r in rules:
        for fam in r.familles:
            for c in r.codes:
                key = (fam, c)
                if key in rs.by_code:
                    raise RulesError(f"code {c} ({fam}) attribué à deux règles : {rs.by_code[key].id} et {r.id}")
                rs.by_code[key] = r
        for s in r.suptypes:
            if s in rs.by_suptype:
                raise RulesError(f"suptype {s} attribué à deux règles : {rs.by_suptype[s].id} et {r.id}")
            rs.by_suptype[s] = r
        for pfx in r.suptypes_prefixe:
            rs.by_suptype_prefix.append((pfx, r))
        for target in r.raffine:
            if target not in by_id:
                raise RulesError(f"règle {r.id} : raffine une règle inexistante {target!r}")
            rs.raffinements.setdefault(target, []).append(r)
    # préfixes les plus longs d'abord ("ac" avant "a")
    rs.by_suptype_prefix.sort(key=lambda t: -len(t[0]))

    for k, v in rs.zonage_base.items():
        if "valeur" not in v:
            raise RulesError(f"zonage.base_perennite.{k} : 'valeur' manquante")
    return rs


@lru_cache(maxsize=4)
def get_rules(path: Optional[str] = None) -> RuleSet:
    return load_rules(path)
