# -*- coding: utf-8 -*-
"""
Test métier de la recherche web dureté V4 — sans lancer le score complet.

Montre, dans l'ordre du pipeline :
  1. le profil envoyé (annuaire, ou saisie manuelle) ;
  2. le prompt exact ;
  3. le texte brut du modèle et les requêtes Google / web_search ;
  4. les sources et les extraits réellement appuyés par une URL ;
  5. le classement (gardé / écarté) : seuls les extraits « siren » ou « nom_commune »
     deviennent des constats. Ils n'entrent pas dans le score.

Depuis KERFONCIER/backend :

  python -m api.recherche_de_foncier.enrichir.durete_fonciere.durete_v4.test_web_research \\
      --siren 552081317 --communes 75101

  --denomination et --commune-siege évitent l'appel annuaire.
  --dirigeants ajoute les noms de décideurs au prompt (désactivé par défaut, RGPD).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path


def _load_backend_env() -> None:
    for parent in Path(__file__).resolve().parents:
        env = parent / ".env"
        if env.is_file() and (parent / "main.py").is_file():
            for line in env.read_text(encoding="utf-8").splitlines():
                raw = line.strip()
                if not raw or raw.startswith("#") or "=" not in raw:
                    continue
                key, value = raw.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
            return


def _profil(siren: str, denomination: str, commune: str) -> dict:
    from api.recherche_de_foncier.enrichir.durete_fonciere.durete_v4.sources import annuaire

    if denomination:
        profil = annuaire.profil_minimal(siren, denomination)
        if commune:
            profil["siege"] = {"commune": commune}
        return profil
    raw = annuaire.fetch_annuaire(siren)
    if not raw:
        raise SystemExit(f"Annuaire vide pour {siren}. Passez --denomination.")
    return annuaire.normaliser_annuaire(raw, siren)


def _section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def main() -> None:
    _load_backend_env()
    parser = argparse.ArgumentParser(description="Test métier recherche web dureté V4")
    parser.add_argument("--siren", required=True, help="SIREN à 9 chiffres")
    parser.add_argument("--denomination", default="", help="Si vide, lu dans l'annuaire")
    parser.add_argument("--commune-siege", default="", help="Commune du siège si pas d'annuaire")
    parser.add_argument("--communes", default="", help="Codes INSEE des parcelles, séparés par des virgules")
    parser.add_argument("--dirigeants", action="store_true", help="Inclure les décideurs dans le prompt")
    parser.add_argument("--provider", default="", help="gemini | mistral | none (défaut : DURETE_LLM_PROVIDER)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s", stream=sys.stderr)

    from api.recherche_de_foncier.enrichir.durete_fonciere.durete_v4.llm import get_llm
    from api.recherche_de_foncier.enrichir.durete_fonciere.durete_v4.web_research import (
        construire_requete,
        extraits_sources,
        prompt_classification,
        valider_constats,
    )

    siren = "".join(args.siren.split())
    profil = _profil(siren, args.denomination.strip(), args.commune_siege.strip())
    communes = [c.strip() for c in args.communes.split(",") if c.strip()]
    prompt = construire_requete(profil, communes, args.dirigeants)

    _section("1. Profil envoyé au web")
    siege = profil.get("siege") or {}
    print(f"SIREN          {profil.get('siren')}")
    print(f"Dénomination   {profil.get('denomination')}")
    print(f"Forme          {profil.get('libelle_forme_juridique') or profil.get('nature_juridique')}")
    print(f"Siège          {siege.get('commune')} {siege.get('code_postal') or ''}")
    print(f"Communes IDU   {', '.join(communes) or '—'}")
    print(f"Dirigeants     {'inclus' if args.dirigeants else 'exclus du prompt'}")

    _section("2. Prompt de recherche")
    print(prompt)

    llm = get_llm(args.provider or None)
    if llm is None:
        raise SystemExit("Aucun LLM (provider=none ou clé API absente).")
    print(f"\nFournisseur    {llm.nom}", file=sys.stderr)

    _section("3. Réponse brute de l'outil de recherche")
    res = llm.search(prompt)
    print(f"Requêtes outil : {res.requetes or '—'}")
    print(f"Sources        : {len(res.sources)}")
    print(f"Appuis         : {len(res.appuis)}")
    print("\n--- texte modèle ---")
    print(res.texte or "(vide)")

    _section("4. Sources")
    if not res.sources:
        print("Aucune source renvoyée par l'outil.")
    for i, src in enumerate(res.sources):
        print(f"[{i}] {src.get('titre') or '?'}")
        print(f"    {src.get('url') or '?'}")

    extraits = extraits_sources(res)
    _section("5. Extraits conservés (segment + au moins une source)")
    print(f"{len(extraits)} extrait(s) — le reste du texte modèle est ignoré.")
    for e in extraits:
        print(f"\n[{e['id']}] {e['texte']}")
        for src in e["sources"]:
            print(f"    → {src.get('titre') or '?'} | {src.get('url') or '?'}")

    if not extraits:
        print("\nArrêt : rien à classer. Le pipeline renverrait constats=[].")
        return

    _section("6. Prompt de classement (sans nouvel accès web)")
    print(prompt_classification(profil, extraits))

    _section("7. Constats après filtre homonymes")
    cls = llm.json(prompt_classification(profil, extraits))
    web = valider_constats(cls, extraits)
    print(f"écartés={web.get('nb_ecartes', 0)}  site={web.get('site_web') or '—'}")
    print("Les écartés sont des homonymes (correspondance incertaine) ou des ids inconnus.")
    print("Aucun de ces constats ne modifie le score : ils nourrissent seulement la synthèse.\n")
    print(json.dumps(web.get("constats") or [], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
