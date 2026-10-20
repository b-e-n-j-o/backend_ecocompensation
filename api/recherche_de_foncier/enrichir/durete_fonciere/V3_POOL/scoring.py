"""
Orchestrateur — Scoring Dureté Foncière Kerelia
================================================
Pipeline complet pour une personne morale (SIREN) et ses parcelles (IDUs).

Usage :
    python scoring.py --siren 892632365 --idus 86275000D0319
    python scoring.py --siren 892632365 --idus 86275000D0319 86275000D0320 --no-rpg
    python scoring.py --siren 892632365 --idus 86275000D0319 --json

Sources :
    1. Annuaire Entreprises  → Axes 1, 2, 3 base
    2. BODACC                → Axe 3 complet (procédures + capital)
    3. DVF (par parcelle)    → Axe 4 (jdatat reconstitué)
    4. RPG (par parcelle)    → Surcharge bail rural
"""

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional

try:
    from .annuaire import fetch_annuaire, fetch_annuaire_raw, scorer_axe1, scorer_axe2
    from .bodacc import fetch_bodacc, fetch_bodacc_raw, scorer_axe3_complet
    from .dvf import lookup_dvf, lookup_dvf_raw, ResultatDVF, StatutDVF
    from .rpg import decompose_idu as _decompose_idu, fetch_rpg_parcelle, scorer_surcharge_bail_rural
    from .sirene import fetch_sirene, signal_stabilite, est_zero_salarie
except ImportError:
    from annuaire import fetch_annuaire, fetch_annuaire_raw, scorer_axe1, scorer_axe2
    from bodacc import fetch_bodacc, fetch_bodacc_raw, scorer_axe3_complet
    from dvf import lookup_dvf, lookup_dvf_raw, ResultatDVF, StatutDVF
    from rpg import decompose_idu as _decompose_idu, fetch_rpg_parcelle, scorer_surcharge_bail_rural
    from sirene import fetch_sirene, signal_stabilite, est_zero_salarie

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("scoring")


# ---------------------------------------------------------------------------
# Modèles
# ---------------------------------------------------------------------------

@dataclass
class ScoreParcelle:
    idu:              str
    score_axe4:       Optional[int]   = None
    note_axe4:        str             = ""
    surcharge_bail:   int             = 0
    note_bail:        str             = ""
    date_acquisition: Optional[str]   = None
    valeur_acquisition: Optional[float] = None
    fiabilite_jdatat: Optional[str]   = None
    bail_rural_certain: bool          = False
    bail_rural_probable: bool         = False
    cultures_rpg:     list            = field(default_factory=list)
    nb_annees_agricoles: int          = 0
    avertissements:   list            = field(default_factory=list)


@dataclass
class ScoreDurete:
    # Identification
    siren:            str
    denomination:     str             = ""
    forme_juridique:  str             = ""
    statut_pm:        str             = ""
    date_creation_pm: Optional[str]   = None
    capital_social:   Optional[float] = None
    nb_dirigeants:    int             = 0
    dirigeants:       list            = field(default_factory=list)

    # Scores par axe
    axe1:             int             = 0
    note_axe1:        str             = ""
    axe2:             int             = 0
    note_axe2:        str             = ""
    axe3:             int             = 0
    note_axe3:        str             = ""

    # Parcelles
    parcelles:        list            = field(default_factory=list)

    # Score agrégé (pire cas sur les parcelles)
    axe4_max:         Optional[int]   = None
    note_axe4_max:    str             = ""
    surcharge_bail_max: int           = 0
    note_bail_max:    str             = ""

    # Score final
    score_base:       int             = 0    # axe1+axe2+axe3+axe4
    surcharge_totale: int             = 0
    score_final:      int             = 0    # score_base + surcharges (plafonné à 100)

    # Interprétation
    niveau_durete:    str             = ""
    recommandation:   str             = ""
    avertissements:   list            = field(default_factory=list)

    # Sirene INSEE (optionnel)
    sirene:           dict            = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Interprétation score final
# ---------------------------------------------------------------------------

