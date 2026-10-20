# -*- coding: utf-8 -*-
import json

import pytest

from conftest import (IDU_CDC, IDU_FOND, IDU_SCI, SIREN_CDC, SIREN_FOND, SIREN_SCI, FakeLLM)
from durete_v4.llm import ResultatRecherche
from durete_v4.pipeline import analyser_pm
from durete_v4.scoring import (bareme_axe4, scorer_axe1, scorer_axe3, scorer_detention,
                               surcharge_bail_parcelle)
from durete_v4.service import run_durete_for_siren
from durete_v4.web_research import valider_constats


def run(sources, siren, idus, **kw):
    return analyser_pm(siren, idus, sources=sources, llm_provider="none", **kw)


# ------------------------------------------------------------------ règles unitaires
def test_bareme_axe4():
    assert [bareme_axe4(a)[0] for a in (1, 4, 10, 20, 40)] == [15, 12, 8, 8, 12]


def test_detention_intervalle_point_median():
    pts, lib, fiab = scorer_detention({"statut": "anterieure_fenetre", "annees_min": 5.8, "annees_max": 40})
    assert pts == 8 and fiab == "faible" and "point médian" in lib


def test_axe1_structure_env_par_nom():
    assert scorer_axe1({"denomination": "Conservatoire d'Espaces Naturels Nouvelle-Aquitaine",
                        "nature_juridique": "9220"}).score == 38


def test_axe1_naf_promoteur():
    assert scorer_axe1({"denomination": "X", "nature_juridique": "5710", "naf": "41.10A"}).score == 18


def test_axe3_finances_annuaire_utilisees():
    p = {"nature_juridique": "5710", "finances": [{"annee": 2024, "ca": 5e5, "resultat_net": -2e4},
                                                  {"annee": 2023, "ca": 6e5, "resultat_net": -1e4}]}
    assert scorer_axe3(p, {}).score == 6


def test_axe3_liquidation_prioritaire():
    p = {"nature_juridique": "5710", "finances": [{"annee": 2024, "ca": 2e7, "resultat_net": 3e6}]}
    b = {"procedure": {"statut": "en_cours", "type": "liquidation", "date": "2026-01-01"}}
    assert scorer_axe3(p, b).score == 0


@pytest.mark.parametrize("code4,naf,attendu", [
    ("6540", "68.20B", ("BAIL_CERTAIN", 15)),        # SCI, PAC déclarée par un tiers
    ("6533", "01.11Z", ("FAIRE_VALOIR_DIRECT", 5)),  # GAEC : déclare sa propre PAC, pas de bail
    ("6534", "68.20B", ("BAIL_GFA", 15)),
])
def test_bail_depend_du_proprietaire(code4, naf, attendu):
    summ = {"nb_annees_agricoles": 6, "max_consecutif": 6, "part_annees_agricoles": 0.5}
    pts, code, _ = surcharge_bail_parcelle(summ, {"nature_juridique": code4, "naf": naf})
    assert (code, pts) == attendu


# ------------------------------------------------------------------ bout en bout (3 PM du pool)
def test_sci_bel_air(sources):
    r = run(sources, SIREN_SCI, [IDU_SCI])
    ax = r["detail_axes"]
    assert r["statut"] == "ok"
    assert (ax["axe1"], ax["axe2"], ax["axe3"], ax["axe4"]) == (10, 10, 12, 8)
    assert ax["surcharges"] == 15                                     # bail rural (vigne déclarée 7 ans)
    assert r["score_final"] == 55 and r["niveau_durete"] == "Dureté modérée"
    assert r["confiance"] == "moyenne"                                # associés inconnus + finances + DVF
    codes = {s["code"] for s in ax["v4"]["signaux"]}
    assert "SIG_DIRIGEANT_AGE" in codes
    assert any("L.132-3" in x for x in r["recommandations"])


def test_fondation(sources):
    r = run(sources, SIREN_FOND, [IDU_FOND])
    ax = r["detail_axes"]
    assert (ax["axe1"], ax["axe2"], ax["axe3"]) == (26, 18, 15)       # finances 2024 lues, CAC exclu
    assert ax["surcharges"] == 0


def test_cdc_montesquieu(sources):
    r = run(sources, SIREN_CDC, [IDU_CDC])
    ax = r["detail_axes"]
    assert (ax["axe1"], ax["axe2"], ax["axe3"]) == (35, 20, 18)
    assert ax["axe4"] == 12                                           # mutation avril 2023 → 3,5 ans
    # bail probable (4 ans consécutifs, +8) + zonage AU 60 % (+5)
    assert ax["surcharges"] == 13
    assert r["score_final"] == 98                                     # 35+20+18+12+13
    assert r["niveau_durete"] == "Dureté rédhibitoire"
    assert any("convention" in x.lower() for x in r["recommandations"])
    assert any("Personne publique" in a for a in r["avertissements"])
    assert any("SIG_DEPRISE" == s["code"] for s in ax["v4"]["signaux"])


