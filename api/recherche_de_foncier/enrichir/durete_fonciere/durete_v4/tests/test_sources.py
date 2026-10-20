# -*- coding: utf-8 -*-
from datetime import date

import pytest
from shapely.geometry import box

from conftest import ANNUAIRE, BODACC, SIREN_FOND, SIREN_SCI, rec
from durete_v4 import net
from durete_v4.cadastre import normaliser_idu
from durete_v4.sources import annuaire, bodacc, dvf, gpu, parcelles, rpg


# ------------------------------------------------------------------ cadastre
@pytest.mark.parametrize("brut,attendu", [
    ("33213000BP0018", "33213000BP0018"),
    ("86275000D0319", "862750000D0319"),     # section 1 lettre non paddée
    ("33213 000 bp 0018", "33213000BP0018"),
])
def test_normaliser_idu(brut, attendu):
    assert normaliser_idu(brut) == attendu


# ------------------------------------------------------------------ annuaire
def test_annuaire_match_exact_siren(monkeypatch):
    leurre = {"siren": "123456782", "nom_complet": "SCI DE BEL AIR (homonyme)"}
    monkeypatch.setattr(net, "get_json", lambda *a, **k: (200, {"results": [leurre, ANNUAIRE[SIREN_SCI]]}))
    assert annuaire.fetch_annuaire(SIREN_SCI)["siren"] == SIREN_SCI
    monkeypatch.setattr(net, "get_json", lambda *a, **k: (200, {"results": [leurre]}))
    assert annuaire.fetch_annuaire(SIREN_SCI) == {}


def test_annuaire_finances_par_annee_et_cac_exclus():
    p = annuaire.normaliser_annuaire(ANNUAIRE[SIREN_FOND])
    assert [f["annee"] for f in p["finances"]] == [2024, 2023]          # V3 lisait finances["ca"] → None
    assert p["finances"][0]["resultat_net"] == 42000
    assert len(p["dirigeants"]) == 3 and len(p["decideurs"]) == 2       # commissaire aux comptes exclu
    assert p["libelle_forme_juridique"] == "Association déclarée"


# ------------------------------------------------------------------ bodacc
def test_bodacc_filtre_autres_siren_et_ordre():
    b = bodacc.analyser_bodacc(BODACC[SIREN_SCI], SIREN_SCI, date(2026, 10, 14))
    assert b["nb_annonces"] == 2 and b["nb_annonces_ecartees_autre_siren"] == 1
    assert b["capital_social"] == pytest.approx(1524.49)
    assert b["procedure"]["statut"] == "aucune" and not b["radiee"]
    assert b["nb_modifications_3ans"] == 1


def test_bodacc_detecte_procedure_collective_la_plus_recente():
    s = "552081317"
    recs = [
        rec(s, "collective", "2025-01-10",
            jugement='{"famille": "Jugement d\'ouverture", "nature": "Jugement d\'ouverture d\'une procédure de redressement judiciaire"}'),
        rec(s, "collective", "2025-06-02",
            jugement='{"famille": "Jugement prononçant", "nature": "Jugement de conversion en liquidation judiciaire"}'),
    ]
    b = bodacc.analyser_bodacc(list(reversed(recs)), s)                  # ordre d'entrée indifférent
    assert b["procedure"] == {**b["procedure"], "statut": "en_cours", "type": "liquidation", "date": "2025-06-02"}


@pytest.mark.parametrize("nature,attendu", [
    ("Jugement arrêtant le plan de redressement", ("plan", "redressement")),
    ("Jugement de clôture pour insuffisance d'actif", ("cloturee", None)),
    ("Jugement arrêtant le plan de cession", ("en_cours", "cession")),
    ("Jugement d'ouverture d'une procédure de sauvegarde", ("en_cours", "sauvegarde")),
])
def test_classer_jugement(nature, attendu):
    assert bodacc.classer_jugement(nature) == attendu


def test_bodacc_dissolution_amiable():
    s = "552081317"
    b = bodacc.analyser_bodacc([rec(s, "modification", "2026-03-01",
                                    modificationsgenerales='{"descriptif": "Dissolution anticipée de la société"}')], s)
    assert b["dissolution"]["date"] == "2026-03-01"


# ------------------------------------------------------------------ dvf
def _row(mid, d, idu, val="100000"):
    return {"id_mutation": mid, "date_mutation": d, "nature_mutation": "Vente", "valeur_fonciere": val,
            "id_parcelle": idu, "surface_terrain": "5000", "nature_culture": "terres", "type_local": ""}


