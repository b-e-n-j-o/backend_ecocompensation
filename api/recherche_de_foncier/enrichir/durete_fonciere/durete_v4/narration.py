# -*- coding: utf-8 -*-
"""
Synthèse textuelle : le LLM EXPLIQUE un score déjà calculé, il ne le modifie pas.

- Contexte compact (facteurs tracés + signaux + constats web) → ~1-2 k tokens
  au lieu du JSON brut complet de V3 (historique RPG année par année, etc.).
- Consignes : n'utiliser que les faits fournis, citer les chiffres, signaler
  les incertitudes, pas de nom de personne physique.
- Repli sans LLM : synthèse déterministe construite à partir des facteurs,
  pour que le pipeline reste utilisable sans clé ni réseau.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from .scoring import ResultatScore

log = logging.getLogger("durete.narration")


def contexte_compact(profil: dict, sc: ResultatScore, web: Optional[dict]) -> dict:
    return {
        "personne_morale": {
            "denomination": profil.get("denomination"),
            "forme": profil.get("libelle_forme_juridique"),
            "naf": profil.get("naf"),
            "creation": profil.get("date_creation"),
            "siege": (profil.get("siege") or {}).get("commune"),
        },
        "score": {"final": sc.score_final, "niveau": sc.niveau_durete,
                  "confiance": f"{sc.confiance} ({sc.indice_confiance}/100)"},
        "axes": {k: {"points": f"{a.score}/{a.max}", "justification": a.note, "confiance": a.confiance}
                 for k, a in sc.axes.items()},
        "surcharges": [{"points": f.points, "motif": f.libelle} for f in sc.surcharges],
        "signaux": [s.libelle for s in sc.signaux],
        "recommandations_regles": sc.recommandations,
        "constats_web": [{"constat": c["constat"], "effet": c["effet_durete"], "date": c.get("date")}
                         for c in (web or {}).get("constats", [])],
        "avertissements": sc.avertissements[:5],
    }


def prompt_narration(ctx: dict) -> str:
    return f"""Tu es expert en maîtrise foncière pour la compensation écologique (bureau d'études Kerelia).
Le score de dureté foncière ci-dessous a été CALCULÉ par des règles déterministes : ne le recalcule pas,
ne le conteste pas, explique-le.

Données (seules sources autorisées) :
{json.dumps(ctx, ensure_ascii=False, indent=1)}

Consignes :
- 4 à 6 phrases, ton opérationnel, en français ; cite les éléments chiffrés qui pèsent le plus.
- N'invente aucun fait, aucun nom de personne. Si une donnée clé est incertaine (confiance faible), dis-le.
- Les constats web sont des signaux à vérifier, présente-les comme tels.

Réponds UNIQUEMENT en JSON :
{{"synthese": "...", "leviers": ["..."], "points_blocage": ["..."], "prochaine_action": "..."}}"""


def synthese_deterministe(profil: dict, sc: ResultatScore, web: Optional[dict]) -> dict:
    noms = {"axe1": "nature de la PM", "axe2": "gouvernance", "axe3": "situation financière",
            "axe4": "ancienneté de détention"}
    pesants = [f"{a.note} ({a.score}/{a.max})" for k, a in sc.axes.items()
               if a.confiance != "faible" and a.score / a.max >= 0.6]
    pesants += [f"{f.libelle} ({f.points:+d})" for f in sc.surcharges if f.points >= 5]
    incomplets = [noms[k] for k, a in sc.axes.items() if a.confiance == "faible"]
    phrases = [
        f"{profil.get('denomination') or profil.get('siren')} obtient {sc.score_final}/100 "
        f"({sc.niveau_durete}, confiance {sc.confiance} {sc.indice_confiance}/100).",
        ("Ce qui pèse : " + " ; ".join(pesants) + ".") if pesants else "Aucun facteur aggravant marqué.",
    ]
    if incomplets:
        phrases.append(f"Valeurs neutres faute de données sur : {', '.join(incomplets)}.")
    nb_web = len((web or {}).get("constats", []))
    if nb_web:
        phrases.append(f"{nb_web} constat(s) web sourcé(s) à vérifier.")
    leviers = [s.libelle for s in sc.signaux if any(m in s.code for m in ("AGE", "DEPRISE", "ESS", "BIO"))]
    leviers += [c["constat"] for c in (web or {}).get("constats", []) if c["effet_durete"] == "baisse"]
    blocages = [f.libelle for f in sc.surcharges] + \
               [c["constat"] for c in (web or {}).get("constats", []) if c["effet_durete"] == "hausse"]
    return {"synthese": " ".join(phrases), "leviers": leviers[:4], "points_blocage": blocages[:4],
            "prochaine_action": sc.recommandations[0] if sc.recommandations else "", "_genere_par": "regles"}


def rediger(llm, profil: dict, sc: ResultatScore, web: Optional[dict] = None) -> dict:
    if llm is None:
        return synthese_deterministe(profil, sc, web)
    try:
        out = llm.json(prompt_narration(contexte_compact(profil, sc, web)), max_tokens=1200)
        if not isinstance(out, dict) or not out.get("synthese"):
            raise ValueError("réponse sans champ 'synthese'")
        out.setdefault("leviers", [])
        out.setdefault("points_blocage", [])
        out.setdefault("prochaine_action", sc.recommandations[0] if sc.recommandations else "")
        out["_genere_par"] = getattr(llm, "nom", "llm")
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("Narration LLM en échec, repli déterministe : %s", e)
        return synthese_deterministe(profil, sc, web)
