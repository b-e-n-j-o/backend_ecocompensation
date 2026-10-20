"""
Module DVF — Kerelia Dureté Foncière
======================================
Source : https://files.data.gouv.fr/geo-dvf/latest/csv/
Gratuit, sans clé. Traitement en RAM, rien écrit sur disque.

Expose :
    lookup_dvf(idu, date_creation_pm) -> ResultatDVF
    scorer_axe4_dvf(resultat)         -> (int, str)
"""

import requests
import gzip
import csv
import io
import logging
import json
import argparse
import sys
from datetime import date, datetime
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

log = logging.getLogger("dvf")

BASE_URL = "https://files.data.gouv.fr/geo-dvf/latest/csv"
ANNEES_DISPONIBLES = ["2020", "2021", "2022", "2023", "2024", "2025"]


class StatutDVF(str, Enum):
    ACQUISITION_RECENTE     = "acquisition_recente"
    ACQUISITION_COURTE      = "acquisition_courte"
    ACQUISITION_MOYENNE     = "acquisition_moyenne"
    ACQUISITION_LONGUE      = "acquisition_longue"
    ACQUISITION_TRES_LONGUE = "acquisition_tres_longue"
    AUCUNE_MUTATION         = "aucune_mutation"
    DATE_INCONNUE           = "date_inconnue"
    INCOHERENCE_DATE        = "incoherence_date"


@dataclass
class MutationDVF:
    date_mutation:    Optional[str]
    nature_mutation:  Optional[str]
    valeur_fonciere:  Optional[float]
    surface_terrain:  Optional[float]
    nature_culture:   Optional[str]
    type_local:       Optional[str]
    nom_commune:      Optional[str]
    id_parcelle_dvf:  str
    annee_millesime:  str


@dataclass
class ResultatDVF:
    idu:               str
    variantes:         list
    dept:              str
    mutations:         list = field(default_factory=list)
    nb_mutations:      int = 0
    date_acquisition:  Optional[str] = None
    valeur_acquisition: Optional[float] = None
    nature_acquisition: Optional[str] = None
    nature_culture:    Optional[str] = None
    fiabilite_jdatat:  Optional[str] = None
    score_axe4:        Optional[int] = None
    statut:            Optional[StatutDVF] = None
    signal:            str = ""
    avertissements:    list = field(default_factory=list)
    millesimes_interroges: list = field(default_factory=list)
    millesimes_erreur:     list = field(default_factory=list)


def _normaliser_idu(idu: str) -> tuple:
    idu = idu.strip().upper().replace(" ", "")
    if len(idu) < 10:
        raise ValueError(f"IDU trop court : '{idu}'")
    dept = idu[:2]
    variantes = {idu}
    if len(idu) == 13:
        variantes.add(idu[:5] + "0" + idu[5:])
    if len(idu) == 14 and idu[5] == "0":
        variantes.add(idu[:5] + idu[6:])
    return dept, sorted(variantes)


def _telecharger_dept(dept: str, annee: str) -> Optional[list]:
    url = f"{BASE_URL}/{annee}/departements/{dept}.csv.gz"
    log.info(f"DVF téléchargement {annee}/dept{dept} ...")
    try:
        r = requests.get(url, timeout=60)
        if r.status_code == 404:
            log.debug(f"DVF {annee} dept {dept} absent (404)")
            return None
        if r.status_code != 200:
            log.error(f"DVF HTTP {r.status_code}")
            return None
        contenu = gzip.decompress(r.content).decode("utf-8")
        rows = list(csv.DictReader(io.StringIO(contenu)))
        log.info(f"  ✓ {len(r.content)//1024} Ko → {len(rows):,} lignes")
        return rows
    except requests.exceptions.Timeout:
        log.error(f"DVF timeout {annee}/{dept}")
        return None
    except Exception as e:
        log.error(f"DVF erreur : {e}")
        return None


def _chercher(rows: list, variantes: list, annee: str) -> list:
    vset = set(v.upper() for v in variantes)
    found = []
    for row in rows:
        dvf_id = row.get("id_parcelle", "").strip().upper()
        if dvf_id not in vset:
            continue
        def _f(k, typ=str):
            v = row.get(k, "").replace(",", ".").strip()
            try: return typ(v) if v else None
            except: return None
        found.append(MutationDVF(
            date_mutation   = _f("date_mutation"),
            nature_mutation = _f("nature_mutation"),
            valeur_fonciere = _f("valeur_fonciere", float),
            surface_terrain = _f("surface_terrain", float),
            nature_culture  = _f("nature_culture"),
            type_local      = _f("type_local"),
            nom_commune     = _f("nom_commune"),
            id_parcelle_dvf = dvf_id,
            annee_millesime = annee,
        ))
    log.info(f"  → {len(found)} mutation(s)")
    return found