def test_dvf_regroupe_mutations_multi_parcelles():
    rows = [_row("M1", "2022-05-03", "33213000BP0018"), _row("M1", "2022-05-03", "33213000BP0019"),
            _row("M2", "2023-01-01", "33213000ZZ0001")]
    m = dvf.regrouper_mutations(rows, "33213000BP0018")
    assert len(m) == 1 and m[0]["nb_parcelles_mutation"] == 2


def test_detention_mutation_observee():
    d = dvf.estimer_detention({"millesimes_ok": ["2021", "2026"], "couverture": "ok",
                               "mutations": [{"date_mutation": "2024-10-01", "nature_mutation": "Vente",
                                              "nb_parcelles_mutation": 1}]},
                              "2010-01-01", date(2026, 10, 14))
    assert d.statut == "mutation_observee" and d.annees_min == pytest.approx(2.0, abs=0.1)


def test_detention_intervalle_sans_mutation():
    d = dvf.estimer_detention({"millesimes_ok": ["2021", "2022"], "couverture": "ok", "mutations": []},
                              "2009-03-12", date(2026, 10, 14))
    assert d.statut == "anterieure_fenetre"
    assert d.annees_min == pytest.approx(5.8, abs=0.1) and d.annees_max == pytest.approx(17.6, abs=0.1)


def test_detention_mutation_avant_creation_pm_apport():
    d = dvf.estimer_detention({"millesimes_ok": ["2021"], "couverture": "ok",
                               "mutations": [{"date_mutation": "2021-06-01"}]}, "2022-02-01", date(2026, 10, 14))
    assert d.statut == "apport_probable"


def test_detention_hors_dvf():
    assert dvf.estimer_detention({"couverture": "hors_dvf"}, None).statut == "hors_dvf"


# ------------------------------------------------------------------ rpg
def _gdf(geoms, codes):
    import geopandas as gpd
    return gpd.GeoDataFrame({"code_cultu": codes}, geometry=geoms, crs=2154)


PARCELLE = box(400000, 6400000, 400100, 6400100)      # 1 ha en Lambert-93


def test_rpg_lisere_ignore_et_surface_l93():
    lisere = box(400099, 6400000, 400150, 6400100)       # 1 m × 100 m dans la parcelle = 100 m²
    assert rpg.analyser_annee(_gdf([lisere], ["BTH"]), PARCELLE)["status"] == "non_declare"
    champ = box(399950, 6400000, 400080, 6400100)        # couvre 80 % de la parcelle
    r = rpg.analyser_annee(_gdf([champ], ["BTH"]), PARCELLE)
    assert r["status"] == "agricole" and r["surface_m2"] == 8000 and r["couverture"] == 0.8


def test_rpg_millesime_indisponible_ne_casse_pas_la_continuite():
    by_year = {y: {"status": "agricole", "cultures": {"PPH": 1}} for y in range(2016, 2022)}
    by_year[2018] = {"status": "indisponible"}
    s = rpg.analyser_continuite(by_year)
    assert s["max_consecutif"] == 5 and s["nb_millesimes_indisponibles"] == 1


# ------------------------------------------------------------------ gpu
@pytest.mark.parametrize("tz,lib,attendu", [
    ("AUc", "1AU", "AU"), ("U", "UB", "U"), ("Nh", "Nh", "STECAL"), ("", "2AUx", "AU"), (None, "N", "N"),
])
def test_classer_zone(tz, lib, attendu):
    assert gpu.classer_zone(tz, lib) == attendu


def test_zonage_parts():
    feats = [{"properties": {"typezone": "U", "libelle": "UB"}, "geometry": box(400000, 6400000, 400030, 6400100).__geo_interface__},
             {"properties": {"typezone": "A", "libelle": "A"}, "geometry": box(400030, 6400000, 400200, 6400100).__geo_interface__}]
    z = gpu.analyser_zonage(feats, PARCELLE)
    assert z["classe_dominante"] == "A" and z["parts"] == {"U": 0.3, "A": 0.7}


# ------------------------------------------------------------------ parcelles
def test_droits_coproprietaires_et_demembrement():
    rows = [{"idu": "33213000BP0018", "siren": SIREN_SCI, "code_droit": "U", "contenance": 1000},
            {"idu": "33213000BP0018", "siren": "489709949", "denomination": "AUTRE PM", "code_droit": "N"}]
    d = parcelles.analyser_droits(rows, SIREN_SCI, ["33213000BP0018"])["33213000BP0018"]
    assert d["demembrement"] and d["co_titulaires_pm"][0]["siren"] == "489709949" and not d["pm_non_proprietaire"]
