"""
Tests du scoring urbanistique : interprétation code x libellé (données GPU
imparfaites), vetos, profils, confiance, exhaustivité vis-à-vis de la nomenclature.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from urba_profiler.nomenclature import lookup
from urba_profiler.schemas import AnneauZonage, Cible, Couche, DocumentUrba, Element, Mesure, Synthese
from urba_profiler.scoring import get_rules, interpret, resolve_zone_type, score_cible
from urba_profiler.scoring.loader import RulesError, load_rules

RS = get_rules()
AREA = 10_000.0


# ----------------------------------------------------------------- helpers
def zone(libelle, typezone, pct, libelong=None, destoui=None, n=[0]):
    n[0] += 1
    code, lab, flags = lookup("typezone", typezone)
    return Element(element_id=f"zonage_plu:{n[0]}", source_ids=["z"], nb_entites=1,
                   attributs={"libelle": libelle, "typezone": typezone, "libelong": libelong, "destoui": destoui,
                              "idurba": "33234_PLU_20190702"},
                   code=code, libelle_cnig=lab, flags=flags,
                   mesure=Mesure(position="interieur", pct_cible=pct, surface_m2=pct * AREA / 100))


def psc(code: Optional[str], libelle: Optional[str], pct=None, longueur=None, nature=None, nomenclature="psc", n=[0]):
    n[0] += 1
    t, st = (code.split("-") if code else (None, None))
    c, lab, flags = lookup(nomenclature, t, st)
    m = Mesure(position="interieur", pct_cible=pct, surface_m2=(pct or 0) * AREA / 100) if pct is not None else \
        Mesure(position="interieur", longueur_m=longueur)
    if not libelle:
        flags = flags + ["libelle_vide"]
    return Element(element_id=f"el:{n[0]}", source_ids=["x"], nb_entites=1,
                   attributs={"libelle": libelle, "nature": nature}, code=c, libelle_cnig=lab, flags=flags, mesure=m)


def sup(suptype, nomsuplitt=None, pct=100.0, nomass=None, n=[0]):
    n[0] += 1
    return Element(element_id=f"sup:{n[0]}", source_ids=["s"], nb_entites=1,
                   attributs={"suptype": suptype, "nomsuplitt": nomsuplitt, "nomass": nomass},
                   mesure=Mesure(position="interieur", pct_cible=pct, surface_m2=pct * AREA / 100))


def couche(key, famille, kind, elements, status="intersected"):
    return Couche(layer_key=key, display_name=key, famille=famille, geom_kind=kind, typename=key,
                  status=status if elements or status == "error" else "not_intersected",
                  elements_count=len(elements), elements=elements)


def cible(zones=(), psc_surf=(), psc_lin=(), infos=(), sups=(), anneau=None, docs=None, zonage_status="intersected",
          extra=()):
    couches = [
        couche("zonage_plu", "zonage", "surf", list(zones), status=zonage_status),
        couche("prescriptions_surf", "prescription", "surf", list(psc_surf)),
        couche("prescriptions_lin", "prescription", "lin", list(psc_lin)),
        couche("infos_surf", "information", "surf", list(infos)),
        couche("sup_assiette_s", "servitude", "surf", list(sups)),
        *extra,
    ]
    syn = Synthese(anneau_zonage=AnneauZonage(rayon_m=200, surface_m2=1e5, pct=anneau) if anneau else None)
    return Cible(id="P", type="parcelle", surface_m2=AREA, synthese=syn, couches=couches,
                 documents=docs or [DocumentUrba(idurba="33234_PLU_20190702", typedoc="PLU", date="2019-07-02")])


def S(c, profil="generique"):
    return score_cible(c, profil=profil, today=date(2026, 10, 13))


def contrib(sc, rule_id):
    return next((c for c in sc.contributions if c.rule_id == rule_id), None)


# ------------------------------------------------- référentiel : intégrité
def test_regles_chargees_et_valides():
    assert RS.version and len(RS.regles) > 80
    assert abs(sum(RS.ponderation.values()) - 1) < 1e-9


def test_exhaustivite_nomenclature_cnig():
    """Chaque code PSC / INF du standard CNIG v2024-01 est couvert par une règle."""
    codes = json.loads((Path(__file__).parents[1] / "urba_profiler" / "cnig_codes.json").read_text(encoding="utf-8"))
    manquants = []
    for nom, fam in (("psc", "prescription"), ("inf", "information")):
        for code in codes[nom]:
            if (fam, code) not in RS.by_code and (fam, code[:2] + "-*") not in RS.by_code:
                manquants.append(f"{fam} {code}")
    assert not manquants, f"codes sans règle : {manquants}"


def test_regles_justifiees():
    for r in RS.regles:
        assert r.justification, f"{r.id} sans justification"
    for s in RS.secteurs:
        assert s.justification, f"{s.id} sans justification"
    for t, v in RS.zonage_base.items():
        assert v.get("justification"), t


def test_validation_yaml(tmp_path):
    bad = (Path(RS.source).read_text(encoding="utf-8")
           .replace("effets: {perennite: 18}", "effets: {perenite: 18}", 1))
    p = tmp_path / "r.yaml"
    p.write_text(bad, encoding="utf-8")
    with pytest.raises(RulesError, match="sous-score inconnu"):
        load_rules(str(p))


# --------------------------------------------- interprétation code x libellé
def it(famille, el):
    return interpret(RS, famille, el)


def test_code_confirme_par_libelle():
    i = it("prescription", psc("01-01", "Espace boisé classé", pct=30))
    assert i.rule.id == "psc_ebc" and i.fiabilite == "haute" and not i.conflit


def test_conflit_code_libelle_le_libelle_gagne():
    # code EBC mais libellé d'emplacement réservé de voirie (erreur de saisie typique)
    i = it("prescription", psc("01-00", "Emplacement réservé n°4 : élargissement de la voie communale", pct=10))
    assert i.rule.id == "psc_er_voirie"
    assert i.fiabilite == "moyenne" and i.conflit and "incohérent" in i.conflit


def test_mot_cle_incident_ne_cree_pas_de_conflit():
    # libellé ER qui mentionne un EBC : le code ER voirie reste valide
    i = it("prescription", psc("05-01", "ER n°3 élargissement de la voie le long de l'EBC", pct=5))
    assert i.rule.id == "psc_er_voirie" and i.fiabilite == "haute" and not i.conflit


def test_code_99_precise_par_libelle():
    i = it("prescription", psc("99-00", "Trame verte et bleue", pct=40))
    assert i.rule.id == "psc_tvb" and i.fiabilite == "moyenne" and not i.conflit


def test_code_absent_libelle_seul_fiabilite_basse():
    i = it("prescription", psc(None, "Zone humide à préserver", pct=40))
    assert i.rule.id == "kw_zone_humide" and i.fiabilite == "basse"


def test_code_hors_nomenclature():
    i = it("prescription", psc("77-00", "Haies bocagères", longueur=80))
    assert i.rule.id == "kw_haie" and i.fiabilite == "basse"


def test_raffinement_er():
    assert it("prescription", psc("05-00", "ER 12 - bassin de rétention des eaux pluviales", pct=10)).rule.id == "psc_er_hydraulique"
    assert it("prescription", psc("05-00", "ER 7 : création d'une coulée verte", pct=10)).rule.id == "psc_er_espaces_verts"
    assert it("prescription", psc("05-00", "Emplacement réservé n°2 logements sociaux", pct=10)).rule.id == "psc_er_logement"
    assert it("prescription", psc("05-00", "ER n°9", pct=10)).rule.id == "psc_er_generique"
    assert it("prescription", psc("05-02", "ER station d'épuration extension bassin d'orage", pct=10)).rule.id == "psc_er_hydraulique"


def test_raffinement_oap():
    assert it("prescription", psc("18-00", "OAP secteur habitat des Vignes", pct=60)).rule.id == "psc_oap_projet"
    assert it("prescription", psc("18-00", "OAP biodiversité et paysage", pct=60)).rule.id == "psc_oap_patrimoniale_eco"


def test_code_paysager_generique_precise_en_haie():
    i = it("prescription", psc("07-02", "Haie bocagère à protéger", longueur=50))
    assert i.rule.id == "kw_haie" and not i.conflit


def test_groupes_compatibles():
    i = it("prescription", psc("02-01", "Zone inondable - aléa fort", pct=50))
    assert i.rule.id == "psc_inconstructible" and i.fiabilite == "haute"


def test_information_mal_rangee_reconnue_par_libelle():
    # un EBC versé par erreur dans la couche "information" (code inexistant côté INF)
    el = psc("01-00", "Espace boisé classé", pct=30, nomenclature="inf")
    assert it("information", el).rule.id == "psc_ebc"


def test_meme_code_familles_differentes():
    assert it("prescription", psc("25-00", "Continuités écologiques", pct=20)).rule.id == "psc_tvb"
    assert it("information", psc("25-00", "PAEN", pct=20, nomenclature="inf")).rule.id == "inf_paen"


def test_servitudes():
    assert it("servitude", sup("I4", "Lignes électriques")).rule.id == "sup_i4_electricite"
    assert it("servitude", sup("ac1", "Monuments historiques")).rule.id == "sup_ac1_mh"
    i = it("servitude", sup("EL99", None))
    assert i.rule.id == "sup_el_autres" and i.fiabilite == "moyenne"  # repli préfixe
    i = it("servitude", sup(None, "Protection des monuments historiques"))
    assert i.rule.id == "sup_ac1_mh" and i.fiabilite == "basse"
    assert it("servitude", sup("AS1", "Captage", nomass="Périmètre de protection immédiate")).rule.id == "sup_as1_immediat"


# ---------------------------------------------------------------- zonage
@pytest.mark.parametrize("tz,lib,attendu,fiab,conflit", [
    ("N", "Nzh", "N", "haute", False),
    ("A", "Ap", "A", "haute", False),
    ("AUc", "1AUh", "AUc", "haute", False),
    ("AUs", "2AU", "AUs", "haute", False),
    ("AUs", "1AUe", "AUc", "moyenne", True),  # incohérence interne AU : hypothèse prudente
    ("N", "1AUh", "AUc", "moyenne", True),  # typezone faux, libellé explicite
    ("A", "UB", "U", "moyenne", True),
    (None, "Nf", "N", "basse", False),
    ("U", None, "U", "moyenne", False),
    (None, None, "inconnu", "basse", False),
])
def test_resolve_zone_type(tz, lib, attendu, fiab, conflit):
    t, f, c = resolve_zone_type(tz, lib)
    assert (t, f, bool(c)) == (attendu, fiab, conflit)


def test_zone_naturelle_meilleure_que_agricole_meilleure_que_au():
    n = S(cible([zone("N", "N", 100)])).score_global
    a = S(cible([zone("A", "A", 100)])).score_global
    aus = S(cible([zone("2AU", "AUs", 100)])).score_global
    assert n > a > aus


def test_veto_zone_urbaine():
    sc = S(cible([zone("UB", "U", 80), zone("N", "N", 20)]))
    assert any(v.niveau == "bloquant" for v in sc.vetos)
    assert sc.score_global <= RS.plafonds_veto["bloquant"] and sc.classe == "E"


def test_veto_typezone_errone_detecte_par_libelle():
    sc = S(cible([zone("1AUh", "N", 100)]))  # typezone N mais libellé 1AU
    assert any("AUc" in v.rule_id for v in sc.vetos)
    assert sc.confiance.valeur < 100


def test_secteurs_suffixe_et_libelle_long():
    sc = S(cible([zone("Npv", "N", 100)]))
    c = contrib(sc, "sect_energie")
    assert c and c.points["perennite"] < 0 and c.fiabilite == "moyenne"
    assert any(v.rule_id == "sect_energie" for v in sc.vetos)
    sc2 = S(cible([zone("N3", "N", 100, libelong="Secteur naturel dédié aux zones humides")]))
    assert contrib(sc2, "sect_zone_humide").fiabilite == "haute"


def test_definition_standard_de_zone_ne_declenche_rien():
    # libellé long type R151-24 : ne doit déclencher ni 'forestier' ni 'protégé'
    sc = S(cible([zone("N", "N", 100, libelong="Zone naturelle et forestière à protéger en raison de la qualité des sites")]))
    assert not [c for c in sc.contributions if c.source == "secteur"]
    sc = S(cible([zone("A", "A", 100, libelong="Zone agricole : seules les constructions nécessaires à l'activité agricole sont autorisées")]))
    assert not [c for c in sc.contributions if c.source == "secteur"]


def test_rnu_confiance_faible():
    sc = S(cible([]))
    assert contrib(sc, "zonage_base:RNU")
    assert sc.confiance.valeur <= 70
    assert any("RNU" in p.texte for p in sc.points_a_verifier)


def test_couche_zonage_en_erreur_pas_rnu():
    sc = S(cible([], zonage_status="error"))
    assert contrib(sc, "zonage_base:inconnu")
    assert any("indisponibles" in m for m in sc.confiance.motifs)


def test_destinations_v2024():
    sc = S(cible([zone("N", "N", 100, destoui="21-22")]))
    assert contrib(sc, "destination_habitat").points["perennite"] < 0


# ------------------------------------------------------- profils & vetos
def test_ebc_selon_profil():
    c = cible([zone("N", "N", 100)], psc_surf=[psc("01-01", "EBC", pct=80)])
    zh, bois = S(c, "zones_humides"), S(c, "boisement")
    assert any(v.niveau == "bloquant" for v in zh.vetos) and zh.classe == "E"
    assert not bois.vetos and bois.score_global > zh.score_global + 30
    assert zh.sous_scores["compatibilite"].valeur < bois.sous_scores["compatibilite"].valeur


def test_veto_non_applique_si_donnee_peu_fiable():
    # EBC reconnu sur le seul libellé (code absent) : pas de veto, mais un point à vérifier
    c = cible([zone("N", "N", 100)], psc_surf=[psc(None, "Espaces boisés classés", pct=90)])
    sc = S(c, "zones_humides")
    assert not sc.vetos
    assert any("Veto non appliqué" in p.texte for p in sc.points_a_verifier)


def test_couverture_ponderee():
    petit = S(cible([zone("A", "A", 100)], psc_surf=[psc("05-01", "ER voirie", pct=5)]))
    grand = S(cible([zone("A", "A", 100)], psc_surf=[psc("05-01", "ER voirie", pct=60)]))
    assert petit.score_global > grand.score_global
    assert not petit.vetos and any(v.niveau == "bloquant" for v in grand.vetos)


def test_cumul_plafonne_par_regle():
    one = S(cible([zone("A", "A", 100)], psc_surf=[psc("25-00", "TVB", pct=100)]))
    two = S(cible([zone("A", "A", 100)], psc_surf=[psc("25-00", "TVB", pct=100), psc("25-00", "Corridor", pct=100)]))
    assert one.sous_scores["synergie"].valeur == two.sous_scores["synergie"].valeur


def test_lineaire():
    sc = S(cible([zone("A", "A", 100)], psc_lin=[psc("07-04", "Haie à préserver", longueur=50)]))
    c = contrib(sc, "psc_paysage_ecologique")
    assert 0.2 <= c.facteur <= 1 and c.couverture_pct is None


def test_tvb_et_pression_urbaine():
    base = cible([zone("N", "N", 100)])
    calme = S(cible([zone("N", "N", 100)], anneau={"N": 80, "A": 20}))
    presse = S(cible([zone("N", "N", 100)], anneau={"U": 50, "AUc": 30, "N": 20}))
    assert calme.score_global > S(base).score_global > presse.score_global
    assert contrib(presse, "voisinage_pression_urbaine").points["perennite"] == RS.voisinage["pression_urbaine"]["plafond"]


def test_preemption_et_synergie_zpens():
    sc = S(cible([zone("N", "N", 100)], infos=[psc("03-00", "Zone de préemption ENS", pct=100, nomenclature="inf")]))
    assert sc.sous_scores["maitrise"].valeur < RS.base["maitrise"]
    assert sc.sous_scores["synergie"].valeur > RS.base["synergie"]


def test_annulation_partielle_et_pos():
    sc = S(cible([zone("N", "N", 100)], infos=[psc("98-00", "Annulation partielle", pct=100, nomenclature="inf")]))
    assert sc.confiance.valeur <= 75
    sc2 = S(cible([zone("N", "N", 100)], docs=[DocumentUrba(idurba="33234_POS_19990101", typedoc="POS", date="1999-01-01")]))
    assert any("POS" in m for m in sc2.confiance.motifs)


def test_non_interprete_et_neutres():
    sc = S(cible([zone("A", "A", 100)], psc_surf=[psc("39-02", "Hauteur maximale 9 m", pct=100), psc("77-00", None, pct=50)]))
    ids = {i.element_id: i for i in sc.interpretations}
    assert any(i.rule_id == "psc_regles_construction" for i in ids.values())
    assert any(i.rule_id is None for i in ids.values())
    assert not contrib(sc, "psc_regles_construction")  # neutre : pas de contribution


def test_profil_inconnu():
    with pytest.raises(ValueError):
        S(cible([zone("N", "N", 100)]), "nope")


def test_score_depuis_json_stocke():
    c = cible([zone("N", "N", 100)], psc_surf=[psc("25-00", "TVB", pct=30)])
    stored = json.loads(c.model_dump_json())
    assert score_cible(stored).score_global == score_cible(c).score_global


def test_resume_et_explicabilite():
    sc = S(cible([zone("A", "A", 70), zone("2AU", "AUs", 30)], psc_surf=[psc("05-01", "ER voirie", pct=10)]))
    assert sc.resume[0].startswith("Zonage")
    assert sc.resume[-1].startswith("Score")
    for c in sc.contributions:
        assert c.justification
    assert 0 <= sc.score_global <= 100


def test_code_neutre_contredit_par_libelle_est_un_conflit():
    i = it("prescription", psc("15-01", "Espace boisé classé à conserver", pct=40))
    assert i.rule.id == "psc_ebc" and i.conflit


def test_rescoring_endpoint():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from urba_profiler.router import router

    app = FastAPI()
    app.include_router(router)
    tc = TestClient(app)
    c = cible([zone("N", "N", 100)], psc_surf=[psc("01-01", "EBC", pct=80)])
    r = tc.post("/urba/score", json={"cibles": [json.loads(c.model_dump_json())], "profil": "zones_humides"})
    assert r.status_code == 200, r.text
    assert r.json()["P"]["classe"] == "E"
    assert tc.get("/urba/scoring/regles").json()["version"] == RS.version
