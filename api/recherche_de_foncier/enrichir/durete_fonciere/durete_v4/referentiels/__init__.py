# -*- coding: utf-8 -*-
"""Chargement (mis en cache) des nomenclatures INSEE et RPG."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).parent


@lru_cache(maxsize=1)
def categories_n2() -> dict[str, dict]:
    with open(_DIR / "categories_juridiques_niveau_2.json", encoding="utf-8") as f:
        return {
            str(e["code_juridique"]).zfill(2): {
                "libelle": e["categorie_juridique"],
                "score_axe1": int(e.get("score_axe1", 20)),
                "note": e.get("note_scoring", ""),
            }
            for e in json.load(f)
        }


@lru_cache(maxsize=1)
def categories_n3() -> dict[str, str]:
    with open(_DIR / "categories_juridiques_niveau_3.json", encoding="utf-8") as f:
        return {str(e["code_juridique"]).zfill(4): e["categorie_juridique"] for e in json.load(f)}


def libelle_forme_juridique(code4: str | None) -> str:
    if not code4:
        return "Forme juridique inconnue"
    code4 = str(code4).strip().zfill(4)
    if code4 in categories_n3():
        return categories_n3()[code4]
    n2 = categories_n2().get(code4[:2])
    return f"{n2['libelle']} (code {code4})" if n2 else f"Forme juridique inconnue (code {code4})"


@lru_cache(maxsize=1)
def rpg_nomenclature() -> dict[str, dict[str, str]]:
    with open(_DIR / "rpg_nomenclature.json", encoding="utf-8") as f:
        raw = json.load(f)
    clean = lambda d: {k: " ".join(str(v).split()) for k, v in d.items()}  # noqa: E731
    return {k: clean(raw.get(k, {})) for k in ("code_cultu", "code_group", "culture_d1")}


def libelle_culture(code: str) -> str:
    nom = rpg_nomenclature()
    return nom["code_cultu"].get(code) or nom["code_group"].get(code) or code
