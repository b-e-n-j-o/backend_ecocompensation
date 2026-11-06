"""Tests de l'agrégation parcelles → UF pour l'export (sans base)."""
from types import SimpleNamespace

import pytest

from api.recherche_de_foncier.enrichir.pool.exports.classement_export_attrs import (
    EXPORT_ATTR_KEYS,
    build_parcelle_export_row,
)
from api.recherche_de_foncier.enrichir.pool.exports import uf_export_aggregate as agg

OPTS = SimpleNamespace(
    zdv_natures=[], remontee_nappes_classefiab=[], ebc_mode="ignore", znieff_mode="ignore",
    troncon_hydro_mode="within_radius", troncon_hydro_radius_m=500.0,
    surface_hydro_mode="ignore", surface_hydro_radius_m=500.0,
)


def _item(idu, surf, *, occ=None, fauna=None, zh_ha=None, dist_hyd=None, dur=None, pm=None, comp=None):
    parcelle = {"idu": idu, "code_insee": idu[:5], "section": "A", "numero": idu[-4:],
                "surface_ha": surf, "miller": 0.5, "distance_km": 3.0, "dist_hydro_m": dist_hyd}
    enrich = {}
    if fauna is not None:
        enrich["fauna_distances"] = fauna
    if zh_ha is not None:
        enrich["zone_humide_ha"] = zh_ha
        enrich["zone_humide_intersects"] = zh_ha > 0
    mmap = {"filter_enrich": enrich}
    if occ:
        mmap["cesbio_profile"] = {k: {"pct": v, "area_m2": v / 100 * surf * 1e4} for k, v in occ.items()}
    if dur is not None:
        mmap["durete_fonciere"] = {"score_final": dur, "eligible": True, "niveau_durete": f"niv{dur}"}
    if pm:
        mmap["parcelles_personnes_morales"] = pm
    if comp is not None:
        mmap["composite_score_v1"] = {"score_composite": comp}
    row = build_parcelle_export_row(parcelle, mmap, OPTS, clip_for_shapefile=False)
    return agg.ParcelItem(row=row, mmap=mmap)


UF = {"uf_id": "UF-1", "uf_rang": 1, "uf_type": "PM", "dist_centre_km": 2.345,
      "score_eco": 4, "score_eco_max": 6, "siren": "123456789", "denomination": "GFA DU BOIS"}


def _three():
    return [
        _item("330290000A0076", 9.7, occ={"Forêt fermée de conifères": 99.5, "Forêt ouverte": 0.5},
              fauna={"Crapaud calamite": 340, "Pélobate": 1200}, zh_ha=1.1, dist_hyd=120, dur=40, comp=60),
        _item("330290000A0077", 1.8, occ={"Prairies": 100.0},
              fauna={"Crapaud calamite": 900, "Pélobate": 50}, zh_ha=0.3, dist_hyd=410, dur=70, comp=30),
        _item("330300000B0012", 0.5, occ={"Prairies": 100.0}, fauna={}, zh_ha=0.0, dist_hyd=None),
    ]


def test_tout_le_schema_est_documente():
    for k in agg.UF_LAYER_KEYS + agg.UF_PARC_LAYER_KEYS:
        assert k in agg.COLUMNS, k
    cols = [r["colonne"] for r in agg.dictionary_rows()]
    assert len(cols) == len(set(cols))
    assert set(EXPORT_ATTR_KEYS) <= set(cols)


def test_surfaces_somme_et_pct_recalcule():
    r = agg.aggregate_uf_row(_three(), UF)
    assert r["surf_ha"] == pytest.approx(12.0)
    assert r["zh_ha"] == pytest.approx(1.4)
    assert r["zh_m2"] == pytest.approx(14000)
    assert r["zh_pct"] == pytest.approx(1.4 / 12.0 * 100, abs=0.01)
    assert r["zh_int"] is True


def test_distances_minimum_et_non_calcule_ignore():
    r = agg.aggregate_uf_row(_three(), UF)
    assert r["dist_hyd"] == 120                     # -1 de la 3e parcelle ignoré
    assert r["espece_esp"] == "Pélobate"             # 50 m sur la 2e parcelle
    assert "Crapaud calamite: 340 m" in r["faune_txt"] and "Pélobate: 50 m" in r["faune_txt"]


def test_occupation_du_sol_ponderee_par_surface():
    r = agg.aggregate_uf_row(_three(), UF)
    import json
    occ = json.loads(r["occ_json"])
    assert sum(occ.values()) == pytest.approx(1.0, abs=1e-4)
    assert occ["Forêt fermée de conifères"] == pytest.approx(9.7 * 0.995 / 12.0, abs=1e-4)
    assert occ["Prairies"] == pytest.approx(2.3 / 12.0, abs=1e-4)
    assert r["occ_sol"].startswith("Forêt fermée de conifères")


def test_durete_max_composite_pondere_et_infos_uf():
    r = agg.aggregate_uf_row(_three(), UF, uf_area_m2=120000, uf_perimeter_m=1600)
    assert r["score_dur"] == 70 and r["dur_niv"] == "niv70"
    assert r["score_comp"] == pytest.approx((60 * 9.7 + 30 * 1.8) / 11.5, abs=0.1)
    assert r["siren"] == "123456789" and r["p_morale"] is True
    assert r["score_eco"] == 4 and r["eco_max"] == 6 and r["dist_km"] == 2.35
    assert r["nb_parc"] == 3 and r["parcelles"].count(";") == 2
    assert r["communes"] == "33029, 33030"
    assert r["miller"] == pytest.approx(4 * 3.14159265 * 120000 / 1600 ** 2, abs=1e-3)
    assert list(r) == list(agg.UF_LAYER_KEYS)


def test_ligne_uf_parcelle_reprend_la_ligne_parcelle():
    it = _three()[0]
    row = agg.uf_parcel_row(it, UF)
    assert list(row) == list(agg.UF_PARC_LAYER_KEYS)
    assert row["uf_id"] == "UF-1" and row["rang"] == 1
    for k in EXPORT_ATTR_KEYS:
        if k != "rang":
            assert row[k] == it.row[k]


def test_shapefile_textes_tronques():
    items = _three()
    for it in items:
        it.row["txt_scor"] = "x" * 200
    r = agg.aggregate_uf_row(items, UF, clip_for_shapefile=True)
    assert len(r["txt_scor"]) <= 254
