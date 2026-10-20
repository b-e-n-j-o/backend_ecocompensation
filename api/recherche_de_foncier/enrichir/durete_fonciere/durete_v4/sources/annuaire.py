# -*- coding: utf-8 -*-
"""
Annuaire des Entreprises (recherche-entreprises.api.gouv.fr) — INSEE + RNE + RNA.
Gratuit, sans clé, ~7 req/s.

Corrections vs V3 :
  - vérification que le résultat correspond EXACTEMENT au SIREN demandé
    (la recherche est full-text : `per_page=1` pouvait renvoyer une autre entité) ;
  - `finances` est indexé par année ({"2024": {"ca":…, "resultat_net":…}}) —
    V3 lisait `finances["ca"]`, toujours None ;
  - les commissaires aux comptes ne sont plus comptés comme décideurs ;
  - distinction dirigeants personnes physiques / personnes morales ;
  - extraction des compléments utiles (service public, association, ESS, bio…).
"""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

from .. import config, net
from ..referentiels import libelle_forme_juridique

log = logging.getLogger("durete.annuaire")

_QUALITES_NON_DECIDEURS = re.compile(r"commissaire aux comptes|suppl[ée]ant|contr[ôo]leur", re.I)


def fetch_annuaire(siren: str) -> dict:
    """Réponse brute de l'API pour ce SIREN exact, ou {}."""
    status, data = net.get_json(
        config.ANNUAIRE_URL,
        {"q": siren, "per_page": 5, "page": 1},
        ttl=86400,
        min_interval=config.ANNUAIRE_MIN_INTERVAL_S,
    )
    if status != 200 or not data:
        log.warning("Annuaire indisponible pour %s (HTTP %s)", siren, status)
        return {}
    for res in data.get("results") or []:
        if str(res.get("siren")) == siren:
            return res
    log.warning("SIREN %s absent des résultats Annuaire (recherche full-text sans match exact)", siren)
    return {}


def _finances(raw: Any) -> list[dict]:
    out = []
    if isinstance(raw, dict):
        for annee, v in raw.items():
            if isinstance(v, dict) and str(annee).isdigit():
                out.append({"annee": int(annee), "ca": v.get("ca"), "resultat_net": v.get("resultat_net")})
    return sorted(out, key=lambda x: x["annee"], reverse=True)


def _dirigeant(d: dict) -> dict:
    est_pm = (d.get("type_dirigeant") or "").lower().startswith("personne morale") or bool(d.get("denomination"))
    annee = d.get("annee_de_naissance") or (str(d.get("date_de_naissance") or "")[:4] or None)
    return {
        "type": "pm" if est_pm else "pp",
        "nom": d.get("nom") if not est_pm else None,
        "prenoms": d.get("prenoms") if not est_pm else None,
        "denomination": d.get("denomination") if est_pm else None,
        "siren": d.get("siren") if est_pm else None,
        "qualite": d.get("qualite") or "",
        "annee_naissance": int(annee) if annee and str(annee).isdigit() else None,
    }


def normaliser_annuaire(raw: dict, siren: str = "") -> dict:
    """Profil normalisé de la PM (pur, testable)."""
    if not raw:
        return {}
    siege = raw.get("siege") or {}
    comp = raw.get("complements") or {}
    dirigeants = [_dirigeant(d) for d in raw.get("dirigeants") or []]
    decideurs = [d for d in dirigeants if not _QUALITES_NON_DECIDEURS.search(d["qualite"])]
    code4 = str(raw.get("nature_juridique") or "").strip()
    code4 = code4.zfill(4) if code4 else ""
    annee_courante = date.today().year
    ages = [annee_courante - d["annee_naissance"] for d in decideurs if d["type"] == "pp" and d["annee_naissance"]]
    return {
        "siren": str(raw.get("siren") or siren),
        "denomination": raw.get("nom_complet") or raw.get("nom_raison_sociale") or "",
        "sigle": raw.get("sigle"),
        "nature_juridique": code4,
        "libelle_forme_juridique": libelle_forme_juridique(code4),
        "etat_administratif": (raw.get("etat_administratif") or "").upper(),
        "date_creation": raw.get("date_creation"),
        "date_fermeture": raw.get("date_fermeture"),
        "naf": raw.get("activite_principale") or "",
        "section_naf": raw.get("section_activite_principale"),
        "categorie_entreprise": raw.get("categorie_entreprise"),
        "tranche_effectif": raw.get("tranche_effectif_salarie"),
        "caractere_employeur": raw.get("caractere_employeur"),
        "nb_etablissements_ouverts": raw.get("nombre_etablissements_ouverts"),
        "siege": {
            "adresse": siege.get("adresse"),
            "code_postal": siege.get("code_postal"),
            "commune": siege.get("libelle_commune"),
            "code_commune": siege.get("commune"),
        },
        "dirigeants": dirigeants,
        "decideurs": decideurs,
        "age_max_decideurs_pp": max(ages) if ages else None,
        "finances": _finances(raw.get("finances")),
        "complements": {
            k: comp.get(k)
            for k in (
                "est_service_public", "est_association", "identifiant_association",
                "est_ess", "est_bio", "est_societe_mission", "est_entrepreneur_individuel",
            )
        },
        "collectivite_territoriale": comp.get("collectivite_territoriale"),
        "_source": "annuaire",
    }


def profil_minimal(siren: str, denomination: str = "", forme_juridique: str = "") -> dict:
    """Profil de repli quand l'Annuaire ne répond pas (données de l'input)."""
    code4 = str(forme_juridique or "").strip()
    code4 = code4.zfill(4) if code4 else ""
    return {
        "siren": siren, "denomination": denomination or siren,
        "nature_juridique": code4, "libelle_forme_juridique": libelle_forme_juridique(code4),
        "etat_administratif": "", "date_creation": None, "naf": "", "dirigeants": [],
        "decideurs": [], "finances": [], "complements": {}, "siege": {},
        "categorie_entreprise": None, "age_max_decideurs_pp": None,
        "_source": "input",
    }
