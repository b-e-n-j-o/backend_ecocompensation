# -*- coding: utf-8 -*-
"""
BODACC — annonces commerciales (API Opendatasoft Explore v2.1, gratuite, sans clé).

Corrections vs V3 :
  - API v1 (`/api/records/1.0/search`) → v2.1 (`/api/explore/v2.1/.../records`) ;
  - la recherche full-text sur le SIREN ramène aussi des annonces d'AUTRES sociétés
    qui le mentionnent (acquéreur d'un fonds, associé PM…) → filtrage strict sur
    `registre` / numéro d'immatriculation de la personne ;
  - procédures collectives : la famille réelle est `collective` (et non
    "liquidation"/"redressement"/"sauvegarde") ; la nature du jugement est dans le
    champ JSON `jugement` → V3 ne détectait AUCUNE procédure ;
  - l'état est déterminé sur l'annonce la plus récente (V3 dépendait de l'ordre
    de parcours : une création pouvait écraser une radiation) ;
  - une procédure clôturée ou un plan adopté n'est plus traité comme une
    liquidation en cours ;
  - nouveaux signaux : dissolution / liquidation amiable, dépôts des comptes,
    modifications récentes (gérance, siège…).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from typing import Any, Optional

from .. import config, net

log = logging.getLogger("durete.bodacc")

_CHAMPS_JSON = ("listepersonnes", "jugement", "depot", "modificationsgenerales",
                "radiationaurcs", "listeprecedentproprietaire", "acte")


def fetch_bodacc_records(siren: str, limit: int = 100) -> list[dict]:
    """Annonces brutes contenant le SIREN (non filtrées). [] si erreur."""
    status, data = net.get_json(
        config.BODACC_URL,
        {"where": f'"{siren}"', "order_by": "dateparution desc", "limit": limit},
        ttl=6 * 3600,
    )
    if status != 200 or not data:
        log.warning("BODACC indisponible pour %s (HTTP %s)", siren, status)
        return []
    return data.get("results") or []


def _loads(v: Any) -> Any:
    if isinstance(v, str) and v[:1] in "{[":
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _digits(s: Any) -> str:
    return re.sub(r"\D", "", str(s or ""))


def _personnes(rec: dict) -> list[dict]:
    lp = _loads(rec.get("listepersonnes"))
    if isinstance(lp, dict):
        p = lp.get("personne", lp)
        return p if isinstance(p, list) else [p]
    if isinstance(lp, list):
        return [x.get("personne", x) if isinstance(x, dict) else {} for x in lp]
    return []


def concerne_siren(rec: dict, siren: str) -> bool:
    reg = rec.get("registre")
    regs = reg if isinstance(reg, list) else str(reg or "").split(",")
    if any(_digits(r) == siren for r in regs):
        return True
    for p in _personnes(rec):
        num = (p.get("numeroImmatriculation") or {})
        if isinstance(num, dict) and _digits(num.get("numeroIdentification")) == siren:
            return True
    return False


def _famille(rec: dict) -> str:
    f = (rec.get("familleavis") or "").lower()
    lib = (rec.get("familleavis_lib") or "").lower()
    if f == "collective" or "procédures collectives" in lib or "procedures collectives" in lib:
        return "collective"
    if f in ("creation", "immatriculation", "modification", "radiation", "vente", "dpc", "conciliation"):
        return f
    if "radiation" in lib:
        return "radiation"
    if "dépôt" in lib or "depot" in lib:
        return "dpc"
    if "modification" in lib:
        return "modification"
    return f or lib or "autre"


def _texte_jugement(rec: dict) -> str:
    j = _loads(rec.get("jugement"))
    if isinstance(j, dict):
        return " ".join(str(j.get(k) or "") for k in ("famille", "nature", "complementJugement"))
    return str(j or "")


def classer_jugement(texte: str) -> tuple[str, Optional[str]]:
    """→ (statut, type) avec statut ∈ en_cours|plan|cloturee|autre."""
    t = texte.lower()
    if "clôture" in t or "cloture" in t:
        return "cloturee", ("liquidation" if "liquidation" in t else None)
    if "plan de cession" in t:
        return "en_cours", "cession"
    if "arrêtant le plan" in t or "arretant le plan" in t or "homologation du plan" in t \
            or "plan de redressement" in t or "plan de sauvegarde" in t:
        if "résolution" in t or "resolution" in t:
            return "en_cours", "liquidation" if "liquidation" in t else "redressement"
        return "plan", "sauvegarde" if "sauvegarde" in t else "redressement"
    if "liquidation judiciaire" in t:
        return "en_cours", "liquidation"
    if "redressement judiciaire" in t:
        return "en_cours", "redressement"
    if "sauvegarde" in t:
        return "en_cours", "sauvegarde"
    return "autre", None


_RE_DISSOLUTION = re.compile(r"dissolution|liquidation amiable|mise en liquidation", re.I)


def _descriptif_modif(rec: dict) -> str:
    m = _loads(rec.get("modificationsgenerales"))
    if isinstance(m, dict):
        return str(m.get("descriptif") or "")
    return str(m or "")


def _capital(rec: dict) -> Optional[float]:
    for p in _personnes(rec):
        cap = p.get("capital") or {}
        val = cap.get("montantCapital") if isinstance(cap, dict) else None
        if val:
            try:
                return float(str(val).replace(" ", "").replace(",", "."))
            except ValueError:
                continue
    return None


def _date(rec: dict) -> str:
    return str(rec.get("dateparution") or "")[:10]


def analyser_bodacc(records: list[dict], siren: str, aujourd_hui: Optional[date] = None) -> dict:
    """Synthèse pure des annonces concernant CE siren."""
    today = aujourd_hui or date.today()
    propres = [r for r in records if concerne_siren(r, siren)]
    ecartees = len(records) - len(propres)
    propres.sort(key=_date, reverse=True)

    annonces, depots, capital = [], [], None
    procedure = {"statut": "aucune", "type": None, "date": None, "libelle": None}
    dissolution = None
    derniere_famille = None
    nb_modifs_3ans = 0

    for r in propres:
        fam = _famille(r)
        d = _date(r)
        derniere_famille = derniere_famille or fam
        item = {"date": d, "famille": fam, "type": r.get("typeavis_lib") or r.get("typeavis"),
                "url": r.get("url_complete")}
        if fam == "collective":
            txt = _texte_jugement(r)
            item["jugement"] = txt.strip()[:300]
            if procedure["statut"] == "aucune":          # la plus récente fait foi
                statut, typ = classer_jugement(txt)
                if statut != "autre":
                    procedure = {"statut": statut, "type": typ, "date": d, "libelle": txt.strip()[:200]}
        elif fam == "modification":
            desc = _descriptif_modif(r)
            item["descriptif"] = desc[:300]
            try:
                if (today - datetime.strptime(d, "%Y-%m-%d").date()).days <= 3 * 365:
                    nb_modifs_3ans += 1
            except ValueError:
                pass
            if dissolution is None and _RE_DISSOLUTION.search(desc):
                dissolution = {"date": d, "descriptif": desc[:200]}
        elif fam == "dpc":
            dep = _loads(r.get("depot"))
            if isinstance(dep, dict):
                depots.append({
                    "date_cloture": dep.get("dateCloture"),
                    "type": dep.get("typeDepot"),
                    "confidentiel": "confidential" in json.dumps(dep, ensure_ascii=False).lower()
                    or "confidentialit" in json.dumps(dep, ensure_ascii=False).lower(),
                })
        if capital is None:
            capital = _capital(r)
        annonces.append(item)

    return {
        "nb_annonces": len(propres),
        "nb_annonces_ecartees_autre_siren": ecartees,
        "procedure": procedure,
        "radiee": derniere_famille == "radiation",
        "dissolution": dissolution,
        "capital_social": capital,
        "depots_comptes": depots,
        "nb_modifications_3ans": nb_modifs_3ans,
        "annonces": annonces[:30],
        "_source": "bodacc",
    }


def fetch_bodacc(siren: str) -> dict:
    return analyser_bodacc(fetch_bodacc_records(siren), siren)
