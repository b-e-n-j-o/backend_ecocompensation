"""Agrégation des parcelles d'une UF à partir de ses combinaisons."""
from api.recherche_de_foncier.filtrage.layers.uf_parcelles import summarize_uf_parcelles


def _uf():
    return {
        "uf_id": "UF1",
        "sous_ensembles": [
            {"subset_id": "a", "idus": ["p1", "p2"], "surface_ha": 4, "score_eco": {"total_score": 3, "max_score": 6}},
            {"subset_id": "b", "idus": ["p1", "p3", "p2"], "surface_ha": 6, "score_eco": {"total_score": 5, "max_score": 6}},
            {"subset_id": "c", "idus": ["p1"], "surface_ha": 1, "score_eco": {"total_score": 5, "max_score": 6}},
        ],
    }


def test_presence_noyau_et_tri():
    out = summarize_uf_parcelles(_uf(), {"p1": 2.5, "p2": 1.0, "p3": 4.0}, {"p1"})
    by_idu = {p["idu"]: p for p in out["parcelles"]}
    assert out["nb_combinaisons"] == 3
    assert out["nb_parcelles_retenues"] == 3
    assert out["nb_parcelles_noyau"] == 1
    assert by_idu["p1"]["noyau"] is True
    assert by_idu["p1"]["presence"] == 1.0
    assert by_idu["p1"]["in_pool"] is True
    assert by_idu["p2"]["nb_combinaisons"] == 2
    assert by_idu["p2"]["noyau"] is False
    assert by_idu["p3"]["presence"] == round(1 / 3, 4)
    assert by_idu["p3"]["in_pool"] is False
    assert [p["idu"] for p in out["parcelles"]] == ["p1", "p2", "p3"]
    assert out["surface_ha_parcelles"] == 7.5
    assert out["best_subset_id"] == "b"
    assert out["best_score_eco"]["total_score"] == 5


def test_sous_ensembles_vides():
    out = summarize_uf_parcelles({"sous_ensembles": []}, {}, set())
    assert out["parcelles"] == []
    assert out["nb_combinaisons"] == 0
    assert out["surface_ha_parcelles"] is None
    assert out["best_subset_id"] is None


def test_idu_compte_une_fois_par_combinaison():
    uf = {"sous_ensembles": [{"subset_id": "a", "idus": ["p1", "p1"], "surface_ha": 1}]}
    out = summarize_uf_parcelles(uf, {}, set())
    assert out["parcelles"][0]["nb_combinaisons"] == 1
    assert out["parcelles"][0]["noyau"] is True