def test_determinisme(sources):
    a = run(sources, SIREN_SCI, [IDU_SCI])
    b = run(sources, SIREN_SCI, [IDU_SCI])
    assert a["score_final"] == b["score_final"] and a["detail_axes"]["v4"]["axes"] == b["detail_axes"]["v4"]["axes"]


def test_idus_depuis_fichier_pm_et_patrimoine(sources):
    r = run(sources, SIREN_SCI, None)
    assert r["idus"] == [IDU_SCI] and r["patrimoine"]["nb_parcelles"] == 1


def test_annuaire_absent_repli_input(sources):
    sources.fetch_annuaire = lambda s: {}
    r = analyser_pm(SIREN_SCI, [IDU_SCI], sources=sources, llm_provider="none", forme_juridique_input="6540")
    assert r["detail_axes"]["axe1"] == 10 and r["confiance"] == "faible"


def test_siren_invalide(sources):
    assert run(sources, "123456789", [IDU_SCI])["statut"] == "erreur"


def test_sortie_compatible_v3_et_serialisable(sources):
    r = run_durete_for_siren(SIREN_SCI, [IDU_SCI], sources=sources, llm_provider="none")
    for k in ("siren", "denomination", "idus", "nb_parcelles", "statut", "score_final", "niveau_durete",
              "explication", "detail_axes", "avertissements", "ts_debut", "ts_fin", "duree_s"):
        assert k in r
    for k in ("axe1", "axe1_note", "axe4_note", "surcharges", "surcharges_note"):
        assert k in r["detail_axes"]
    json.dumps(r, ensure_ascii=False)
    assert r["explication"]                                           # synthèse déterministe sans LLM


def test_domaine_public_confirme_force_100(sources):
    r = run(sources, SIREN_CDC, [IDU_CDC], domaine_public_confirme=[IDU_CDC])
    assert r["score_final"] == 100


# ------------------------------------------------------------------ LLM (factice)
def test_narration_llm_n_altere_pas_le_score(sources):
    llm = FakeLLM(json_out={"synthese": "Texte.", "leviers": ["a"], "points_blocage": [], "prochaine_action": "b",
                            "score_final": 3})
    r = analyser_pm(SIREN_SCI, [IDU_SCI], sources=sources, llm=llm)
    assert r["score_final"] == 55 and r["explication"] == "Texte."


def test_recherche_web_garde_seulement_le_source_et_non_homonyme(sources):
    res = ResultatRecherche(
        texte="...",
        sources=[{"titre": "sudouest.fr", "url": "https://sudouest.fr/a"}],
        appuis=[{"texte": "La SCI de Bel Air à Martillac a déposé un permis d'aménager en 2025.", "sources": [0]},
                {"texte": "Une SCI Bel Air à Lille a été liquidée.", "sources": [0]},
                {"texte": "Fait sans source aucune ici présent.", "sources": []}],
    )
    cls = {"constats": [
        {"extrait": 1, "categorie": "projet_amenagement", "effet_durete": "hausse", "correspondance": "nom_commune",
         "date": "2025", "constat": "Permis d'aménager déposé en 2025."},
        {"extrait": 2, "categorie": "difficultes", "effet_durete": "baisse", "correspondance": "incertaine",
         "constat": "Homonyme liquidé."},
        {"extrait": 9, "categorie": "autre", "effet_durete": "baisse", "correspondance": "siren",
         "constat": "Inventé."},
    ]}
    llm = FakeLLM(recherche=res, json_out=cls)
    r = analyser_pm(SIREN_SCI, [IDU_SCI], sources=sources, llm=llm, avec_recherche_web=True, avec_narration=False)
    web = r["recherche_web"]
    assert [c["constat"] for c in web["constats"]] == ["Permis d'aménager déposé en 2025."]
    assert web["nb_ecartes"] == 2 and web["constats"][0]["sources"][0]["url"] == "https://sudouest.fr/a"
    assert "GERANT-A" not in llm.prompts[0]                           # RGPD : pas de nom de dirigeant envoyé
    assert r["score_final"] == 55                                     # le web n'entre pas dans le score


def test_valider_constats_vide():
    assert valider_constats({}, [])["constats"] == []


def test_sources_muettes_score_non_fiable(sources):
    sources.fetch_annuaire = lambda s: {}
    sources.fetch_bodacc_records = lambda s: []
    sources.fetch_mutations = lambda i: {"idu": i, "mutations": [], "millesimes_ok": [], "couverture": "indisponible"}
    sources.fetch_rpg = lambda i: {"idu": i, "erreur": "réseau", "summary": None}
    sources.fetch_zonage = lambda i: {"statut": "indisponible", "parts": {}}
    sources.parcelles = None
    r = run(sources, SIREN_SCI, [IDU_SCI])
    assert r["statut"] == "ok" and r["score_fiable"] is False and r["indice_confiance"] < 40
