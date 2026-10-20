# -*- coding: utf-8 -*-
"""
Recherche web sourcée sur une personne morale — NOUVEAU en V4.

Ce que les API publiques ne disent pas et qu'une recherche web révèle souvent :
projet d'aménagement / lotissement / carrière annoncé, cession ou acquisition
foncière relayée par la presse locale, contentieux, changement d'actionnaire,
engagement environnemental, objet réel d'une SCI ou d'une association.

Garde-fous anti-hallucination (2 étapes) :
  1. appel avec outil de recherche (Gemini grounding / Mistral web_search) →
     on ne conserve que les SEGMENTS appuyés par au moins une source retournée
     par l'outil (grounding_supports / tool_reference) ;
  2. appel SANS outil qui ne fait que CLASSER ces extraits numérotés
     (catégorie, effet sur la dureté, niveau de correspondance) — il n'a pas
     le droit d'ajouter un fait. Les extraits à correspondance « incertaine »
     (homonymes : « SCI DE BEL AIR » existe des centaines de fois) sont écartés.
Les constats web n'entrent PAS dans le score : ils alimentent la synthèse et
sont affichés comme signaux à vérifier.

RGPD : la requête ne contient que des données de la PM (dénomination, SIREN,
communes) ; les noms des dirigeants ne sont pas envoyés par défaut.
"""
from __future__ import annotations

import logging
from typing import Optional

from .llm import ResultatRecherche

log = logging.getLogger("durete.web")

CATEGORIES = ("projet_amenagement", "transaction_fonciere", "contentieux", "difficultes",
              "gouvernance", "engagement_environnemental", "activite", "autre")


def construire_requete(profil: dict, communes_parcelles: list[str], inclure_dirigeants: bool = False) -> str:
    siege = profil.get("siege") or {}
    lignes = [
        "Tu es analyste foncier. Recherche sur le web des informations PUBLIQUES et RÉCENTES (5 dernières années) "
        "sur la personne morale suivante, utiles pour évaluer si elle serait disposée à céder ou conventionner "
        "du foncier pour de la compensation écologique.",
        f"- Dénomination : {profil.get('denomination')}",
        f"- SIREN : {profil.get('siren')}",
        f"- Forme juridique : {profil.get('libelle_forme_juridique')}",
        f"- Siège : {siege.get('commune') or 'inconnu'} {siege.get('code_postal') or ''}",
    ]
    if communes_parcelles:
        lignes.append(f"- Communes des parcelles (code INSEE) : {', '.join(communes_parcelles[:5])}")
    if inclure_dirigeants:
        noms = [d.get("denomination") or f"{d.get('prenoms') or ''} {d.get('nom') or ''}".strip()
                for d in profil.get("decideurs") or []][:3]
        if noms:
            lignes.append(f"- Dirigeants : {', '.join(noms)}")
    lignes += [
        "Cherche notamment : projets d'aménagement, de construction, de lotissement ou de carrière ; "
        "achats/ventes de terrains ; contentieux ; difficultés ou restructurations ; activité réelle et objet ; "
        "engagements environnementaux ; site web officiel.",
        "Règles : ne retiens que des résultats qui désignent sans ambiguïté CETTE entité (même SIREN, ou même nom "
        "ET même commune). Ignore les homonymes. Pas d'informations sur la vie privée de personnes physiques. "
        "Rédige une liste de constats factuels courts, un par ligne, datés si possible. "
        "Si tu ne trouves rien de fiable, écris exactement : AUCUN RÉSULTAT FIABLE.",
    ]
    return "\n".join(lignes)


def extraits_sources(res: ResultatRecherche) -> list[dict]:
    """Segments appuyés par au moins une source (étape 1)."""
    out, vus = [], set()
    for a in res.appuis:
        t = " ".join((a.get("texte") or "").split())
        idx = [i for i in a.get("sources") or [] if 0 <= i < len(res.sources)]
        if len(t) < 15 or not idx or t in vus:
            continue
        vus.add(t)
        out.append({"id": len(out) + 1, "texte": t[:500], "sources": [res.sources[i] for i in idx][:3]})
    return out


