# -*- coding: utf-8 -*-
"""
Orchestrateur V4 : SIREN (+ IDU optionnels) → collecte → score déterministe →
recherche web (option) → synthèse.

Les sources sont injectables (`Sources`) : tests hors-ligne, ou branchement de
modules Kerelia existants (module urba GPU, table parcelles locale…).
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from . import config
from .cadastre import normaliser_idu, parse_idu
from .llm import get_llm
from .narration import rediger
from .scoring import calculer_score
from .sources import annuaire, bodacc, dvf, gpu, parcelles as src_parcelles, rpg, sirene
from .web_research import rechercher

log = logging.getLogger("durete.pipeline")


@dataclass
class Sources:
    fetch_annuaire: Callable[[str], dict] = annuaire.fetch_annuaire
    fetch_bodacc_records: Callable[[str], list] = bodacc.fetch_bodacc_records
    fetch_mutations: Callable[[str], dict] = dvf.fetch_mutations
    fetch_rpg: Callable[[str], dict] = rpg.fetch_rpg
    fetch_zonage: Callable[[str], dict] = gpu.fetch_zonage
    fetch_sirene: Callable[[str], dict] = sirene.fetch_sirene
    parcelles: Optional[object] = field(default_factory=src_parcelles.provider_par_defaut)


def siren_valide(siren: str) -> bool:
    """9 chiffres + clé de Luhn (exception connue : La Poste 356000000)."""
    if len(siren) != 9 or not siren.isdigit():
        return False
    if siren == "356000000":
        return True
    total = 0
    for i, c in enumerate(reversed(siren)):
        d = int(c) * (2 if i % 2 else 1)
        total += d - 9 if d > 9 else d
    return total % 10 == 0


def _analyser_parcelle(idu: str, sources: Sources, date_creation: Optional[str], millesime_pm: Optional[str],
                       avec_rpg: bool, avec_gpu: bool) -> dict:
    out: dict = {"idu": idu, "erreurs": []}
    try:
        out["dvf"] = sources.fetch_mutations(idu)
        out["detention"] = dvf.estimer_detention(out["dvf"], date_creation,
                                                 date_millesime_pm=millesime_pm).to_dict()
    except Exception as e:  # noqa: BLE001
        out["erreurs"].append(f"DVF : {e}")
        out["detention"] = {"statut": "inconnue", "signal": "DVF en erreur"}
    if avec_rpg:
        r = sources.fetch_rpg(idu) or {}
        out["rpg"] = r.get("summary")
        out["rpg_detail"] = r.get("by_year")
        if r.get("surface_parcelle_m2"):
            out["surface_m2_cadastre"] = r["surface_parcelle_m2"]
        if r.get("erreur"):
            out["erreurs"].append(f"RPG : {r['erreur']}")
    if avec_gpu:
        out["zonage"] = sources.fetch_zonage(idu)
    return out


def analyser_pm(
    siren: str,
    idus: Optional[list[str]] = None,
    *,
    avec_rpg: bool = True,
    avec_gpu: bool = True,
    avec_sirene: bool = False,
    avec_recherche_web: bool = False,
    avec_narration: bool = True,
    inclure_dirigeants_web: bool = False,
    llm_provider: Optional[str] = None,
    model: Optional[str] = None,
    llm=None,
    sources: Optional[Sources] = None,
    denomination_input: str = "",
    forme_juridique_input: str = "",
    domaine_public_confirme: Optional[list[str]] = None,
    max_workers: int = 4,
) -> dict:
    t0 = datetime.now()
    sources = sources or Sources()
    siren = "".join(str(siren).split())
    log.info(
        "DURETE V4 START | siren=%s idus_in=%s rpg=%s gpu=%s web=%s narration=%s",
        siren, len(idus or []), avec_rpg, avec_gpu, avec_recherche_web, avec_narration,
    )
    if not siren_valide(siren):
        return _erreur(siren, idus or [], f"SIREN invalide : '{siren}' (9 chiffres + clé de Luhn)", t0)
    avert: list[str] = []

    # 1. Parcelles (input > fichier PM) + patrimoine + droits
    prov = sources.parcelles
    patrimoine, droits = None, {}
    try:
        rows_siren = prov.par_siren(siren) if prov else []
        patrimoine = src_parcelles.analyser_patrimoine(rows_siren, siren) if rows_siren else None
    except Exception as e:  # noqa: BLE001
        rows_siren = []
        avert.append(f"Fichier PM indisponible : {e}")
    if idus:
        idus_ok = []
        for i in idus:
            try:
                idus_ok.append(normaliser_idu(i))
            except ValueError as e:
                avert.append(str(e))
        idus = list(dict.fromkeys(idus_ok))
    else:
        idus = (patrimoine or {}).get("idus") or []
    if not idus:
        log.info("DURETE V4 STOP | siren=%s raison=aucun IDU", siren)
        return _erreur(siren, [], "Aucun IDU disponible (ni en entrée ni dans le fichier des PM)", t0)
    try:
        rows_idus = prov.par_idus(idus) if prov else []
        droits = src_parcelles.analyser_droits(rows_idus, siren, idus) if prov else {}
    except Exception as e:  # noqa: BLE001
        avert.append(f"Droits par parcelle indisponibles : {e}")

    # 2. Identité + BODACC (+ Sirene optionnel)
    raw = sources.fetch_annuaire(siren)
    profil = annuaire.normaliser_annuaire(raw, siren) if raw else \
        annuaire.profil_minimal(siren, denomination_input or (patrimoine or {}).get("denomination") or "",
                                forme_juridique_input)
    if not raw:
        avert.append("Annuaire des Entreprises indisponible — profil reconstitué depuis l'entrée (confiance réduite)")
    bod = bodacc.analyser_bodacc(sources.fetch_bodacc_records(siren), siren)
    sir = sources.fetch_sirene(siren) if avec_sirene else {}

    log.info(
        "DURETE V4 IDENTITE | siren=%s denomination=%s forme=%s annuaire=%s bodacc=%s",
        siren,
        profil.get("denomination"),
        profil.get("nature_juridique") or profil.get("libelle_forme_juridique"),
        "ok" if raw else "absent",
        (bod or {}).get("nb_annonces") if isinstance(bod, dict) else None,
    )

    # 3. Parcelles en parallèle (DVF + RPG + GPU)
    millesime = (patrimoine or {}).get("millesime")
    millesime_date = f"{millesime[:4]}-01-01" if millesime and millesime[:4].isdigit() else None
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        analyses = list(ex.map(lambda i: _analyser_parcelle(i, sources, profil.get("date_creation"),
                                                             millesime_date, avec_rpg, avec_gpu), idus))
    surfaces = (patrimoine or {}).get("surfaces") or {}
    for a in analyses:
        a["droits"] = droits.get(a["idu"])
        a["surface_m2"] = surfaces.get(a["idu"]) or (a["droits"] or {}).get("surface_m2") or a.get("surface_m2_cadastre")
        avert += [f"[{a['idu']}] {e}" for e in a.pop("erreurs")]

    # 4. Score déterministe
    sc = calculer_score(profil, bod, analyses, patrimoine,
                        {"avec_rpg": avec_rpg, "avec_gpu": avec_gpu,
                         "domaine_public_confirme": domaine_public_confirme or []})

    log.info(
        "DURETE V4 SCORE | siren=%s score=%s niveau=%s confiance=%s fiable=%s",
        siren, sc.score_final, sc.niveau_durete, sc.indice_confiance, sc.indice_confiance >= 40,
    )

    # 5. Recherche web + synthèse (LLM optionnel, jamais dans le score)
    client = llm if llm is not None else (get_llm(llm_provider, model)
                                         if (avec_recherche_web or avec_narration) else None)
    web = rechercher(client, profil, sorted({parse_idu(i).insee for i in idus}),
                     inclure_dirigeants_web) if avec_recherche_web else None
    if avec_recherche_web:
        constats = (web or {}).get("constats") or []
        log.info(
            "DURETE V4 WEB | siren=%s statut=%s constats=%d raison=%s",
            siren, (web or {}).get("statut"), len(constats), (web or {}).get("raison") or "",
        )
        for c in constats:
            log.info(
                "DURETE V4 WEB CONSTAT | siren=%s effet=%s categorie=%s date=%s | %s",
                siren, c.get("effet_durete"), c.get("categorie"), c.get("date"), c.get("constat"),
            )
    synth = rediger(client if avec_narration else None, profil, sc, web)
    out = _assembler(siren, idus, profil, bod, sir, patrimoine, sc, web, synth, avert, t0)
    log.info(
        "DURETE V4 DONE | siren=%s statut=%s score=%s duree_s=%s avertissements=%d",
        siren, out.get("statut"), out.get("score_final"), out.get("duree_s"), len(out.get("avertissements") or []),
    )
    return out


def _assembler(siren, idus, profil, bod, sir, patrimoine, sc, web, synth, avert, t0) -> dict:
    t1 = datetime.now()
    d = sc.to_dict()
    v4 = {
        "scoring_version": sc.scoring_version,
        "indice_confiance": sc.indice_confiance,
        "confiance": sc.confiance,
        "axes": d["axes"],
        "surcharges": d["surcharges"],
        "signaux": d["signaux"],
        "recommandations": sc.recommandations,
        "synthese": synth,
        "recherche_web": web,
    }
    detail = {**sc.detail_axes_compat(), "v4": v4}
    return {
        # --- format V3 (compatibilité front / cache / JSONL) ---
        "siren": siren,
        "denomination": profil.get("denomination") or siren,
        "idus": idus,
        "nb_parcelles": len(idus),
        "statut": "ok",
        "score_final": sc.score_final,
        "niveau_durete": sc.niveau_durete,
        "explication": synth.get("synthese", ""),
        "detail_axes": detail,
        "avertissements": avert + sc.avertissements,
        "ts_debut": t0.isoformat(),
        "ts_fin": t1.isoformat(),
        "duree_s": round((t1 - t0).total_seconds(), 1),
        # --- ajouts V4 ---
        "version": "4",
        "forme_juridique": profil.get("nature_juridique"),
        "indice_confiance": sc.indice_confiance,
        "confiance": sc.confiance,
        # < 40 : trop de sources muettes, le score est surtout fait de valeurs neutres → ne pas classer dessus
        "score_fiable": sc.indice_confiance >= 40,
        "recommandations": sc.recommandations,
        "leviers": synth.get("leviers", []),
        "points_blocage": synth.get("points_blocage", []),
        "prochaine_action": synth.get("prochaine_action"),
        "profil": {k: v for k, v in profil.items() if k not in ("dirigeants",)},
        "bodacc": bod,
        "sirene": sir or None,
        "patrimoine": {k: v for k, v in (patrimoine or {}).items() if k not in ("idus", "surfaces")} or None,
        "parcelles": [
            {k: p.get(k) for k in ("idu", "surface_m2", "detention", "axe4", "rpg", "bail", "zonage", "droits")}
            for p in d["parcelles"]
        ],
        "recherche_web": web,
    }


def _erreur(siren: str, idus: list, msg: str, t0: datetime) -> dict:
    return {"siren": siren, "idus": idus, "statut": "erreur", "erreur": msg, "ts": t0.isoformat(), "version": "4"}
