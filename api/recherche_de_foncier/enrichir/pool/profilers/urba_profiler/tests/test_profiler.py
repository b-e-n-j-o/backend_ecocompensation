"""
Tests hors ligne : un faux client WFS sert des entités synthétiques en 2154.
Lancer : pytest -q tests/
"""

from __future__ import annotations

from typing import Dict, List

import pytest
from shapely.geometry import LineString, Point, Polygon, box, mapping

from urba_profiler import ProfilerSettings, UrbaProfilRequest, run_urba_profile
from urba_profiler.config import default_layer_keys
from urba_profiler.nomenclature import lookup, parse_idurba
from urba_profiler.wfs_client import FetchStats, Feature, WfsClient, WfsError

X0, Y0 = 425_000.0, 6_420_000.0  # vers Latresne, Lambert-93
P1 = box(X0, Y0, X0 + 100, Y0 + 100)  # 10 000 m²
P2 = box(X0 + 100, Y0, X0 + 200, Y0 + 100)  # contiguë à P1


def F(fid, geom, **props) -> Feature:
    return Feature(fid=fid, props={k.lower(): v for k, v in props.items()}, geom=geom)


DOC = dict(idurba="33234_PLU_20190702", partition="DU_33234", insee="33234", datappro="20190702")

FAKE: Dict[str, List[Feature]] = {
    "wfs_du:zone_urba": [
        F("z_a", box(X0 - 50, Y0 - 50, X0 + 80, Y0 + 150), libelle="A", typezone="A", urlfic="https://ex/reg.pdf#page=40", **DOC),
        # zone N découpée en 2 polygones -> doit être fusionnée en 1 élément
        F("z_n1", box(X0 + 80, Y0 - 50, X0 + 300, Y0 + 50), libelle="Nzh", typezone="N", **DOC),
        F("z_n2", box(X0 + 80, Y0 + 50, X0 + 300, Y0 + 150), libelle="Nzh", typezone="N", **DOC),
        # zone U qui ne fait que "mordre" de 0,3 m : décalage cadastre -> écartée
        F("z_u", box(X0 - 100, Y0 - 50, X0 + 0.3, Y0 + 150), libelle="UB", typezone="U", **DOC),
    ],
    "wfs_du:prescription_surf": [
        F("p_ebc", box(X0 + 10, Y0 + 10, X0 + 14, Y0 + 15), libelle="EBC", typepsc="01", stypepsc="00", **DOC),  # 20 m² < seuils
        F("p_er", box(X0, Y0 + 90, X0 + 100, Y0 + 100), libelle="ER n°3 élargissement voie", txt="ER3", typepsc="05", stypepsc="01", **DOC),
        F("p_bad", box(X0 + 20, Y0 + 20, X0 + 60, Y0 + 60), libelle="", typepsc="77", stypepsc="00", **DOC),
    ],
    "wfs_du:prescription_lin": [
        # haie protégée le long de la limite sud, 0,2 m à l'extérieur -> "limite"
        F("l_haie", LineString([(X0, Y0 - 0.2), (X0 + 100, Y0 - 0.2)]), libelle="Haie à préserver", typepsc="07", stypepsc="04", **DOC),
        F("l_tvb", LineString([(X0 + 50, Y0 - 20), (X0 + 50, Y0 + 120)]), libelle="Corridor", typepsc="25", stypepsc="00", **DOC),
    ],
    "wfs_du:prescription_pct": [],
    "wfs_du:info_surf": [
        F("i_dpu", box(X0 - 500, Y0 - 500, X0 + 500, Y0 + 500), libelle="DPU", typeinf="04", stypeinf="00", **DOC),
        # à 30 m au nord : uniquement en voisinage
        F("i_zad", box(X0, Y0 + 130, X0 + 100, Y0 + 200), libelle="ZAD", typeinf="05", stypeinf="00", **DOC),
    ],
    "wfs_du:info_pct": [F("i_archeo", Point(X0 + 30, Y0 + 30), libelle="Site", typeinf="99", stypeinf="00", **DOC)],
    "wfs_du:secteur_cc": [],
    "wfs_sup:assiette_sup_s": [
        F("s_ac1", box(X0 - 400, Y0 - 400, X0 + 400, Y0 + 400), suptype="ac1", nomsuplitt="Monuments historiques",
          nomass="Périmètre église", idass="A1", urlreg="https://ex/arrete.pdf"),
    ],
    "wfs_sup:assiette_sup_l": [],
    "wfs_sup:assiette_sup_p": [],
}