def _interpreter(score: int) -> tuple[str, str]:
    if score <= 20:
        return "Opportunité exceptionnelle 🟢🟢", \
               "Action immédiate — contacter liquidateur / administrateur judiciaire."
    elif score <= 40:
        return "Dureté faible 🟢", \
               "Cible prioritaire — prospection directe, taux de conversion 15-25%."
    elif score <= 60:
        return "Dureté modérée 🟠", \
               "Cible secondaire — négociation 6-18 mois, argumentaire solide requis."
    elif score <= 80:
        return "Dureté forte 🔴", \
               "Veille foncière uniquement — cibler si enjeu écologique exceptionnel."
    else:
        return "Dureté rédhibitoire ⛔", \
               "Exclure du portefeuille — investissement en temps non rentabilisé."


# ---------------------------------------------------------------------------
# Pipeline principal
# ---------------------------------------------------------------------------

def scorer_durete(
    siren:      str,
    idus:       list[str],
    avec_rpg:   bool = True,
    verbose:    bool = False,
) -> ScoreDurete:

    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    result = ScoreDurete(siren=siren)

    # ── 1. Annuaire Entreprises ──────────────────────────────────────────
    log.info(f"=== ÉTAPE 1 : Annuaire Entreprises (SIREN {siren}) ===")
    ann = fetch_annuaire(siren)

    if not ann:
        result.avertissements.append("Annuaire Entreprises : SIREN introuvable")
    else:
        result.denomination     = ann.get("nom_complet", "")
        result.forme_juridique  = ann.get("nature_juridique", "")
        result.statut_pm        = ann.get("etat_administratif", "")
        result.date_creation_pm = ann.get("date_creation", "")
        result.nb_dirigeants    = len(ann.get("dirigeants", []))
        result.dirigeants       = [
            {
                "nom":     d.get("nom", ""),
                "prenom":  d.get("prenoms", d.get("prenom", "")),
                "qualite": d.get("qualite", ""),
            }
            for d in ann.get("dirigeants", [])
        ]

    result.axe1, result.note_axe1 = scorer_axe1(ann) if ann else (20, "Annuaire indisponible")
    result.axe2, result.note_axe2 = scorer_axe2(ann) if ann else (12, "Annuaire indisponible")

    # ── 2. BODACC ────────────────────────────────────────────────────────
    log.info(f"=== ÉTAPE 2 : BODACC ===")
    bod = fetch_bodacc(siren)

    result.capital_social = bod.get("capital_social")
    result.axe3, result.note_axe3 = scorer_axe3_complet(ann or {}, bod)

    # Enrichissement dirigeants depuis BODACC si Annuaire incomplet
    if bod.get("associes_bodacc") and result.nb_dirigeants == 0:
        result.nb_dirigeants = len(bod["associes_bodacc"])
        result.avertissements.append("Dirigeants issus BODACC (Annuaire incomplet)")

    # ── 2bis. Sirene INSEE ───────────────────────────────────────────────
    log.info("=== ÉTAPE 2bis : Sirene INSEE ===")
    sir = fetch_sirene(siren)  # {} si indisponible, jamais d'exception
    result.sirene = sir or {}

    # Affiner Axe 2 si structure instable
    delta_axe2, note_stab = signal_stabilite(result.sirene)
    if delta_axe2 > 0:
        result.axe2 = min(25, result.axe2 + delta_axe2)
        result.note_axe2 += f" | {note_stab}"
    elif result.sirene:
        result.note_axe2 += f" | {note_stab}"

    # ── 3. DVF + RPG par parcelle ────────────────────────────────────────
    log.info(f"=== ÉTAPE 3 : DVF + RPG ({len(idus)} parcelle(s)) ===")

    scores_axe4   = []
    surcharges    = []

    for idu in idus:
        log.info(f"--- Parcelle {idu} ---")
        sp = ScoreParcelle(idu=idu)

        # DVF
        dvf = lookup_dvf(idu, date_creation_pm=result.date_creation_pm)
        sp.score_axe4       = dvf.score_axe4
        sp.note_axe4        = dvf.signal
        sp.date_acquisition = dvf.date_acquisition
        sp.valeur_acquisition = dvf.valeur_acquisition
        sp.fiabilite_jdatat = dvf.fiabilite_jdatat
        sp.avertissements  += dvf.avertissements

        if dvf.score_axe4 is not None:
            scores_axe4.append((dvf.score_axe4, dvf.signal, idu))

        # RPG
        if avec_rpg:
            # Décompose l'IDU → insee / section / numero
            try:
                insee, section, numero = _decompose_idu(idu)
                rpg = fetch_rpg_parcelle(insee, section, numero)
                surcharge, note_bail = scorer_surcharge_bail_rural(
                    rpg,
                    ann or {},
                    sirene=result.sirene,
                )

                sp.surcharge_bail      = surcharge
                sp.note_bail           = note_bail
                sp.bail_rural_certain  = rpg.get("summary", {}).get("bail_rural_certain", False)
                sp.bail_rural_probable = rpg.get("summary", {}).get("bail_rural_probable", False)
                sp.cultures_rpg        = rpg.get("summary", {}).get("codes_cultures", [])
                sp.nb_annees_agricoles = rpg.get("summary", {}).get("nb_annees_agricoles", 0)

                surcharges.append((surcharge, note_bail, idu))
            except Exception as e:
                log.error(f"RPG erreur parcelle {idu} : {e}")
                sp.avertissements.append(f"RPG indisponible : {e}")
        else:
            sp.surcharge_bail = 0
            sp.note_bail = "RPG désactivé"

        result.parcelles.append(sp)

    # ── 4. Agrégation multi-parcelles ────────────────────────────────────
    # Stratégie : on prend le score Axe 4 le plus défavorable (max)
    # et la surcharge bail la plus élevée (une seule parcelle avec bail suffit)

    if scores_axe4:
        worst = max(scores_axe4, key=lambda x: x[0])
        result.axe4_max      = worst[0]
        result.note_axe4_max = f"[{worst[2]}] {worst[1]}"
    else:
        result.axe4_max      = 7   # score neutre si aucun DVF
        result.note_axe4_max = "jdatat non reconstituable via DVF — score neutre"
        result.avertissements.append(
            "Axe 4 non calculé : aucune mutation DVF trouvée sur les parcelles. "
            "Vérifier apports en nature ou acquisitions pré-2019."
        )

    if surcharges:
        worst_bail = max(surcharges, key=lambda x: x[0])
        result.surcharge_bail_max = worst_bail[0]
        result.note_bail_max      = f"[{worst_bail[2]}] {worst_bail[1]}"

    # ── 5. Score final ───────────────────────────────────────────────────
    result.score_base     = result.axe1 + result.axe2 + result.axe3 + (result.axe4_max or 0)
    result.surcharge_totale = result.surcharge_bail_max
    result.score_final    = min(100, result.score_base + result.surcharge_totale)

    result.niveau_durete, result.recommandation = _interpreter(result.score_final)

    log.info(
        f"Score final : {result.axe1}+{result.axe2}+{result.axe3}+"
        f"{result.axe4_max} + surcharge {result.surcharge_totale} = {result.score_final}/100"
    )

    return result