def _dedoublonner(mutations: list) -> list:
    seen, uniques = set(), []
    for m in mutations:
        key = (m.date_mutation, m.id_parcelle_dvf, m.valeur_fonciere)
        if key not in seen:
            seen.add(key)
            uniques.append(m)
    nb = len(mutations) - len(uniques)
    if nb > 0:
        log.info(f"DVF dédoublonnage : {nb} doublon(s) supprimé(s)")
    return uniques


def lookup_dvf_raw(
    idu: str,
    date_creation_pm: Optional[str] = None,
    annees: Optional[list] = None,
) -> dict:
    """
    Mode brut : renvoie uniquement les données « brutes » issues des téléchargements DVF,
    i.e. les lignes (dict) correspondant à la/aux parcelle(s) recherchée(s), sans scoring.

    NB : le téléchargement se fait au niveau « département » (CSV.gz), comme en mode normal.
    """
    annees = annees or ANNEES_DISPONIBLES
    dept, variantes = _normaliser_idu(idu)

    result = {
        "idu": idu,
        "dept": dept,
        "variantes": variantes,
        "date_creation_pm": date_creation_pm,
        "millesimes": [],
        "matches": [],
        "erreurs": [],
    }

    vset = set(v.upper() for v in variantes)

    for annee in annees:
        url = f"{BASE_URL}/{annee}/departements/{dept}.csv.gz"
        try:
            r = requests.get(url, timeout=60)
            if r.status_code == 404:
                result["millesimes"].append({"annee": annee, "url": url, "http_status": 404, "downloaded": False})
                result["erreurs"].append({"annee": annee, "erreur": "404"})
                continue
            if r.status_code != 200:
                result["millesimes"].append({"annee": annee, "url": url, "http_status": r.status_code, "downloaded": False})
                result["erreurs"].append({"annee": annee, "erreur": f"HTTP {r.status_code}"})
                continue

            contenu = gzip.decompress(r.content).decode("utf-8")
            rows = csv.DictReader(io.StringIO(contenu))

            nb_matches = 0
            for row in rows:
                dvf_id = (row.get("id_parcelle") or "").strip().upper()
                if dvf_id not in vset:
                    continue
                nb_matches += 1
                # On stocke la ligne DVF telle quelle (+ millésime pour contexte)
                result["matches"].append({"annee_millesime": annee, **row})

            result["millesimes"].append({
                "annee": annee,
                "url": url,
                "http_status": 200,
                "downloaded": True,
                "nb_matches": nb_matches,
            })
        except requests.exceptions.Timeout:
            result["millesimes"].append({"annee": annee, "url": url, "http_status": None, "downloaded": False})
            result["erreurs"].append({"annee": annee, "erreur": "timeout"})
        except Exception as e:
            result["millesimes"].append({"annee": annee, "url": url, "http_status": None, "downloaded": False})
            result["erreurs"].append({"annee": annee, "erreur": str(e)})

    return result