class FakeClient:
    def __init__(self, fail: tuple = ()):
        self.stats = FetchStats()
        self.fail = fail
        self.calls = []

    def get_features(self, typename, bbox):
        self.calls.append(typename)
        self.stats.requests += 1
        if typename in self.fail:
            raise WfsError(f"{typename} : timeout simulé")
        b = box(*bbox)
        return [f for f in FAKE.get(typename, []) if f.geom.intersects(b)]


def _req(**opts):
    return UrbaProfilRequest(
        parcelles=[
            {"id": "P1", "geometry": mapping(P1), "srid": 2154, "meta": {"commune": "Latresne"}},
            {"id": "P2", "geometry": mapping(P2), "srid": 2154},
        ],
        unites_foncieres=[{"id": "UF1", "parcelle_ids": ["P1", "P2"]}],
        options=opts,
    )


def _couche(cible, key):
    return next(c for c in cible.couches if c.layer_key == key)


@pytest.fixture(scope="module")
def res():
    fast = ProfilerSettings(retry_pass_wait_s=0, layer_cooldown_s=0)
    return run_urba_profile(
        _req(voisinage_m=50),
        settings=fast,
        client=FakeClient(fail=("wfs_du:info_lin",)),
    )


def test_zonage_fusion_et_contact(res):
    z = _couche(res.parcelles["P1"], "zonage_plu")
    assert z.status == "intersected"
    libs = {e.attributs["libelle"]: e for e in z.elements}
    assert set(libs) == {"A", "Nzh"}
    assert libs["A"].mesure.pct_cible == pytest.approx(80.0, abs=0.1)
    assert libs["Nzh"].mesure.pct_cible == pytest.approx(20.0, abs=0.1)
    assert libs["Nzh"].nb_entites == 2  # 2 polygones -> 1 élément
    assert libs["A"].libelle_cnig == "Zone agricole"
    assert libs["A"].liens[0].type == "reglement"
    # UB ne fait que toucher la limite -> écartée avec raison
    assert [e.attributs["libelle"] for e in z.ecartes] == ["UB"]
    assert z.ecartes[0].raison_ecart == "contact_limite"


def test_prescriptions_seuils_codes(res):
    ps = _couche(res.parcelles["P1"], "prescriptions_surf")
    kept = {e.code: e for e in ps.elements}
    assert "05-01" in kept and kept["05-01"].libelle_cnig == "Emplacement réservé aux voies publiques"
    assert kept["05-01"].mesure.pct_cible == pytest.approx(10.0, abs=0.1)
    # code hors nomenclature + libellé vide -> flags
    assert "77-00" in kept
    assert {"code_hors_nomenclature", "libelle_vide"} <= set(kept["77-00"].flags)
    # EBC de 20 m² : sous le seuil -> écarté (mais visible)
    assert [e.code for e in ps.ecartes] == ["01-00"]
    assert ps.ecartes[0].raison_ecart.startswith("sous_seuil")


def test_lineaires_limite_et_interieur(res):
    pl = _couche(res.parcelles["P1"], "prescriptions_lin")
    by = {e.code: e for e in pl.elements}
    assert by["07-04"].mesure.position == "limite"
    assert by["07-04"].mesure.distance_m == pytest.approx(0.2, abs=0.01)
    assert by["25-00"].mesure.position == "interieur"
    assert by["25-00"].mesure.longueur_m == pytest.approx(100.0, abs=0.1)


def test_infos_voisinage_ponctuel_99(res):
    inf = _couche(res.parcelles["P1"], "infos_surf")
    assert [e.code for e in inf.elements] == ["04-00"]
    assert inf.elements[0].mesure.pct_cible == 100.0
    assert [e.code for e in inf.voisinage] == ["05-00"]
    assert inf.voisinage[0].mesure.distance_m == pytest.approx(30.0, abs=0.1)
    pct = _couche(res.parcelles["P1"], "infos_pct")
    assert pct.elements[0].mesure.nb_points == 1
    assert "code_generique_99" in pct.elements[0].flags