def prompt_classification(profil: dict, extraits: list[dict]) -> str:
    lignes = "\n".join(f"[{e['id']}] {e['texte']}  (sources : "
                       f"{', '.join((s.get('titre') or s.get('url') or '?') for s in e['sources'])})"
                       for e in extraits)
    return f"""Entité analysée : {profil.get('denomination')} (SIREN {profil.get('siren')}), siège {(profil.get('siege') or {}).get('commune')}.

Extraits web sourcés :
{lignes}

Pour CHAQUE extrait qui concerne réellement cette entité, renvoie un objet. N'ajoute AUCUN fait absent des extraits.
- categorie ∈ {list(CATEGORIES)}
- effet_durete : "hausse" (rend une cession moins probable : projet en cours, plus-value attendue, attachement),
  "baisse" (rend une cession plus probable : difficultés, vente annoncée, désengagement, intérêt environnemental), ou "neutre"
- correspondance : "siren" (SIREN cité), "nom_commune" (nom ET commune concordent) ou "incertaine"
- date : "AAAA" ou "AAAA-MM" si connue, sinon null
- constat : reformulation factuelle en une phrase

Réponds UNIQUEMENT en JSON : {{"constats": [{{"extrait": <id>, "categorie": "...", "effet_durete": "...",
"correspondance": "...", "date": null, "constat": "..."}}], "site_web": null}}"""


def rechercher(
    llm,
    profil: dict,
    communes_parcelles: Optional[list[str]] = None,
    inclure_dirigeants: bool = False,
) -> dict:
    if llm is None:
        log.info("DURETE WEB OFF | siren=%s raison=aucun LLM", profil.get("siren"))
        return {"statut": "desactive", "constats": [], "raison": "aucun LLM configuré"}
    requete = construire_requete(profil, communes_parcelles or [], inclure_dirigeants)
    log.info(
        "DURETE WEB START | siren=%s denomination=%s communes=%s dirigeants=%s",
        profil.get("siren"), profil.get("denomination"),
        ",".join((communes_parcelles or [])[:5]), inclure_dirigeants,
    )
    log.info("DURETE WEB PROMPT | siren=%s\n%s", profil.get("siren"), requete)
    try:
        res = llm.search(requete)
    except Exception as e:  # noqa: BLE001
        log.warning("Recherche web en échec : %s", e)
        return {"statut": "erreur", "constats": [], "raison": str(e)}
    log.info(
        "DURETE WEB GROUNDING | siren=%s requetes=%s sources=%d appuis=%d",
        profil.get("siren"), res.requetes, len(res.sources), len(res.appuis),
    )
    for i, src in enumerate(res.sources):
        log.info(
            "DURETE WEB SOURCE | siren=%s [%d] %s | %s",
            profil.get("siren"), i, src.get("titre"), src.get("url"),
        )
    if "AUCUN RÉSULTAT FIABLE" in (res.texte or "").upper():
        return {"statut": "ok", "constats": [], "requetes": res.requetes, "fournisseur": llm.nom,
                "raison": "aucun résultat fiable"}
    extraits = extraits_sources(res)
    log.info("DURETE WEB EXTRAITS | siren=%s retenus=%d", profil.get("siren"), len(extraits))
    for e in extraits:
        urls = ", ".join((s.get("url") or s.get("titre") or "?") for s in e["sources"])
        log.info("DURETE WEB EXTRAIT | siren=%s [%s] %s | %s", profil.get("siren"), e["id"], e["texte"], urls)
    if not extraits:
        return {"statut": "ok", "constats": [], "requetes": res.requetes, "fournisseur": llm.nom,
                "raison": "aucun segment appuyé par une source"}
    try:
        cls = llm.json(prompt_classification(profil, extraits))
    except Exception as e:  # noqa: BLE001
        log.warning("Classification web en échec : %s", e)
        return {"statut": "erreur", "constats": [], "raison": str(e)}
    out = {"statut": "ok", "fournisseur": llm.nom, "requetes": res.requetes,
           **valider_constats(cls, extraits)}
    log.info(
        "DURETE WEB CLASSE | siren=%s gardes=%d ecartes=%d site=%s",
        profil.get("siren"), len(out.get("constats") or []), out.get("nb_ecartes"), out.get("site_web"),
    )
    return out


def valider_constats(cls: dict, extraits: list[dict]) -> dict:
    """Ne garde que les constats rattachés à un extrait sourcé et non homonymes (pur)."""
    par_id = {e["id"]: e for e in extraits}
    gardes, ecartes = [], 0
    for c in (cls or {}).get("constats") or []:
        e = par_id.get(c.get("extrait"))
        if not e or c.get("correspondance") not in ("siren", "nom_commune"):
            ecartes += 1
            continue
        gardes.append({
            "constat": str(c.get("constat") or e["texte"])[:400],
            "categorie": c.get("categorie") if c.get("categorie") in CATEGORIES else "autre",
            "effet_durete": c.get("effet_durete") if c.get("effet_durete") in ("hausse", "baisse", "neutre") else "neutre",
            "correspondance": c["correspondance"],
            "date": c.get("date"),
            "sources": e["sources"],
        })
    return {"constats": gardes, "nb_ecartes": ecartes, "site_web": (cls or {}).get("site_web")}