def lookup_dvf(
    idu: str,
    date_creation_pm: Optional[str] = None,
    annees: Optional[list] = None,
) -> ResultatDVF:
    """Point d'entrée principal — retourne ResultatDVF complet."""
    annees = annees or ANNEES_DISPONIBLES
    dept, variantes = _normaliser_idu(idu)

    log.info(f"DVF lookup IDU={idu} variantes={variantes}")

    toutes, erreurs = [], []
    for annee in annees:
        rows = _telecharger_dept(dept, annee)
        if rows is None:
            erreurs.append(annee)
            continue
        toutes.extend(_chercher(rows, variantes, annee))
        del rows

    uniques = _dedoublonner(toutes)

    # Construction résultat
    res = ResultatDVF(
        idu=idu, variantes=variantes, dept=dept,
        mutations=uniques, nb_mutations=len(uniques),
        millesimes_interroges=annees, millesimes_erreur=erreurs,
    )

    if not uniques:
        res.statut = StatutDVF.AUCUNE_MUTATION
        res.signal = "Aucune mutation DVF trouvée"
        res.avertissements.append(
            "Apport en nature, donation, héritage ou acquisition antérieure à 2019."
        )
        return res

    tries = sorted([m for m in uniques if m.date_mutation], key=lambda m: m.date_mutation)
    if not tries:
        res.statut = StatutDVF.DATE_INCONNUE
        res.signal = "Mutations présentes sans date exploitable"
        return res

    acq = tries[-1]
    res.date_acquisition   = acq.date_mutation
    res.valeur_acquisition = acq.valeur_fonciere
    res.nature_acquisition = acq.nature_mutation
    res.nature_culture     = acq.nature_culture
    res.fiabilite_jdatat   = "moyenne" if len(tries) > 1 else "haute"

    if len(tries) > 1:
        res.avertissements.append(
            f"{len(tries)} mutations — on retient la plus récente ({acq.date_mutation}) "
            f"comme date d'acquisition par la PM actuelle."
        )

    # Vérification cohérence date création PM
    if date_creation_pm and acq.date_mutation:
        try:
            dt_acq = datetime.strptime(acq.date_mutation, "%Y-%m-%d").date()
            dt_pm  = datetime.strptime(date_creation_pm[:10], "%Y-%m-%d").date()
            if dt_acq < dt_pm:
                res.statut = StatutDVF.INCOHERENCE_DATE
                res.fiabilite_jdatat = "faible"
                res.avertissements.append(
                    f"INCOHÉRENCE : mutation DVF ({acq.date_mutation}) "
                    f"antérieure à la création PM ({date_creation_pm}). "
                    f"Parcelle probablement apportée à la constitution — "
                    f"mutation DVF concerne un propriétaire précédent."
                )
        except ValueError:
            pass

    # Score ancienneté
    try:
        dt_acq    = datetime.strptime(acq.date_mutation, "%Y-%m-%d").date()
        anciennete = (date.today() - dt_acq).days // 365
    except ValueError:
        res.statut = StatutDVF.DATE_INCONNUE
        res.signal = f"Date non parseable : {acq.date_mutation}"
        return res

    if anciennete < 3:
        res.statut, res.score_axe4, res.signal = StatutDVF.ACQUISITION_RECENTE, 15, \
            f"Acquisition très récente ({acq.date_mutation}, {anciennete} an(s)) — revente très improbable."
    elif anciennete < 5:
        res.statut, res.score_axe4, res.signal = StatutDVF.ACQUISITION_COURTE, 12, \
            f"Détention courte ({anciennete} ans) — peu probable de revendre rapidement."
    elif anciennete < 15:
        res.statut, res.score_axe4, res.signal = StatutDVF.ACQUISITION_MOYENNE, 8, \
            f"Détention moyenne ({anciennete} ans) — réceptif si terrain sans revenu."
    elif anciennete < 30:
        res.statut, res.score_axe4, res.signal = StatutDVF.ACQUISITION_LONGUE, 8, \
            f"Détention longue ({anciennete} ans) — attachement probable, projet abandonné."
    else:
        res.statut, res.score_axe4, res.signal = StatutDVF.ACQUISITION_TRES_LONGUE, 12, \
            f"Très longue détention ({anciennete} ans) — inertie décisionnelle maximale."

    return res


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Lookup DVF par IDU (Kerelia)")
    parser.add_argument("--idu", required=True, help="IDU (ex: 86275000D0319)")
    parser.add_argument("--raw", action="store_true", help="Sortie brute JSON (lignes DVF matchées)")
    args = parser.parse_args()

    if args.raw:
        print(json.dumps(lookup_dvf_raw(args.idu), ensure_ascii=False, indent=2))
        sys.exit(0)

    res = lookup_dvf(args.idu)
    # Dataclass -> dict lisible
    print(json.dumps({
        "idu": res.idu,
        "dept": res.dept,
        "variantes": res.variantes,
        "nb_mutations": res.nb_mutations,
        "date_acquisition": res.date_acquisition,
        "valeur_acquisition": res.valeur_acquisition,
        "nature_acquisition": res.nature_acquisition,
        "fiabilite_jdatat": res.fiabilite_jdatat,
        "score_axe4": res.score_axe4,
        "statut": res.statut.value if res.statut else None,
        "signal": res.signal,
        "avertissements": res.avertissements,
        "millesimes_interroges": res.millesimes_interroges,
        "millesimes_erreur": res.millesimes_erreur,
        "mutations": [
            {
                "date_mutation": m.date_mutation,
                "nature_mutation": m.nature_mutation,
                "valeur_fonciere": m.valeur_fonciere,
                "surface_terrain": m.surface_terrain,
                "nature_culture": m.nature_culture,
                "type_local": m.type_local,
                "nom_commune": m.nom_commune,
                "id_parcelle_dvf": m.id_parcelle_dvf,
                "annee_millesime": m.annee_millesime,
            }
            for m in res.mutations
        ],
    }, ensure_ascii=False, indent=2))