def test_sup_et_erreur_couche(res):
    sup = _couche(res.parcelles["P1"], "sup_assiette_s")
    assert sup.elements[0].attributs["suptype"] == "ac1"
    assert sup.elements[0].liens[0].type == "acte_sup"
    err = _couche(res.parcelles["P1"], "infos_lin")
    assert err.status == "error" and "timeout" in err.error
    assert "couches_en_erreur" in res.parcelles["P1"].flags
    assert res.meta.errors


def test_documents_synthese(res):
    c = res.parcelles["P1"]
    assert c.documents[0].idurba == "33234_PLU_20190702"
    assert c.documents[0].typedoc == "PLU" and c.documents[0].date == "2019-07-02"
    assert c.synthese.zonages[0].libelle == "A"
    assert "zonage_multiple" in c.flags
    assert c.meta == {"commune": "Latresne"}


def test_unite_fonciere(res):
    uf = res.unites_foncieres["UF1"]
    assert uf.surface_m2 == pytest.approx(20_000.0)
    assert uf.parcelle_ids == ["P1", "P2"]
    z = {e.attributs["libelle"]: e.mesure.pct_cible for e in _couche(uf, "zonage_plu").elements}
    assert z["Nzh"] == pytest.approx(60.0, abs=0.1)  # 20 % de P1 + 100 % de P2
    assert z["A"] == pytest.approx(40.0, abs=0.1)
    assert "uf_non_contigue" not in uf.flags
    # P2 : 100 % Nzh
    assert res.parcelles["P2"].synthese.zonages[0].pct_cible == 100.0


def test_un_seul_appel_par_couche_et_cluster():
    cl = FakeClient()
    r = run_urba_profile(_req(), client=cl)
    assert r.meta.nb_clusters == 1
    assert len(cl.calls) == len(r.meta.layers)  # 1 requête / couche pour les 2 parcelles


def test_geometries_4326_et_options():
    from urba_profiler.geometry import to_geojson

    req = UrbaProfilRequest(
        parcelles=[{"id": "P1", "geometry": to_geojson(P1), "srid": 4326}],
        options={"layers": ["zonage_plu"], "include_geometries": True},
    )
    r = run_urba_profile(req, client=FakeClient())
    z = r.parcelles["P1"].couches
    assert [c.layer_key for c in z] == ["zonage_plu"]
    assert z[0].elements[0].geometry["type"] in ("Polygon", "MultiPolygon")
    assert r.parcelles["P1"].surface_m2 == pytest.approx(10_000, rel=1e-3)


def test_couche_inconnue():
    with pytest.raises(ValueError):
        run_urba_profile(_req(layers=["nope"]), client=FakeClient())


def test_nomenclature():
    assert lookup("psc", "5", "4")[1] == "Emplacement réservé aux espaces verts/continuités écologiques"
    assert lookup("inf", "03", None)[1] == "Zone de préemption dans un espace naturel et sensible"
    code, lab, flags = lookup("psc", "01", "42")
    assert code == "01-42" and lab == "Espace boisé classé" and "sous_type_hors_nomenclature" in flags
    assert lookup("psc", None)[2] == ["code_absent"]
    assert parse_idurba("200070761_PLUI_20230115")["typedoc"] == "PLUI"
    assert parse_idurba("n'importe quoi")["typedoc"] is None


# --- Client WFS : subdivision, dédup, ordre des axes -------------------------


def _gj(fid, lon, lat):
    d = 0.0005
    return {"type": "Feature", "id": fid, "properties": {"LIBELLE": "A"},
            "geometry": {"type": "Polygon", "coordinates": [[[lon, lat], [lon + d, lat], [lon + d, lat + d], [lon, lat + d], [lon, lat]]]}}


def test_wfs_subdivision_dedup_axes(monkeypatch):
    import urba_profiler.wfs_client as w

    monkeypatch.setattr(w, "WFS_MAX_FEATURES", 3)
    from urba_profiler.wfs_client import ResponseCache

    cl = WfsClient(ProfilerSettings(), cache=ResponseCache(ttl_s=0))
    calls = {"n": 0}

    def fake_get_json(params):
        calls["n"] += 1
        if calls["n"] == 1:  # tuile racine saturée -> doit subdiviser
            return {"features": [_gj(f"f{i}", -0.5, 44.8) for i in range(3)]}
        # sous-tuiles : même entité renvoyée 4 fois (dédup) ; 2e en lat/lon inversé
        return {"features": [_gj("x1", -0.5, 44.8),
                             {**_gj("x2", 44.8, -0.5), "id": "x2"}]}

    monkeypatch.setattr(cl, "_get_json", fake_get_json)
    feats = cl.get_features("wfs_du:zone_urba", (420000, 6410000, 430000, 6430000))
    assert calls["n"] == 5  # 1 racine + 4 sous-tuiles
    assert sorted(f.fid for f in feats) == ["x1", "x2"]
    for f in feats:  # les deux sont bien en Lambert-93 autour de Bordeaux
        x, y = f.geom.centroid.coords[0]
        assert 400_000 < x < 450_000 and 6_400_000 < y < 6_450_000
    assert feats[0].props["libelle"] == "A"  # clés en minuscules


