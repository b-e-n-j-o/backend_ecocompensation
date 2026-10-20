# -*- coding: utf-8 -*-
"""
DVF géolocalisées (Etalab, files.data.gouv.fr/geo-dvf) — gratuit, sans clé.

Corrections vs V3 :
  - téléchargement par COMMUNE ({annee}/communes/{dept}/{insee}.csv, quelques Ko)
    au lieu du fichier DÉPARTEMENTAL gzippé (dizaines de Mo) × 6 millésimes × chaque parcelle ;
  - cache (mémoire + disque optionnel) : en batch, une commune n'est lue qu'une fois ;
  - millésimes sondés dynamiquement (N années glissantes, 404 ignorés) au lieu
    d'une liste codée en dur qui vieillit ;
  - les mutations sont regroupées par `id_mutation` (une mutation = plusieurs lignes) ;
  - la valeur foncière d'une mutation multi-parcelles est signalée comme telle ;
  - la détention est estimée par INTERVALLE quand aucune mutation n'est visible :
    l'acquisition a eu lieu entre la création de la PM et le début de la fenêtre DVF.

Limites connues (documentées dans DESIGN.md) : DVF ne couvre ni l'Alsace-Moselle
ni Mayotte, ne nomme pas acheteur/vendeur, et n'inclut pas apports en société,
donations, successions. La vraie date d'acte (`jdatat`) est dans les Fichiers
fonciers (Cerema, accès restreint).
"""
from __future__ import annotations

import csv
import io
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from functools import lru_cache
from typing import Optional

from .. import config, net
from ..cadastre import parse_idu

log = logging.getLogger("durete.dvf")

DEPTS_SANS_DVF = {"57", "67", "68", "976"}


def annees_candidates(aujourd_hui: Optional[date] = None) -> list[str]:
    y = (aujourd_hui or date.today()).year
    return [str(a) for a in range(y - config.DVF_NB_ANNEES + 1, y + 1)]


@lru_cache(maxsize=512)
def _lignes_commune(insee: str, dept: str, annee: str) -> Optional[tuple]:
    """Lignes DVF d'une commune pour un millésime. None si absent (404/erreur)."""
    url = f"{config.DVF_BASE_URL}/{annee}/communes/{dept}/{insee}.csv"
    status, body = net.get(url, timeout=60, ttl=30 * 86400)
    if status != 200 or not body:
        if status not in (404, None):
            log.warning("DVF HTTP %s pour %s", status, url)
        return None
    return tuple(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))


def _f(v: Optional[str]) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return None


def regrouper_mutations(lignes: list[dict], idu14: str) -> list[dict]:
    """Mutations (dédoublonnées par id_mutation) touchant la parcelle."""
    par_mutation: dict[str, list[dict]] = defaultdict(list)
    for row in lignes:
        par_mutation[row.get("id_mutation") or f"{row.get('date_mutation')}|{row.get('valeur_fonciere')}"].append(row)
    out = []
    for mid, rows in par_mutation.items():
        if not any((r.get("id_parcelle") or "").upper() == idu14 for r in rows):
            continue
        parcelles = {(r.get("id_parcelle") or "").upper() for r in rows if r.get("id_parcelle")}
        r0 = next(r for r in rows if (r.get("id_parcelle") or "").upper() == idu14)
        out.append({
            "id_mutation": mid,
            "date_mutation": r0.get("date_mutation"),
            "nature_mutation": r0.get("nature_mutation"),
            "valeur_fonciere": _f(r0.get("valeur_fonciere")),
            "nb_parcelles_mutation": len(parcelles),
            "surface_terrain": _f(r0.get("surface_terrain")),
            "nature_culture": r0.get("nature_culture") or None,
            "types_locaux": sorted({r.get("type_local") for r in rows if r.get("type_local")}),
        })
    return sorted(out, key=lambda m: m["date_mutation"] or "")


def fetch_mutations(idu: str, annees: Optional[list[str]] = None) -> dict:
    ref = parse_idu(idu)
    annees = annees or annees_candidates()
    if ref.departement in DEPTS_SANS_DVF:
        return {"idu": ref.idu, "mutations": [], "millesimes_ok": [], "couverture": "hors_dvf"}
    lignes, ok = [], []
    for a in annees:
        rows = _lignes_commune(ref.insee, ref.departement, a)
        if rows is None:
            continue
        ok.append(a)
        # on garde toute la commune : nécessaire pour reconstituer les mutations multi-parcelles
        lignes.extend(rows)
    muts = regrouper_mutations(lignes, ref.idu) if lignes else []
    # dédoublonnage inter-millésimes (une même mutation peut réapparaître)
    vues, uniques = set(), []
    for m in muts:
        k = (m["id_mutation"], m["date_mutation"])
        if k not in vues:
            vues.add(k)
            uniques.append(m)
    return {"idu": ref.idu, "mutations": uniques, "millesimes_ok": ok,
            "couverture": "ok" if ok else "indisponible"}


