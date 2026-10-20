# -*- coding: utf-8 -*-
"""
Fixtures HORS-LIGNE. Données SYNTHÉTIQUES calquées sur la structure réelle des API
(Annuaire, BODACC v2.1, DVF, RPG, GPU) pour les 3 PM du pool test — les valeurs
(dates, dirigeants, finances) sont illustratives, PAS les données réelles de ces entités.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from durete_v4.llm import ResultatRecherche  # noqa: E402
from durete_v4.pipeline import Sources  # noqa: E402

SIREN_SCI, SIREN_FOND, SIREN_CDC = "952824076", "489709949", "243301264"
IDU_SCI, IDU_FOND, IDU_CDC = "33213000BP0018", "33213000BR0019", "33213000BK0002"

ANNUAIRE = {
    SIREN_SCI: {
        "siren": SIREN_SCI, "nom_complet": "SCI DE BEL AIR", "nature_juridique": "6540",
        "etat_administratif": "A", "date_creation": "2009-03-12", "activite_principale": "68.20B",
        "categorie_entreprise": "PME", "tranche_effectif_salarie": "NN",
        "siege": {"adresse": "1 CHEMIN X 33650 MARTILLAC", "libelle_commune": "MARTILLAC",
                  "code_postal": "33650", "commune": "33268"},
        "dirigeants": [{"nom": "GERANT-A", "prenoms": "Alpha", "annee_de_naissance": "1946",
                        "qualite": "Gérant", "type_dirigeant": "personne physique"}],
        "finances": None, "complements": {"est_association": False},
    },
    SIREN_FOND: {
        "siren": SIREN_FOND, "nom_complet": "FONDATION JACQUELINE DE CHABANNES", "nature_juridique": "9220",
        "etat_administratif": "A", "date_creation": "2005-06-01", "activite_principale": "94.99Z",
        "siege": {"libelle_commune": "LA BREDE", "code_postal": "33650"},
        "dirigeants": [
            {"nom": "PRESIDENT-B", "prenoms": "Beta", "qualite": "Président", "type_dirigeant": "personne physique"},
            {"nom": "TRESORIER-C", "prenoms": "Gamma", "qualite": "Trésorier", "type_dirigeant": "personne physique"},
            {"denomination": "CABINET AUDIT", "siren": "111111118", "qualite": "Commissaire aux comptes titulaire",
             "type_dirigeant": "personne morale"},
        ],
        "finances": {"2024": {"ca": 850000, "resultat_net": 42000}, "2023": {"ca": 790000, "resultat_net": -5000}},
        "complements": {"est_association": True, "identifiant_association": "W332000000"},
    },
    SIREN_CDC: {
        "siren": SIREN_CDC, "nom_complet": "COMMUNAUTE DE COMMUNES DE MONTESQUIEU", "nature_juridique": "7346",
        "etat_administratif": "A", "date_creation": "2001-12-28", "activite_principale": "84.11Z",
        "siege": {"libelle_commune": "MARTILLAC", "code_postal": "33650"},
        "dirigeants": [], "finances": None,
        "complements": {"est_service_public": True, "collectivite_territoriale": {"niveau": "epci"}},
    },
}


def rec(siren, famille, date, **kw):
    r = {"registre": [f"{siren[:3]} {siren[3:6]} {siren[6:]}", siren], "familleavis": famille,
         "dateparution": date, "typeavis_lib": "Avis initial", "url_complete": f"https://bodacc.fr/{siren}/{date}"}
    r.update(kw)
    return r


BODACC = {
    SIREN_SCI: [
        rec(SIREN_SCI, "modification", "2025-02-03",
            modificationsgenerales='{"descriptif": "Modification de la gérance"}'),
        rec(SIREN_SCI, "creation", "2009-03-20",
            listepersonnes='{"personne": {"typePersonne": "pm", "capital": {"montantCapital": "1524,49"}, '
                           '"administration": "Gérant : GERANT-A, Alpha"}}'),
        # annonce d'une AUTRE société qui mentionne le SIREN de la SCI → doit être écartée
        {"registre": ["999 999 999", "999999999"], "familleavis": "vente", "dateparution": "2024-01-10",
         "listepersonnes": '{"personne": {"numeroImmatriculation": {"numeroIdentification": "999 999 999"}}}'},
    ],
    SIREN_FOND: [],
    SIREN_CDC: [],
}

DVF = {  # mutations déjà regroupées (sortie de fetch_mutations)
    IDU_SCI: {"idu": IDU_SCI, "mutations": [], "millesimes_ok": [str(y) for y in range(2021, 2027)], "couverture": "ok"},
    IDU_FOND: {"idu": IDU_FOND, "mutations": [], "millesimes_ok": [str(y) for y in range(2021, 2027)], "couverture": "ok"},
    IDU_CDC: {"idu": IDU_CDC, "mutations": [
        {"id_mutation": "2023-1", "date_mutation": "2023-04-17", "nature_mutation": "Vente",
         "valeur_fonciere": 120000.0, "nb_parcelles_mutation": 3, "surface_terrain": 12000.0}],
        "millesimes_ok": [str(y) for y in range(2021, 2027)], "couverture": "ok"},
}

RPG = {
    IDU_SCI: {"idu": IDU_SCI, "surface_parcelle_m2": 18500, "summary": {
        "nb_millesimes_observes": 13, "nb_annees_agricoles": 9, "max_consecutif": 7,
        "part_annees_agricoles": 0.69, "declaree_dernier_millesime": True, "derniere_annee_agricole": 2024,
        "annees_agricoles": [2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024],
        "cultures_principales": [{"code": "VRC", "libelle": "Vigne : raisins de cuve"}]}, "by_year": {}},
    IDU_FOND: {"idu": IDU_FOND, "surface_parcelle_m2": 42000, "summary": {
        "nb_millesimes_observes": 13, "nb_annees_agricoles": 0, "max_consecutif": 0,
        "part_annees_agricoles": 0.0, "declaree_dernier_millesime": False, "annees_agricoles": [],
        "cultures_principales": []}, "by_year": {}},
    IDU_CDC: {"idu": IDU_CDC, "surface_parcelle_m2": 9000, "summary": {
        "nb_millesimes_observes": 13, "nb_annees_agricoles": 4, "max_consecutif": 4,
        "part_annees_agricoles": 0.31, "declaree_dernier_millesime": False, "derniere_annee_agricole": 2019,
        "annees_agricoles": [2016, 2017, 2018, 2019],
        "cultures_principales": [{"code": "PPH", "libelle": "Prairie permanente"}]}, "by_year": {}},
}

ZONAGE = {
    IDU_SCI: {"statut": "ok", "classe_dominante": "A", "parts": {"A": 1.0}, "zones": []},
    IDU_FOND: {"statut": "ok", "classe_dominante": "N", "parts": {"N": 1.0}, "zones": []},
    IDU_CDC: {"statut": "ok", "classe_dominante": "AU", "parts": {"AU": 0.6, "N": 0.4}, "zones": []},
}


class FakeParcelles:
    def __init__(self, rows):
        self.rows = rows

    def par_siren(self, siren):
        return [r for r in self.rows if r["siren"] == siren]

    def par_idus(self, idus):
        return [r for r in self.rows if r["idu"] in idus]


PARCELLES_ROWS = [
    {"idu": IDU_SCI, "siren": SIREN_SCI, "denomination": "SCI DE BEL AIR", "contenance": 18500,
     "code_insee": "33213", "code_droit": "P"},
    {"idu": IDU_FOND, "siren": SIREN_FOND, "denomination": "FONDATION JACQUELINE DE CHABANNES",
     "contenance": 42000, "code_insee": "33213", "code_droit": "P"},
    {"idu": IDU_CDC, "siren": SIREN_CDC, "denomination": "CDC MONTESQUIEU", "contenance": 9000,
     "code_insee": "33213", "code_droit": "P"},
]


@pytest.fixture
def sources():
    return Sources(
        fetch_annuaire=lambda s: ANNUAIRE.get(s, {}),
        fetch_bodacc_records=lambda s: BODACC.get(s, []),
        fetch_mutations=lambda i: DVF[i],
        fetch_rpg=lambda i: RPG[i],
        fetch_zonage=lambda i: ZONAGE[i],
        fetch_sirene=lambda s: {},
        parcelles=FakeParcelles(PARCELLES_ROWS),
    )


class FakeLLM:
    nom = "fake"

    def __init__(self, recherche: ResultatRecherche | None = None, json_out: dict | None = None):
        self.recherche, self.json_out, self.prompts = recherche, json_out or {}, []

    def search(self, prompt):
        self.prompts.append(prompt)
        return self.recherche

    def json(self, prompt, max_tokens=2048):
        self.prompts.append(prompt)
        return self.json_out