def test_wfs_params():
    cl = WfsClient(ProfilerSettings())
    p = cl._params("wfs_du:zone_urba", (425000, 6420000, 425100, 6420100))
    assert p["typeNames"] == "wfs_du:zone_urba" and p["outputFormat"] == "application/json"
    lon0, lat0, lon1, lat1, srs = p["bbox"].split(",")
    assert srs == "EPSG:4326" and -1 < float(lon0) < 0 and 44 < float(lat0) < 45


def test_router_fastapi(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import urba_profiler.service as svc
    from urba_profiler.router import router

    monkeypatch.setattr(svc, "WfsClient", lambda s: FakeClient())
    app = FastAPI()
    app.include_router(router, prefix="/api/foncier")
    tc = TestClient(app)
    r = tc.post("/api/foncier/urba/profil", json=_req().model_dump())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["parcelles"]["P1"]["couches"][0]["layer_key"] == "zonage_plu"
    assert tc.get("/api/foncier/urba/couches").status_code == 200
    assert tc.post("/api/foncier/urba/profil", json={**_req().model_dump(), "options": {"layers": ["x"]}}).status_code == 422


def _fast(**kwargs):
    base = dict(retry_pass_wait_s=0, layer_cooldown_s=0, min_interval_s=0)
    base.update(kwargs)
    return ProfilerSettings(**base)


def _clock(monkeypatch):
    import urba_profiler.wfs_client as w

    clock = {"t": 0.0}
    sleeps: List[float] = []

    def fake_sleep(dt):
        sleeps.append(float(dt))
        clock["t"] += float(dt)

    monkeypatch.setattr(w.time, "sleep", fake_sleep)
    monkeypatch.setattr(w.time, "monotonic", lambda: clock["t"])
    w._GPU_GATE._next_ok = 0.0
    w._GPU_GATE._cooldown_until = 0.0
    return sleeps


class _Resp:
    def __init__(self, status, payload=None, headers=None, text=""):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_couches_par_defaut_sans_flux_vides():
    keys = default_layer_keys()
    assert "secteur_cc" not in keys
    assert "sup_assiette_p" not in keys
    assert len(keys) == 9


def test_deux_parcelles_meme_cellule_une_requete_par_couche():
    cl = FakeClient()
    r = run_urba_profile(_req(), settings=_fast(), client=cl)
    assert r.meta.nb_clusters == 1
    assert len(cl.calls) == 9
    assert r.meta.wfs_requests_planned == 9


def test_quarante_parcelles_fusion_de_clusters():
    """40 parcelles par paires à cheval sur une limite de cellule : la fusion divise par deux."""
    parcelles = []
    for i in range(20):
        boundary = 3000 + i * 6000
        for dx in (-80, 80):
            x = boundary + dx
            g = box(x, 0, x + 10, 10)
            parcelles.append({"id": f"p{i}_{dx}", "geometry": mapping(g), "srid": 2154})
    cl = FakeClient()
    r = run_urba_profile(UrbaProfilRequest(parcelles=parcelles), settings=_fast(), client=cl)
    assert r.meta.nb_clusters == 20
    assert r.meta.nb_clusters < 40
    assert len(cl.calls) == 20 * 9
    assert r.meta.wfs_requests_planned == 180


def test_echec_limite_au_cluster(monkeypatch):
    import urba_profiler.service as svc

    monkeypatch.setattr(svc.time, "sleep", lambda _dt: None)

    class Selective(FakeClient):
        def get_features(self, typename, bbox):
            self.calls.append(typename)
            self.stats.requests += 1
            if typename == "wfs_du:zone_urba" and bbox[0] > 5_000:
                raise WfsError("zone loin")
            return []

    near = box(0, 0, 40, 40)
    far = box(20_000, 0, 20_040, 40)
    req = UrbaProfilRequest(parcelles=[
        {"id": "near", "geometry": mapping(near), "srid": 2154},
        {"id": "far", "geometry": mapping(far), "srid": 2154},
    ])
    r = run_urba_profile(req, settings=_fast(), client=Selective())
    assert _couche(r.parcelles["near"], "zonage_plu").status == "not_intersected"
    assert _couche(r.parcelles["far"], "zonage_plu").status == "error"
    assert _couche(r.parcelles["far"], "prescriptions_surf").status == "not_intersected"
    assert r.meta.failed_tasks
    assert r.meta.failed_tasks[0]["layer_key"] == "zonage_plu"


def test_disjoncteur_trois_echecs(monkeypatch):
    import urba_profiler.service as svc

    sleeps: List[float] = []
    monkeypatch.setattr(svc.time, "sleep", lambda dt: sleeps.append(float(dt)))

    class Always(FakeClient):
        def get_features(self, typename, bbox):
            self.calls.append(typename)
            if typename == "wfs_du:zone_urba":
                raise WfsError("down")
            return []

    parcelles = []
    for i in range(4):
        x = i * 10_000
        g = box(x, 0, x + 20, 20)
        parcelles.append({"id": f"p{i}", "geometry": mapping(g), "srid": 2154})
    run_urba_profile(
        UrbaProfilRequest(parcelles=parcelles),
        settings=_fast(layer_cooldown_s=60),
        client=Always(),
    )
    assert 60.0 in sleeps


def test_limiteur_cinq_requetes(monkeypatch):
    import urba_profiler.wfs_client as w

    sleeps = _clock(monkeypatch)
    for _ in range(5):
        w._GPU_GATE.wait_turn(1.0)
    assert sum(sleeps) >= 4.0


def test_503_puis_succes_compte_un_retry(monkeypatch):
    from urba_profiler.wfs_client import ResponseCache, WfsClient

    _clock(monkeypatch)
    calls = {"n": 0}

    def fake_get(self, url, params=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp(503, text="down")
        return _Resp(200, {"type": "FeatureCollection", "features": []})

    import urba_profiler.wfs_client as w
    monkeypatch.setattr(w.requests.Session, "get", fake_get)
    cl = WfsClient(_fast(), cache=ResponseCache(ttl_s=0))
    assert cl.get_features("wfs_du:zone_urba", (425000, 6420000, 426000, 6421000)) == []
    assert cl.stats.retries >= 1
    assert calls["n"] == 2


def test_429_respecte_retry_after(monkeypatch):
    from urba_profiler.wfs_client import ResponseCache, WfsClient
    import urba_profiler.wfs_client as w

    sleeps = _clock(monkeypatch)
    calls = {"n": 0}

    def fake_get(self, url, params=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp(429, headers={"Retry-After": "2"}, text="slow")
        return _Resp(200, {"type": "FeatureCollection", "features": []})

    monkeypatch.setattr(w.requests.Session, "get", fake_get)
    cl = WfsClient(_fast(), cache=ResponseCache(ttl_s=0))
    cl.get_features("wfs_du:zone_urba", (425000, 6420000, 426000, 6421000))
    assert max(sleeps) >= 2.0


def test_400_sans_nouvelle_tentative(monkeypatch):
    from urba_profiler.wfs_client import ResponseCache, WfsClient
    import urba_profiler.wfs_client as w

    _clock(monkeypatch)
    calls = {"n": 0}

    def fake_get(self, url, params=None, timeout=None):
        calls["n"] += 1
        return _Resp(400, text="bad bbox")

    monkeypatch.setattr(w.requests.Session, "get", fake_get)
    cl = WfsClient(_fast(), cache=ResponseCache(ttl_s=60))
    with pytest.raises(WfsError) as exc:
        cl.get_features("wfs_du:zone_urba", (425000, 6420000, 426000, 6421000))
    assert exc.value.failure.type == "http"
    assert exc.value.failure.status == 400
    assert cl.stats.retries == 0
    assert calls["n"] == 1
    with pytest.raises(WfsError):
        cl.get_features("wfs_du:zone_urba", (425000, 6420000, 426000, 6421000))
    assert calls["n"] == 2
