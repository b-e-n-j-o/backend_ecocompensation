# -*- coding: utf-8 -*-
"""
API Sirene INSEE — OPTIONNELLE en V4 (désactivée par défaut).

L'Annuaire des Entreprises expose déjà tranche d'effectif, caractère employeur,
NAF et état administratif issus de Sirene. Seul apport propre : l'historique des
périodes (changements de dénomination, NAF, catégorie juridique), fourni ici à
titre informatif — plus utilisé dans le score : `nombrePeriodesUniteLegale`
augmente à chaque changement de NAF, de nom ou d'ESS, ce n'est pas un indicateur
de « restructuration » (V3 en faisait +2 pts axe 2).

⚠️ V3 contenait une clé INSEE en dur dans le CLI : à révoquer sur portail-api.insee.fr.
"""
from __future__ import annotations

import logging
import os

from .. import config, net

log = logging.getLogger("durete.sirene")


def fetch_sirene(siren: str, api_key: str | None = None) -> dict:
    key = api_key or os.getenv("INSEE_API_KEY", "")
    if not key:
        return {}
    status, data = net.get_json(f"{config.SIRENE_URL}/siren/{siren}",
                                headers={"X-INSEE-Api-Key-Integration": key}, ttl=86400)
    if status != 200 or not data:
        if status not in (404, None):
            log.warning("Sirene HTTP %s pour %s", status, siren)
        return {}
    ul = data.get("uniteLegale") or {}
    periodes = ul.get("periodesUniteLegale") or []
    return {
        "nb_periodes": ul.get("nombrePeriodesUniteLegale"),
        "historique": [
            {"debut": p.get("dateDebut"), "fin": p.get("dateFin"),
             "denomination": p.get("denominationUniteLegale"),
             "naf": p.get("activitePrincipaleUniteLegale"),
             "categorie_juridique": p.get("categorieJuridiqueUniteLegale"),
             "etat": p.get("etatAdministratifUniteLegale")}
            for p in periodes[:20]
        ],
        "_source": "sirene",
    }
