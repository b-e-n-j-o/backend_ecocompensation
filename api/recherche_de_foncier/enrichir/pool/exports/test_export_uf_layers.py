"""Câblage de build_uf_export + écriture GeoPackage (accès base simulés)."""
import pyogrio
import pytest
from shapely.geometry import box

from api.recherche_de_foncier.enrichir.pool.exports import export_uf_layers as m
from api.recherche_de_foncier.enrichir.pool.exports.classement_export_attrs import EXPORT_ATTR_KEYS


RESULTS_UF = {"unites_foncieres": [
    {"uf_id": "UF-A", "rang": 1, "siren": "111", "denomination": "SCI A", "distance_centre_km": 1.2,
     "parcelles": [{"idu": "330290000A0001", "noyau": True}, {"idu": "330290000A0002", "noyau": False}],
     "best_score_eco": {"total_score": 5, "max_score": 6}},
    {"uf_id": "UF-B", "rang": 2, "siren": "222", "denomination": "GFA B", "distance_centre_km": 3.4,
     "parcelles": [{"idu": "330290000B0003"}], "best_score_eco": {"total_score": 2, "max_score": 6}},
]}
GEOMS = {"330290000A0001": box(0, 0, 100, 100), "330290000A0002": box(100, 0, 200, 100),
         "330290000B0003": box(500, 500, 600, 700)}
UF_OF = {"330290000A0001": "UF-A", "330290000A0002": "UF-A", "330290000B0003": "UF-B"}


@pytest.fixture
def fake_db(monkeypatch):
    monkeypatch.setattr(m, "_load_uf_filter", lambda e, p: {"fauna_criteria": [{"species": "Crapaud", "dist_m": 500}]})
    monkeypatch.setattr(m, "_project_centre", lambda e, p: (0.0, 0.0))

    def load(engine, pid, retained, centre):
        return [{"uf_id": UF_OF[i], "idu": i, "geom_2154": GEOMS[i].wkb, "code_insee": i[:5], "section": i[8:10],
                 "numero": i[-4:], "surface_ha": GEOMS[i].area / 1e4, "miller": 0.7, "distance_km": 0.1}
                for ids in retained.values() for i in ids]
    monkeypatch.setattr(m, "load_uf_parcels", load)

    def attach(engine, pid, parcels, species, log):
        for p in parcels:
            p["fauna_distances"] = {"Crapaud": 100 if p["idu"].endswith("1") else 40}
            p["zone_humide_ha"] = 0.25
    monkeypatch.setattr(m, "attach_filter_fields", attach)

    def metrics(engine, pid, parcels, opts, force=False, log=None):
        by = {p["idu"]: [{"metric_key": "cesbio_profile",
                          "metric_value_jsonb": {"Prairies": {"pct": 100.0, "area_m2": 1}}}] for p in parcels}
        return "run-1", by
    monkeypatch.setattr(m, "ensure_uf_parcel_metrics", metrics)


def test_build_et_ecriture_gpkg(fake_db, tmp_path):
    exp = m.build_uf_export(None, "pid", RESULTS_UF)
    uf, parc = exp.unites_foncieres, exp.uf_parcelles
    assert list(uf["uf_id"]) == ["UF-A", "UF-B"]                      # ordre du classement
    a = uf.iloc[0]
    assert a["nb_parc"] == 2 and a["surf_ha"] == pytest.approx(2.0)
    assert a["zh_ha"] == pytest.approx(0.5)                           # somme
    assert a["espece_esp"] == "Crapaud" and "Crapaud: 40 m" in a["faune_txt"]   # minimum
    assert a["siren"] == "111" and a["score_eco"] == 5
    assert a["miller"] == pytest.approx(4 * 3.14159265 * 20000 / 600 ** 2, abs=1e-3)  # union 200x100
    assert uf.geometry.iloc[0].area == pytest.approx(20000)
    assert len(parc) == 3 and set(EXPORT_ATTR_KEYS) <= set(parc.columns)
    assert "presence" not in parc.columns and "noyau" not in parc.columns

    gpkg = tmp_path / "etude.gpkg"
    exp.unites_foncieres.to_file(gpkg, layer="unites_foncieres", driver="GPKG")
    exp.uf_parcelles.to_file(gpkg, layer="uf_parcelles", driver="GPKG", mode="a")
    m.write_attribute_table(gpkg, exp.dictionnaire, "dictionnaire")
    layers = dict(pyogrio.list_layers(gpkg))
    assert layers == {"unites_foncieres": "Polygon", "uf_parcelles": "Polygon", "dictionnaire": None}
    d = pyogrio.read_dataframe(gpkg, layer="dictionnaire", read_geometry=False)
    assert "regle_uf" in d.columns and (d["colonne"] == "zh_ha").any()