# ---------------------------------------------------------------------------
# Estimation de la durée de détention (pur)
# ---------------------------------------------------------------------------
@dataclass
class Detention:
    statut: str                       # mutation_observee | anterieure_fenetre | apport_probable | inconnue | hors_dvf
    annees_min: Optional[float] = None
    annees_max: Optional[float] = None
    date_derniere_mutation: Optional[str] = None
    nature_derniere_mutation: Optional[str] = None
    valeur_derniere_mutation: Optional[float] = None
    fiabilite: str = "faible"
    signal: str = ""
    avertissements: list = field(default_factory=list)
    nb_mutations: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _parse(d: Optional[str]) -> Optional[date]:
    try:
        return datetime.strptime(str(d)[:10], "%Y-%m-%d").date() if d else None
    except ValueError:
        return None


def _ans(d0: date, d1: date) -> float:
    return round((d1 - d0).days / 365.25, 1)


def estimer_detention(
    dvf: dict,
    date_creation_pm: Optional[str],
    aujourd_hui: Optional[date] = None,
    date_millesime_pm: Optional[str] = None,
) -> Detention:
    today = aujourd_hui or date.today()
    if dvf.get("couverture") == "hors_dvf":
        return Detention("hors_dvf", signal="Département non couvert par DVF (Alsace-Moselle/Mayotte)",
                         avertissements=["Livre foncier : consulter le registre local"])
    ok = sorted(dvf.get("millesimes_ok") or [])
    if not ok:
        return Detention("inconnue", signal="DVF indisponible", avertissements=["Aucun millésime DVF lisible"])

    creation = _parse(date_creation_pm)
    debut_fenetre = date(int(ok[0]), 1, 1)
    muts = [m for m in dvf.get("mutations") or [] if _parse(m.get("date_mutation"))]
    avert = []

    posterieures = [m for m in muts if not creation or _parse(m["date_mutation"]) >= creation]
    anterieures = [m for m in muts if creation and _parse(m["date_mutation"]) < creation]

    if posterieures:
        last = posterieures[-1]
        d = _parse(last["date_mutation"])
        det = Detention(
            "mutation_observee",
            annees_min=_ans(d, today), annees_max=_ans(d, today),
            date_derniere_mutation=last["date_mutation"],
            nature_derniere_mutation=last.get("nature_mutation"),
            valeur_derniere_mutation=last.get("valeur_fonciere"),
            fiabilite="moyenne",
            signal=f"Dernière mutation {last['date_mutation']} ({last.get('nature_mutation')})",
            nb_mutations=len(muts),
        )
        if last.get("nb_parcelles_mutation", 1) > 1:
            avert.append(f"Valeur foncière globale d'une mutation portant sur "
                         f"{last['nb_parcelles_mutation']} parcelles")
        if len(posterieures) > 1:
            avert.append(f"{len(posterieures)} mutations depuis la création de la PM — "
                         "rotation du bien, la plus récente est retenue")
        if date_millesime_pm and _parse(date_millesime_pm) and d > _parse(date_millesime_pm):
            avert.append("Mutation POSTÉRIEURE au millésime du fichier des PM : "
                         "la PM n'est peut-être plus propriétaire")
        det.avertissements = avert + ["DVF ne nomme pas l'acquéreur : on suppose que la dernière "
                                      "mutation correspond à l'acquisition par la PM"]
        return det

    if anterieures:
        # mutation(s) avant la création de la PM puis plus rien : apport en société probable
        hi = _ans(creation, today)
        return Detention(
            "apport_probable", annees_min=min(hi, _ans(debut_fenetre, today)), annees_max=hi,
            date_derniere_mutation=anterieures[-1]["date_mutation"],
            fiabilite="faible",
            signal=f"Mutation {anterieures[-1]['date_mutation']} antérieure à la création de la PM "
                   f"({date_creation_pm}) : apport en société probable",
            avertissements=["La mutation DVF concerne un propriétaire précédent"],
            nb_mutations=len(muts),
        )

    lo = _ans(debut_fenetre, today)
    hi = _ans(creation, today) if creation else None
    if hi is not None and hi < lo:       # PM créée pendant la fenêtre sans mutation visible
        return Detention("apport_probable", annees_min=hi, annees_max=hi, fiabilite="faible",
                         signal="PM créée dans la fenêtre DVF sans mutation : apport/donation probable",
                         avertissements=["Apports, donations et successions ne figurent pas dans DVF"])
    return Detention(
        "anterieure_fenetre", annees_min=lo, annees_max=hi, fiabilite="faible",
        signal=f"Aucune mutation depuis {ok[0]} : acquisition antérieure"
               + (f", après la création de la PM ({str(date_creation_pm)[:4]})" if creation else ""),
        avertissements=["Apports, donations et successions ne figurent pas dans DVF"],
    )
