import re

from api.recherche_de_foncier.filtrage.layers.filter_pipeline import (
    FilterConfig,
    FloraCriterion,
    filter_config_dict,
    flora_step_label,
)

_FLORA = re.compile(r"flore", re.I)
_FAUNA = re.compile(r"faune|esp[èe]ces?", re.I)


def test_filter_config_roundtrip_with_and_without_flora():
    empty = filter_config_dict(FilterConfig())
    assert empty["flora_criteria"] == []
    assert empty["flora_combine"] == "or"
    full = filter_config_dict(FilterConfig(
        flora_criteria=[FloraCriterion("nom:robinia pseudoacacia", "Robinia pseudoacacia", 200)],
        flora_combine="and",
    ))
    assert full["flora_criteria"] == [{
        "taxon_key": "nom:robinia pseudoacacia",
        "label": "Robinia pseudoacacia",
        "dist_m": 200,
    }]
    assert full["flora_combine"] == "and"
    assert "search_mode" in full


def test_flora_step_label():
    cfg = FilterConfig(
        flora_criteria=[
            FloraCriterion("nom:a", "A", 100),
            FloraCriterion("nom:b", "B", 50),
        ],
        flora_combine="or",
    )
    assert flora_step_label(cfg) == "Flore (2 taxon(s), OU)"
    cfg.flora_combine = "and"
    assert flora_step_label(cfg) == "Flore (2 taxon(s), ET)"


def test_step_matcher_flore_avant_faune():
    label = "Flore (1 taxon(s), OU)"
    assert _FLORA.search(label)
    assert _FAUNA.search(label) is None
    assert _FAUNA.search("Faune (2 espèce(s), ET)")
    # La regex faune attrape « espèces » : le matcher flore doit passer avant.
    assert _FAUNA.search("Présence d'espèces de flore")
    assert _FLORA.search("Présence d'espèces de flore")
