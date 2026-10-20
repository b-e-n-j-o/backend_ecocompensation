# -*- coding: utf-8 -*-
"""
Moteur de scoring DÉTERMINISTE — dureté foncière V4.

Principes :
  1. Le score est calculé à 100 % en Python à partir des sorties normalisées
     des sources. Aucun LLM dans la boucle → reproductible au point près,
     classement fiable sur un pool de 50 PM (cf. doc_scoring_deterministe.md).
  2. Chaque point est TRAÇABLE : un `Facteur` (axe, code de règle, points,
     libellé, source, preuve). L'UI et la narration LLM consomment ces facteurs.
  3. Le guide méthodologique (4 axes /100 + surcharges) est conservé comme
     référence métier ; les écarts sont explicites et marqués `ext_v4`.
  4. Chaque axe porte une confiance ; le résultat global a un indice de
     confiance 0-100 : un 45 « confiance faible » ne se lit pas comme un 45 solide.
Fonctions pures : aucun appel réseau ici.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from . import config
from .referentiels import categories_n2, libelle_forme_juridique


# ---------------------------------------------------------------------------
# Modèle de sortie
# ---------------------------------------------------------------------------
@dataclass
class Facteur:
    axe: str            # axe1..axe4 | surcharge | signal
    code: str           # identifiant stable de la règle
    points: int
    libelle: str
    source: str         # annuaire | bodacc | dvf | rpg | gpu | parcelles | regle | input
    preuve: Optional[dict] = None


@dataclass
class Axe:
    score: int
    max: int
    note: str
    confiance: str = "moyenne"   # haute | moyenne | faible
    facteurs: list = field(default_factory=list)


@dataclass
class ResultatScore:
    axes: dict
    surcharges: list
    score_base: int
    surcharge_totale: int
    score_final: int
    niveau_durete: str
    indice_confiance: int
    confiance: str
    recommandations: list
    signaux: list
    avertissements: list
    parcelles: list
    scoring_version: str = config.SCORING_VERSION

    def to_dict(self) -> dict:
        return asdict(self)

    def detail_axes_compat(self) -> dict:
        """Format `detail_axes` V3 (front + table cache inchangés)."""
        d = {}
        for k in ("axe1", "axe2", "axe3", "axe4"):
            d[k] = self.axes[k].score if isinstance(self.axes[k], Axe) else self.axes[k]["score"]
            d[f"{k}_note"] = self.axes[k].note if isinstance(self.axes[k], Axe) else self.axes[k]["note"]
        d["surcharges"] = self.surcharge_totale
        d["surcharges_note"] = " | ".join(f"{f.libelle} ({f.points:+d})" for f in self.surcharges) or \
            "Aucune surcharge applicable"
        return d


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _sans_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn").upper()


def _code4(profil: dict) -> str:
    return str(profil.get("nature_juridique") or "").zfill(4) if profil.get("nature_juridique") else ""


def est_public(code4: str) -> bool:
    return code4[:1] == "7" or code4[:2] == "41"


FAMILLE_AGRICOLE_EXPLOITANTE = {"6533", "6597", "6598", "6316", "6317"}   # GAEC, SCEA, EARL, CUMA, coop
FAMILLE_GFA = {"6534", "6535", "6538"}                                    # GFA, GAF, GFR


def est_exploitant_agricole(profil: dict) -> bool:
    return _code4(profil) in FAMILLE_AGRICOLE_EXPLOITANTE or str(profil.get("naf") or "").startswith("01.")


def bareme_axe4(annees: float) -> tuple[int, str]:
    for borne, pts, lib in config.AXE4_BAREME:
        if annees < borne:
            return pts, lib
    return config.AXE4_BAREME[-1][1], config.AXE4_BAREME[-1][2]


def niveau(score: int) -> str:
    for borne, lib in config.NIVEAUX:
        if score <= borne:
            return lib
    return config.NIVEAUX[-1][1]


# ---------------------------------------------------------------------------
# AXE 1 — Nature de la personne morale (/40)
# ---------------------------------------------------------------------------
RE_STRUCTURE_ENV = re.compile(
    r"CONSERVATOIRE D.?ESPACES NATURELS|CONSERVATOIRE DU LITTORAL|CONSERVATOIRE REGIONAL"
    r"|\bCEN\b|PARC NATUREL|RESERVE NATURELLE|LIGUE POUR LA PROTECTION DES OISEAUX|\bLPO\b"
    r"|OFFICE NATIONAL DES FORETS|OFFICE FRANCAIS DE LA BIODIVERSITE"
    r"|FEDERATION (DEPARTEMENTALE |REGIONALE |NATIONALE )?DES CHASSEURS"
    r"|FEDERATION .*PECHE ET (DE )?PROTECTION DU MILIEU AQUATIQUE"
)

# Surcharges niveau 4 chiffres. Corrigé vs V3 : 6515/6516 n'existent pas dans la
# nomenclature INSEE (supprimés) ; ajout HLM SA (guide : 30), SEM aménageuses (18).
AXE1_CODE4: dict[str, tuple[int, str]] = {
    "6540": (10, "SCI — structure patrimoniale, cession sous accord des associés"),
    "6541": (12, "SCI de construction-vente — logique d'opération, vente si projet abandonné"),
    "6544": (12, "SCI d'accession progressive"),
    "6539": (12, "Société civile foncière"),
    "6521": (14, "SCPI — logique financière, vente si rendement atteint"),
    "6534": (14, "GFA — attachement patrimonial fort, terres louées à bail long"),
    "6535": (14, "Groupement agricole foncier"),
    "6538": (16, "Groupement foncier et rural"),
    "6533": (16, "GAEC — outil de travail des exploitants, décision collégiale"),
    "6597": (16, "SCEA — attachement à l'outil de travail"),
    "6598": (16, "EARL — exploitation agricole"),
    "6536": (22, "Groupement forestier — patrimoine de long terme, vente rare"),
    "6537": (20, "Groupement pastoral — usage collectif"),
    "7210": (37, "Commune — délibération du conseil municipal"),
    "7220": (37, "Département — délibération"),
    "7230": (37, "Région — délibération"),
    "7343": (35, "Communauté urbaine — délibération communautaire"),
    "7344": (35, "Métropole — délibération communautaire"),
    "7346": (35, "Communauté de communes — délibération communautaire"),
    "7348": (35, "Communauté d'agglomération — délibération communautaire"),
    "7364": (33, "Établissement d'hospitalisation — patrimoine affecté"),
    "7331": (33, "EPLE — patrimoine affecté à l'enseignement"),
    "7371": (30, "OPH — cessions encadrées (CCH)"),
    "5546": (30, "SA d'HLM — cessions encadrées (CCH)"),
    "5646": (30, "SA d'HLM — cessions encadrées (CCH)"),
    "5430": (20, "SAFER — opérateur foncier, peut céder à des projets environnementaux"),
    "5530": (20, "SAFER — opérateur foncier"),
    "5630": (20, "SAFER — opérateur foncier"),
    "5415": (18, "SEM — logique d'aménageur"),
    "5515": (18, "SEM — logique d'aménageur"),
    "5615": (18, "SEM — logique d'aménageur"),
    "9230": (32, "Association reconnue d'utilité publique — patrimoine lié à l'objet social"),
    "9300": (30, "Fondation — approbation de l'autorité de tutelle"),
}

# Ajustements NAF (sociétés commerciales famille 5x uniquement) — ext_v4
AXE1_NAF: list[tuple[str, int, str]] = [
    ("41.10", 18, "Promoteur immobilier — logique d'opération (guide : promoteurs/aménageurs)"),
    ("68.10Z", 15, "Marchand de biens — logique de rotation (guide : investisseurs)"),
    ("64.30Z", 15, "Fonds / véhicule d'investissement (guide : investisseurs professionnels)"),
    ("66.30Z", 15, "Gestion de fonds (guide : investisseurs professionnels)"),
    ("01.", 16, "Société commerciale agricole (guide : structures agricoles)"),
]


def scorer_axe1(profil: dict) -> Axe:
    code4 = _code4(profil)
    lib = libelle_forme_juridique(code4)
    nom = _sans_accents(profil.get("denomination") or "")
    src = "annuaire" if profil.get("_source") == "annuaire" else "input"
    conf = "haute" if code4 else "faible"

    if RE_STRUCTURE_ENV.search(nom):
        f = Facteur("axe1", "AXE1_STRUCTURE_ENV", 38,
                    "Structure environnementale (CEN, PNR, Conservatoire, ONF…) — partenaire plutôt que vendeur",
                    "regle", {"denomination": profil.get("denomination")})
        return Axe(38, 40, f.libelle, "moyenne", [f])

    if code4 in AXE1_CODE4:
        pts, raison = AXE1_CODE4[code4]
        raison = raison.split(" — ", 1)[-1]          # le libellé INSEE est déjà en tête
        f = Facteur("axe1", f"AXE1_CODE4_{code4}", pts, f"{lib} — {raison}", src, {"nature_juridique": code4})
        return Axe(pts, 40, f.libelle, conf, [f])

    if code4[:1] == "5":
        naf = str(profil.get("naf") or "")
        for prefixe, pts, raison in AXE1_NAF:
            if naf.startswith(prefixe):
                f = Facteur("axe1", f"AXE1_NAF_{prefixe}", pts, f"{lib} — {raison}", src,
                            {"naf": naf, "nature_juridique": code4})
                return Axe(pts, 40, f.libelle, conf, [f])
        if profil.get("categorie_entreprise") in ("GE", "ETI"):
            f = Facteur("axe1", "AXE1_GRANDE_ENTREPRISE", 20,
                        f"{lib} — {profil['categorie_entreprise']} (guide : grandes entreprises)", src,
                        {"categorie_entreprise": profil["categorie_entreprise"]})
            return Axe(20, 40, f.libelle, conf, [f])

    n2 = categories_n2().get(code4[:2]) if code4 else None
    if n2:
        f = Facteur("axe1", f"AXE1_N2_{code4[:2]}", n2["score_axe1"], f"{lib} — {n2['note']}", src,
                    {"nature_juridique": code4})
        return Axe(n2["score_axe1"], 40, f.libelle, conf, [f])

    f = Facteur("axe1", "AXE1_INCONNU", 20, "Forme juridique inconnue — score neutre", "regle")
    return Axe(20, 40, f.libelle, "faible", [f])


# ---------------------------------------------------------------------------
# AXE 2 — Gouvernance et complexité décisionnelle (/25)
# ---------------------------------------------------------------------------
def scorer_axe2(profil: dict, droits: dict[str, dict]) -> Axe:
    code4 = _code4(profil)
    decideurs = profil.get("decideurs") or []
    facteurs: list[Facteur] = []
    conf = "moyenne"

    co_titulaires = {c["siren"] for d in droits.values() for c in d.get("co_titulaires_pm", [])}
    if code4 == "9110":
        base = Facteur("axe2", "AXE2_COPROPRIETE", 25, "Syndicat de copropriété — décision en AG", "annuaire")
        conf = "haute"
    elif co_titulaires:
        base = Facteur("axe2", "AXE2_INDIVISION_PM", 22,
                       f"Parcelle(s) détenue(s) avec {len(co_titulaires)} autre(s) PM — accord de tous les titulaires",
                       "parcelles", {"co_titulaires": sorted(co_titulaires)[:10]})
        conf = "haute"
    elif est_public(code4):
        base = Facteur("axe2", "AXE2_PUBLIC", 20, "Personne publique — décision délibérative", "annuaire")
        conf = "haute"
    elif code4[:2] in ("55", "56"):
        base = Facteur("axe2", "AXE2_SA", 18, "Société anonyme — conseil d'administration / directoire", "annuaire")
        conf = "haute"
    elif code4[:2] in ("92", "93"):
        base = Facteur("axe2", "AXE2_ASSO_FONDATION", 18,
                       "Association/fondation — décision du CA ou de l'AG, statuts à vérifier", "annuaire")
    elif code4[:2] == "65":
        associes = [d for d in decideurs if "associ" in d["qualite"].lower()]
        gerants = [d for d in decideurs if "associ" not in d["qualite"].lower()]
        if associes:
            n = len(associes)
            pts = 2 if n == 1 else 15 if n <= 5 else 20
            base = Facteur("axe2", "AXE2_SC_ASSOCIES", pts,
                           f"Société civile : {n} associé(s) identifié(s) au RNE", "annuaire", {"nb_associes": n})
            conf = "haute"
        else:
            n = len(gerants)
            pts = 10 if n <= 1 else 15 if n <= 5 else 20
            base = Facteur("axe2", "AXE2_SC_GERANTS", pts,
                           f"Société civile : {n} gérant(s), associés NON publiés — "
                           "statuts (RNE/INPI) nécessaires pour confirmer", "annuaire", {"nb_gerants": n})
            conf = "faible"
    else:
        n = len(decideurs)
        if n == 0:
            base = Facteur("axe2", "AXE2_INCONNU", 10, "Décideurs inconnus — score neutre", "regle")
            conf = "faible"
        elif n == 1:
            unique = any(re.search(r"unique|seul", d["qualite"], re.I) for d in decideurs)
            base = Facteur("axe2", "AXE2_UNIPERSONNEL" if unique else "AXE2_1_DIRIGEANT", 2 if unique else 4,
                           "Associé unique — décision immédiate" if unique else "Un seul dirigeant — décision rapide",
                           "annuaire")
        elif n <= 6:
            base = Facteur("axe2", "AXE2_PLURALITE", 7, f"{n} dirigeants — pluralité d'associés (guide : 7)",
                           "annuaire", {"nb_dirigeants": n})
        else:
            base = Facteur("axe2", "AXE2_GOUVERNANCE_LARGE", 12, f"{n} dirigeants — gouvernance étendue",
                           "annuaire", {"nb_dirigeants": n})
    facteurs.append(base)
    score = base.points

    if any(d.get("demembrement") for d in droits.values()):
        facteurs.append(Facteur("axe2", "AXE2_DEMEMBREMENT", 3,
                                "Démembrement de propriété (usufruit / nue-propriété) — deux accords requis",
                                "parcelles"))
        score += 3
    if any(d["type"] == "pm" for d in decideurs) and not est_public(code4):
        facteurs.append(Facteur("axe2", "AXE2_DIRIGEANT_PM", 2,
                                "Dirigeant personne morale — chaîne de décision indirecte", "annuaire",
                                {"dirigeants_pm": [d.get("denomination") for d in decideurs if d["type"] == "pm"]}))
        score += 2
    score = min(25, score)
    return Axe(score, 25, " | ".join(f.libelle for f in facteurs), conf, facteurs)


# ---------------------------------------------------------------------------
# AXE 3 — Situation financière et statut (/20)
# ---------------------------------------------------------------------------
def scorer_axe3(profil: dict, bodacc: dict) -> Axe:
    proc = (bodacc or {}).get("procedure") or {}
    code4 = _code4(profil)

    def axe(pts: int, code: str, lib: str, src: str, conf: str, preuve: Optional[dict] = None) -> Axe:
        f = Facteur("axe3", code, pts, lib, src, preuve)
        return Axe(pts, 20, lib, conf, [f])

    if proc.get("statut") == "en_cours":
        t = proc.get("type")
        pts, lib = {"liquidation": (0, "Liquidation judiciaire en cours — réalisation des actifs obligatoire"),
                    "cession": (2, "Plan de cession en cours — actifs cédés sous contrôle du tribunal"),
                    "redressement": (3, "Redressement judiciaire en cours — cession d'actifs probable"),
                    "sauvegarde": (5, "Procédure de sauvegarde en cours")}.get(t, (4, "Procédure collective en cours"))
        return axe(pts, f"AXE3_PROC_{(t or 'NC').upper()}", f"{lib} ({proc.get('date')})", "bodacc", "haute", proc)
    if proc.get("statut") == "plan":
        return axe(8, "AXE3_PLAN", f"Sous plan de {proc.get('type')} depuis {proc.get('date')} — "
                   "cessions d'actifs encadrées par le tribunal", "bodacc", "haute", proc)
    if (bodacc or {}).get("dissolution"):
        d = bodacc["dissolution"]
        return axe(3, "AXE3_DISSOLUTION", f"Dissolution / liquidation amiable publiée ({d['date']}) — actifs à réaliser",
                   "bodacc", "haute", d)
    if profil.get("etat_administratif") == "C" or (bodacc or {}).get("radiee"):
        return axe(3, "AXE3_CESSEE", "Personne morale cessée / radiée — actif dormant", "annuaire", "haute")
    if est_public(code4):
        return axe(18, "AXE3_PUBLIC", "Personne publique — pas de pression financière à vendre", "regle", "haute")

    fins = [f for f in profil.get("finances") or [] if f.get("ca") is not None or f.get("resultat_net") is not None]
    if fins:
        last = fins[0]
        rn, ca = last.get("resultat_net"), last.get("ca")
        pertes = [f for f in fins[:2] if (f.get("resultat_net") or 0) < 0]
        if len(pertes) == 2:
            return axe(6, "AXE3_PERTES_REPETEES", f"Deux exercices déficitaires consécutifs "
                       f"({fins[1]['annee']}-{last['annee']}) — pression financière", "annuaire", "haute", last)
        if rn is not None and rn < 0:
            return axe(8, "AXE3_DEFICITAIRE", f"Exercice {last['annee']} déficitaire "
                       f"(résultat {rn:,.0f} €) — société active en perte".replace(",", " "), "annuaire", "haute", last)
        if (ca or 0) >= 10_000_000 or (rn or 0) >= 1_000_000:
            return axe(20, "AXE3_FORTE_RENTABILITE", f"Forte surface financière ({last['annee']} : CA "
                       f"{(ca or 0):,.0f} €, résultat {(rn or 0):,.0f} €)".replace(",", " "), "annuaire", "haute", last)
        return axe(15, "AXE3_EQUILIBRE", f"Comptes à l'équilibre ({last['annee']})", "annuaire", "haute", last)
    if profil.get("categorie_entreprise") in ("GE", "ETI"):
        return axe(18, "AXE3_GE_ETI", f"{profil['categorie_entreprise']} sans comptes publiés — surface financière présumée",
                   "annuaire", "moyenne")
    return axe(12, "AXE3_NEUTRE", "Active, aucun compte publié (fréquent pour SCI/associations) — score neutre",
               "regle", "faible")


# ---------------------------------------------------------------------------
# AXE 4 — Comportement patrimonial et ancienneté (/15)
# ---------------------------------------------------------------------------
def scorer_detention(det: dict) -> tuple[Optional[int], str, str]:
    """(points, libellé, fiabilité) pour une parcelle."""
    st = det.get("statut")
    lo, hi = det.get("annees_min"), det.get("annees_max")
    if st == "mutation_observee" and lo is not None:
        pts, lib = bareme_axe4(lo)
        return pts, f"{lib} — {det.get('signal')}", "moyenne"
    if st in ("anterieure_fenetre", "apport_probable") and lo is not None:
        if hi is None:
            pts, lib = bareme_axe4(max(lo, 5))
            return pts, f"Détention ≥ {lo:.0f} ans (aucune mutation visible) — {lib}", "faible"
        p_lo, _ = bareme_axe4(lo)
        p_hi, _ = bareme_axe4(hi)
        if p_lo == p_hi:
            pts, lib = bareme_axe4(lo)
            return pts, f"Détention entre {lo:.0f} et {hi:.0f} ans — {lib}", "faible"
        mid = (lo + hi) / 2
        pts, lib = bareme_axe4(mid)
        return pts, f"Détention entre {lo:.0f} et {hi:.0f} ans (point médian {mid:.0f} ans) — {lib}", "faible"
    return None, det.get("signal") or "Ancienneté inconnue", "faible"


def scorer_axe4(parcelles: list[dict], bodacc: dict) -> Axe:
    if (bodacc or {}).get("dissolution"):
        f = Facteur("axe4", "AXE4_DISSOLUTION", 0, "PM en dissolution / liquidation amiable (guide : 0/15)",
                    "bodacc", bodacc["dissolution"])
        return Axe(0, 15, f.libelle, "haute", [f])
    notes = []
    for p in parcelles:
        pts, lib, fiab = scorer_detention(p.get("detention") or {})
        p["axe4"] = {"points": pts, "libelle": lib, "fiabilite": fiab}
        if pts is not None:
            notes.append((pts, lib, fiab, p["idu"]))
    if not notes:
        f = Facteur("axe4", "AXE4_NEUTRE", 8, "Ancienneté non reconstituable (DVF indisponible) — score neutre", "regle")
        return Axe(8, 15, f.libelle, "faible", [f])
    pts, lib, fiab, idu = max(notes, key=lambda n: n[0])   # pire cas : la parcelle la plus dure fixe le score
    f = Facteur("axe4", "AXE4_DETENTION", pts, f"[{idu}] {lib}", "dvf",
                {"parcelles": [{"idu": n[3], "points": n[0]} for n in notes]})
    return Axe(pts, 15, f.libelle, fiab, [f])


# ---------------------------------------------------------------------------
# Surcharges
# ---------------------------------------------------------------------------
def _poids(parcelles: list[dict]) -> list[float]:
    w = [float(p.get("surface_m2") or 0) for p in parcelles]
    return w if sum(w) > 0 else [1.0] * len(parcelles)


def _pondere(valeurs: list[float], poids: list[float]) -> int:
    return round(sum(v * w for v, w in zip(valeurs, poids)) / sum(poids)) if poids else 0


def surcharge_bail_parcelle(rpg_summary: Optional[dict], profil: dict) -> tuple[int, str, str]:
    """(points, code, libellé) — dépend du propriétaire (corrige V3 qui ignorait ann/sirene)."""
    if not rpg_summary:
        return 0, "BAIL_NC", "RPG indisponible"
    n, run = rpg_summary.get("nb_annees_agricoles", 0), rpg_summary.get("max_consecutif", 0)
    if n == 0:
        return 0, "BAIL_AUCUN", "Aucune déclaration PAC significative"
    if est_exploitant_agricole(profil):
        return (config.SURCHARGE_FAIRE_VALOIR_DIRECT, "FAIRE_VALOIR_DIRECT",
                f"PAC déclarée {n} an(s) — la PM est exploitante (faire-valoir direct probable) : "
                "pas de bail, mais terre support de l'outil de travail")
    if _code4(profil) in FAMILLE_GFA:
        return (config.SURCHARGE_BAIL_CERTAIN, "BAIL_GFA",
                f"GFA dont la terre est déclarée à la PAC ({n} an(s)) — bail rural à long terme quasi systématique")
    if run >= 5:
        return (config.SURCHARGE_BAIL_CERTAIN, "BAIL_CERTAIN",
                f"PAC déclarée {run} années consécutives par un tiers — bail rural très probable")
    if run >= 3 or (rpg_summary.get("part_annees_agricoles") or 0) > 0.5:
        return (config.SURCHARGE_BAIL_PROBABLE, "BAIL_PROBABLE",
                f"PAC déclarée {n} an(s) (max {run} consécutifs) — bail rural ou convention probable")
    return config.SURCHARGE_BAIL_PONCTUEL, "BAIL_PONCTUEL", f"Occupation agricole ponctuelle ({n} an(s))"


def surcharge_zonage_parcelle(zonage: Optional[dict]) -> tuple[Optional[float], str]:
    if not zonage or zonage.get("statut") != "ok":
        return None, "zonage indisponible"
    pts = sum(part * config.SURCHARGE_ZONAGE.get(classe, 0) for classe, part in (zonage.get("parts") or {}).items())
    return pts, f"zone dominante {zonage.get('classe_dominante')} ({', '.join(f'{k} {v:.0%}' for k, v in zonage['parts'].items())})"


def calculer_surcharges(parcelles: list[dict], profil: dict, options: dict) -> tuple[list[Facteur], bool]:
    facteurs: list[Facteur] = []
    poids = _poids(parcelles)
    force_100 = False

    # Domaine public : jamais déduit automatiquement (une parcelle cadastrée d'une
    # commune relève le plus souvent du domaine PRIVÉ) — uniquement sur confirmation.
    confirmes = set(options.get("domaine_public_confirme") or [])
    if confirmes & {p["idu"] for p in parcelles}:
        facteurs.append(Facteur("surcharge", "DOMAINE_PUBLIC", 0,
                                "Domaine public confirmé — inaliénable, score forcé à 100", "input",
                                {"idus": sorted(confirmes)}))
        force_100 = True

    if options.get("avec_rpg", True):
        res = [surcharge_bail_parcelle(p.get("rpg"), profil) for p in parcelles]
        for p, r in zip(parcelles, res):
            p["bail"] = {"points": r[0], "code": r[1], "libelle": r[2]}
        pts = _pondere([r[0] for r in res], poids)
        if pts:
            dominante = max(res, key=lambda r: r[0])
            detail = "" if len(parcelles) == 1 else " (moyenne pondérée par la surface)"
            facteurs.append(Facteur("surcharge", dominante[1], pts, dominante[2] + detail, "rpg",
                                    {"par_parcelle": [{"idu": p["idu"], **p["bail"]} for p in parcelles]}))

    droits_tiers = [1.0 if (p.get("droits") or {}).get("droit_reel_tiers") else 0.0 for p in parcelles]
    if any(droits_tiers):
        pts = _pondere([v * config.SURCHARGE_DROIT_REEL_TIERS for v in droits_tiers], poids)
        facteurs.append(Facteur("surcharge", "DROIT_REEL_TIERS", pts,
                                "Emphytéose / bail à construction au profit d'un tiers (fichier PM)", "parcelles"))

    if config.SURCHARGE_ZONAGE_ACTIVE and options.get("avec_gpu", True):
        vals = [surcharge_zonage_parcelle(p.get("zonage")) for p in parcelles]
        connus = [(v, w, p) for (v, _), w, p in zip(vals, poids, parcelles) if v is not None]
        if connus:
            pts = round(sum(v * w for v, w, _ in connus) / sum(w for _, w, _ in connus))
            libs = "; ".join(f"{p['idu']} : {lib}" for (v, lib), p in zip(vals, parcelles) if v is not None)
            if pts:
                facteurs.append(Facteur("surcharge", "ZONAGE_CONSTRUCTIBLE", pts,
                                        "Zonage constructible (U/AU) — attente de plus-value du propriétaire [ext_v4]",
                                        "gpu", {"detail": libs}))
    return facteurs, force_100


# ---------------------------------------------------------------------------
# Signaux qualitatifs (non scorés, pour la narration et l'UI)
# ---------------------------------------------------------------------------
def collecter_signaux(profil: dict, bodacc: dict, parcelles: list[dict], patrimoine: Optional[dict]) -> list[Facteur]:
    s: list[Facteur] = []
    age = profil.get("age_max_decideurs_pp")
    if age and age >= 75 and not est_public(_code4(profil)):
        s.append(Facteur("signal", "SIG_DIRIGEANT_AGE", 0,
                         f"Dirigeant le plus âgé : {age} ans — transmission patrimoniale à anticiper (levier possible)",
                         "annuaire"))
    if (bodacc or {}).get("nb_modifications_3ans", 0) >= 2:
        s.append(Facteur("signal", "SIG_MODIFS_RECENTES", 0,
                         f"{bodacc['nb_modifications_3ans']} modifications publiées au BODACC en 3 ans "
                         "(gérance, siège, objet…) — structure en mouvement", "bodacc"))
    if (bodacc or {}).get("procedure", {}).get("statut") == "cloturee":
        s.append(Facteur("signal", "SIG_PROC_CLOTUREE", 0,
                         f"Procédure collective clôturée ({bodacc['procedure']['date']})", "bodacc"))
    if patrimoine and patrimoine.get("nb_parcelles"):
        s.append(Facteur("signal", "SIG_PATRIMOINE", 0,
                         f"Patrimoine connu : {patrimoine['nb_parcelles']} parcelle(s), "
                         f"{patrimoine['surface_totale_ha']} ha sur {patrimoine['nb_communes']} commune(s)",
                         "parcelles"))
    comp = profil.get("complements") or {}
    if comp.get("est_bio"):
        s.append(Facteur("signal", "SIG_BIO", 0, "Exploitation certifiée bio — sensibilité environnementale", "annuaire"))
    if comp.get("est_ess") or comp.get("est_societe_mission"):
        s.append(Facteur("signal", "SIG_ESS", 0, "ESS / société à mission — argumentaire d'intérêt général pertinent",
                         "annuaire"))
    for p in parcelles:
        d = p.get("droits") or {}
        if d.get("pm_non_proprietaire"):
            s.append(Facteur("signal", "SIG_PM_NON_PROPRIETAIRE", 0,
                             f"[{p['idu']}] la PM n'y est pas propriétaire "
                             f"({', '.join(x['libelle'] for x in d.get('droits_pm', []))})", "parcelles"))
        for a in (p.get("detention") or {}).get("avertissements", []):
            if "POSTÉRIEURE" in a:
                s.append(Facteur("signal", "SIG_VENTE_RECENTE", 0, f"[{p['idu']}] {a}", "dvf"))
        rpg = p.get("rpg") or {}
        if rpg and rpg.get("nb_annees_agricoles") and not rpg.get("declaree_dernier_millesime"):
            s.append(Facteur("signal", "SIG_DEPRISE", 0,
                             f"[{p['idu']}] plus déclarée à la PAC depuis {rpg.get('derniere_annee_agricole')} — "
                             "déprise agricole / fin de bail possible (levier)", "rpg"))
    return s


# ---------------------------------------------------------------------------
# Recommandation de mode de maîtrise foncière (déterministe)
# ---------------------------------------------------------------------------
def recommandations(profil: dict, bodacc: dict, parcelles: list[dict], score: int) -> list[str]:
    code4 = _code4(profil)
    rec = []
    proc = (bodacc or {}).get("procedure") or {}
    if proc.get("statut") in ("en_cours", "plan"):
        rec.append("Contacter le mandataire / administrateur judiciaire : les actifs peuvent être cédés "
                   "dans le cadre de la procédure.")
    if (bodacc or {}).get("dissolution"):
        rec.append("Contacter le liquidateur amiable : la société doit réaliser ses actifs.")
    if RE_STRUCTURE_ENV.search(_sans_accents(profil.get("denomination") or "")):
        rec.append("Acteur environnemental : viser une convention de gestion ou une ORE en partenariat "
                   "plutôt qu'une acquisition.")
    elif est_public(code4):
        rec.append("Personne publique : privilégier convention de mise à disposition / ORE / bail emphytéotique "
                   "(délibération nécessaire) ; vérifier l'absence de projet ou de réserve foncière.")
    if any((p.get("bail") or {}).get("points", 0) >= config.SURCHARGE_BAIL_PROBABLE
           and (p.get("bail") or {}).get("code") != "FAIRE_VALOIR_DIRECT" for p in parcelles):
        rec.append("Bail rural probable : négocier avec le propriétaire ET le preneur (indemnité d'éviction / "
                   "résiliation amiable). Une ORE sur un fonds loué exige l'accord préalable du preneur "
                   "(art. L.132-3 C. env.).")
    if code4[:2] == "65" and not any("associ" in d["qualite"].lower() for d in profil.get("decideurs") or []):
        rec.append("Société civile : récupérer les statuts et la liste des associés au RNE (INPI) "
                   "avant approche — clauses d'agrément / majorités de cession.")
    if any((p.get("droits") or {}).get("co_titulaires_pm") for p in parcelles):
        rec.append("Plusieurs titulaires sur la même parcelle : identifier et approcher chacun.")
    if not rec:
        rec.append("Acquisition amiable directe" if score <= 40 else
                   "Acquisition amiable ou ORE selon l'appétence du propriétaire ; argumentaire de valorisation "
                   "pérenne du foncier.")
    return rec


# ---------------------------------------------------------------------------
# Orchestration du score
# ---------------------------------------------------------------------------
def calculer_score(
    profil: dict,
    bodacc: dict,
    parcelles: list[dict],
    patrimoine: Optional[dict] = None,
    options: Optional[dict] = None,
) -> ResultatScore:
    """
    parcelles : [{idu, surface_m2, detention: dict, rpg: summary|None, zonage: dict|None, droits: dict|None}]
    options   : {avec_rpg, avec_gpu, domaine_public_confirme: [idu]}
    """
    options = {"avec_rpg": True, "avec_gpu": True, **(options or {})}
    parcelles = [dict(p) for p in parcelles]
    droits = {p["idu"]: p.get("droits") or {} for p in parcelles}

    a1 = scorer_axe1(profil)
    a2 = scorer_axe2(profil, droits)
    a3 = scorer_axe3(profil, bodacc)
    a4 = scorer_axe4(parcelles, bodacc)
    surch, force_100 = calculer_surcharges(parcelles, profil, options)

    base = a1.score + a2.score + a3.score + a4.score
    tot = sum(f.points for f in surch)
    final = 100 if force_100 else max(0, min(100, base + tot))

    avert = []
    if est_public(_code4(profil)) and not force_100:
        avert.append("Personne publique : vérifier domaine public / privé (non déductible du cadastre) — "
                     "passer domaine_public_confirme si confirmé.")
    avert.append("Non évalués faute de source ouverte : ORE existantes, DIA récentes, ventes récentes "
                 "réalisées par la PM (DV3F/Fichiers fonciers à accès restreint).")
    for p in parcelles:
        for a in (p.get("detention") or {}).get("avertissements", []):
            if "POSTÉRIEURE" in a or "globale" in a:
                avert.append(f"[{p['idu']}] {a}")

    # Indice de confiance (complétude / solidité des données)
    conf = 100
    if profil.get("_source") != "annuaire":
        conf -= 35
    conf -= {"faible": 10, "moyenne": 0, "haute": 0}[a2.confiance]
    conf -= {"faible": 10, "moyenne": 3, "haute": 0}[a3.confiance]
    conf -= {"faible": 10, "moyenne": 3, "haute": 0}[a4.confiance]
    if options["avec_rpg"] and any(p.get("rpg") is None for p in parcelles):
        conf -= 10
    if options["avec_gpu"] and config.SURCHARGE_ZONAGE_ACTIVE and \
            any((p.get("zonage") or {}).get("statut") != "ok" for p in parcelles):
        conf -= 5
    if parcelles and not any((p.get("droits") or {}).get("connu_fichier_pm") for p in parcelles):
        conf -= 5
    conf = max(0, min(100, conf))

    return ResultatScore(
        axes={"axe1": a1, "axe2": a2, "axe3": a3, "axe4": a4},
        surcharges=surch,
        score_base=base,
        surcharge_totale=tot,
        score_final=final,
        niveau_durete=niveau(final),
        indice_confiance=conf,
        confiance="haute" if conf >= 75 else "moyenne" if conf >= 50 else "faible",
        recommandations=recommandations(profil, bodacc, parcelles, final),
        signaux=collecter_signaux(profil, bodacc, parcelles, patrimoine),
        avertissements=avert,
        parcelles=parcelles,
    )