def afficher_rapport(r: ScoreDurete):
    sep  = "=" * 62
    sep2 = "─" * 62

    print(f"\n{sep}")
    print(f"  RAPPORT DURETÉ FONCIÈRE — {r.denomination or r.siren}")
    print(sep)

    # Identité PM
    print(f"\n{'IDENTITÉ DE LA PERSONNE MORALE':─<62}")
    print(f"  SIREN              : {r.siren}")
    print(f"  Dénomination       : {r.denomination}")
    print(f"  Forme juridique    : {r.forme_juridique}")
    print(f"  Statut             : {r.statut_pm}")
    print(f"  Date création      : {r.date_creation_pm or 'NC'}")
    print(f"  Capital social     : {f'{r.capital_social:,.0f} €' if r.capital_social else 'NC'}")
    print(f"  Nb dirigeants      : {r.nb_dirigeants}")
    for d in r.dirigeants:
        print(f"    • {d['qualite']} : {d['prenom']} {d['nom']}")

    # Scores par axe
    print(f"\n{'SCORES PAR AXE':─<62}")
    print(f"  Axe 1 — Nature PM      : {r.axe1:>3}/40  {r.note_axe1}")
    print(f"  Axe 2 — Gouvernance    : {r.axe2:>3}/25  {r.note_axe2}")
    print(f"  Axe 3 — Finances       : {r.axe3:>3}/20  {r.note_axe3}")
    print(f"  Axe 4 — Patrimoine     : {r.axe4_max if r.axe4_max is not None else '?':>3}/15  {r.note_axe4_max}")

    # Parcelles
    print(f"\n{'ANALYSE PAR PARCELLE':─<62}")
    for sp in r.parcelles:
        print(f"\n  [{sp.idu}]")
        print(f"    DVF date acquisition  : {sp.date_acquisition or 'non tracée'}")
        print(f"    DVF valeur            : {f'{sp.valeur_acquisition:,.0f} €' if sp.valeur_acquisition else 'NC'}")
        print(f"    DVF fiabilité jdatat  : {sp.fiabilite_jdatat or 'N/A'}")
        print(f"    DVF signal            : {sp.note_axe4}")
        print(f"    RPG bail rural        : {'CONFIRMÉ ✓' if sp.bail_rural_certain else 'probable' if sp.bail_rural_probable else 'non détecté'}")
        print(f"    RPG cultures          : {', '.join(sp.cultures_rpg) or 'N/A'} ({sp.nb_annees_agricoles} années)")
        print(f"    Surcharge bail        : +{sp.surcharge_bail} pts — {sp.note_bail}")
        if sp.avertissements:
            for a in sp.avertissements:
                print(f"    ⚠ {a}")

    # Score final
    print(f"\n{'SCORE FINAL':─<62}")
    print(f"  Score base  (axe1+2+3+4) : {r.score_base}/100")
    print(f"  Surcharge bail rural      : +{r.surcharge_totale}")
    print(f"  {'─'*40}")
    print(f"  SCORE FINAL               : {r.score_final}/100")
    print(f"  Niveau de dureté          : {r.niveau_durete}")
    print(f"  Recommandation            : {r.recommandation}")

    if r.avertissements:
        print(f"\n{'AVERTISSEMENTS':─<62}")
        for a in r.avertissements:
            print(f"  ⚠ {a}")

    print(f"\n{sep}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Scoring Dureté Foncière — Kerelia"
    )
    parser.add_argument("--siren",    required=True,  help="SIREN de la personne morale")
    parser.add_argument("--idus",     nargs="+",      required=True, help="IDU(s) des parcelles")
    parser.add_argument("--no-rpg",   action="store_true", help="Désactiver l'analyse RPG (plus rapide)")
    parser.add_argument("--verbose",  action="store_true")
    parser.add_argument("--json",     action="store_true", help="Sortie JSON")
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Mode brut : ne renvoie que les JSON bruts (Annuaire/BODACC/DVF), sans scoring",
    )
    args = parser.parse_args()

    if args.raw:
        payload = {
            "siren": args.siren,
            "idus": args.idus,
            "annuaire_raw": fetch_annuaire_raw(args.siren),
            "bodacc_raw": fetch_bodacc_raw(args.siren),
            "sirene": fetch_sirene(args.siren),
            "dvf_raw": {idu: lookup_dvf_raw(idu) for idu in args.idus},
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        sys.exit(0)

    rapport = scorer_durete(
        siren    = args.siren,
        idus     = args.idus,
        avec_rpg = not args.no_rpg,
        verbose  = args.verbose,
    )

    if args.json:
        def _serial(obj):
            if hasattr(obj, "__dataclass_fields__"):
                return {k: _serial(v) for k, v in asdict(obj).items()}
            if isinstance(obj, StatutDVF):
                return obj.value
            if isinstance(obj, list):
                return [_serial(i) for i in obj]
            return obj
        from dataclasses import asdict
        print(json.dumps(_serial(rapport), indent=2, ensure_ascii=False))
    else:
        afficher_rapport(rapport)