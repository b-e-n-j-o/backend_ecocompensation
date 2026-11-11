"""Tests purs (sans base) : découpage en terrains et verdict."""
from shapely.geometry import box

from api.recherche_de_foncier.filtrage.layers.unites_foncieres.evaluation import (
    ELIMINATOIRE, EXISTENCE, FAUNE, FLORE, ZH_SURFACE,
    Critere, ParcelleUF, Regles, chain_bool, components, criteres_portes, evaluer_terrain,
)
from api.recherche_de_foncier.filtrage.layers.unites_foncieres.geom import union_metrics

HA = 10_000.0


def P(i, ha=1.0, zh_m2=0.0, **crit):
    return ParcelleUF(id=i, idu=f"I{i}", siren="S", surface_m2=ha * HA, criteres=crit, zh_m2=zh_m2)


# ----------------------------------------------------------------- composantes
def test_components_coupe_quand_une_parcelle_est_retiree():
    # chaîne 1-2-3-4 ; si 2 est retirée -> {1} et {3,4}
    pairs = [(1, 2), (2, 3), (3, 4)]
    assert components([1, 2, 3, 4], pairs) == [[1, 2, 3, 4]]
    assert components([1, 3, 4], pairs) == [[3, 4], [1]]


def test_components_ignore_paires_hors_liste():
    assert components([1, 2], [(1, 2), (2, 99)]) == [[1, 2]]


# ----------------------------------------------------------------- combinaison ET / OU
def test_chain_bool_gauche_a_droite():
    # (A ET B) OU C, comme faune.chain_sql
    assert chain_bool([True, False, True], ["and", "or"]) is True
    assert chain_bool([True, False, False], ["and", "or"]) is False
    assert chain_bool([False, True], None, "or") is True
    assert chain_bool([False, True], None, "and") is False
    assert chain_bool([], None) is True


# ----------------------------------------------------------------- verdict
def test_existence_une_parcelle_suffit():
    c = [Critere("cesbio_libelles", "CESBIO", EXISTENCE)]
    v = evaluer_terrain([P(1, 5, cesbio_libelles=False), P(2, 5, cesbio_libelles=True)], c, Regles(min_area_m2=7 * HA))
    assert v.ok and v.detail["cesbio_libelles"]["idus"] == ["I2"]


def test_eliminatoire_manuel_echoue_et_liste_la_fautive():
    c = [Critere("geomce", "GEOMCE", ELIMINATOIRE)]
    v = evaluer_terrain([P(1, 5, geomce=True), P(2, 5, geomce=False)], c, Regles(min_area_m2=7 * HA))
    assert not v.ok and v.motif == "geomce" and v.detail["geomce"]["idus"] == ["I2"]


def test_surface_est_une_somme():
    v = evaluer_terrain([P(1, 3), P(2, 3)], [], Regles(min_area_m2=7 * HA))
    assert not v.ok and v.motif == "surface"
    assert evaluer_terrain([P(1, 4), P(2, 3)], [], Regles(min_area_m2=7 * HA)).ok


def test_faune_et_deux_especes_sur_deux_parcelles_differentes():
    c = [Critere("faune_0", "crapaud", FAUNE), Critere("faune_1", "pélobate", FAUNE)]
    ps = [P(1, 5, faune_0=True, faune_1=False), P(2, 5, faune_0=False, faune_1=True)]
    assert evaluer_terrain(ps, c, Regles(min_area_m2=HA, fauna_combine="and")).ok
    ps = [P(1, 5, faune_0=True, faune_1=False), P(2, 5, faune_0=True, faune_1=False)]
    v = evaluer_terrain(ps, c, Regles(min_area_m2=HA, fauna_combine="and"))
    assert not v.ok and v.motif == "biodiversite"


def test_faune_ou_flore():
    c = [Critere("faune_0", "crapaud", FAUNE), Critere("flore_0", "orchidée", FLORE)]
    ps = [P(1, 5, faune_0=False, flore_0=True)]
    assert evaluer_terrain(ps, c, Regles(min_area_m2=HA, bio_combine="or")).ok
    assert not evaluer_terrain(ps, c, Regles(min_area_m2=HA, bio_combine="and")).ok


def test_zone_humide_somme_des_parcelles():
    c = [Critere("zone_humide_mode", "ZH ≥ 1 ha", ZH_SURFACE)]
    ps = [P(1, 5, zh_m2=6000), P(2, 5, zh_m2=5000)]
    assert evaluer_terrain(ps, c, Regles(min_area_m2=HA, min_zh_m2=HA)).ok
    assert not evaluer_terrain(ps[:1], c, Regles(min_area_m2=HA, min_zh_m2=HA)).ok


def test_miller_affiche_mais_bloquant_seulement_sur_option():
    r = Regles(min_area_m2=HA, miller_thresh=0.5)
    assert evaluer_terrain([P(1, 5)], [], r, miller=0.2).ok
    r.miller_sur_terrain = True
    v = evaluer_terrain([P(1, 5)], [], r, miller=0.2)
    assert not v.ok and v.motif == "miller"


def test_criteres_portes():
    c = [Critere("faune_0", "crapaud", FAUNE), Critere("cesbio_libelles", "maïs", EXISTENCE)]
    v = evaluer_terrain([P(1, 5, faune_0=True, cesbio_libelles=True), P(2, 5)], c, Regles(min_area_m2=HA))
    assert criteres_portes("I1", v.detail) == ["faune_0", "cesbio_libelles"]
    assert criteres_portes("I2", v.detail) == []


# ----------------------------------------------------------------- géométrie
def test_union_metrics_deux_carres_colles():
    m = union_metrics([box(0, 0, 100, 100).wkt, box(100, 0, 200, 100).wkt], cx=100, cy=50)
    assert abs(m["surface_ha"] - 2.0) < 1e-9
    assert abs(m["dist_centre_m"]) < 1e-9
    assert 0.69 < m["miller"] < 0.70        # rectangle 2x1 : 4π·2/36
