# -*- coding: utf-8 -*-
"""
CLI — python -m durete_v4 <commande>

  analyse --siren 952824076 [--idus 33213000BP0018 ...] [--web] [--json] [--no-rpg] [--no-gpu]
  batch   --input pool.json --output res.jsonl [--workers 2] [--resume] [--web] [--dry-run]
  resume  res.jsonl                       # tableau trié par score
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from .service import run_durete_for_siren


def lire_input(path: str) -> list[dict]:
    """CSV ou JSON, colonnes/clés : siren, idus|idu, denomination?, forme_juridique?"""
    p = Path(path)
    rows = json.loads(p.read_text(encoding="utf-8")) if p.suffix.lower() == ".json" else \
        list(csv.DictReader(p.open(encoding="utf-8", newline="")))
    out = []
    for r in rows:
        idus = r.get("idus") if "idus" in r else r.get("idu")
        if isinstance(idus, str):
            idus = [i.strip() for i in idus.split(",") if i.strip()]
        out.append({"siren": str(r.get("siren", "")).strip(), "idus": idus or [],
                    "denomination": (r.get("denomination") or "").strip(),
                    "forme_juridique": str(r.get("forme_juridique") or "").strip()})
    # regroupe les lignes d'un même SIREN (pool « 1 ligne = 1 parcelle »)
    par_siren: dict[str, dict] = {}
    for r in out:
        cur = par_siren.setdefault(r["siren"], {**r, "idus": []})
        cur["idus"] += [i for i in r["idus"] if i not in cur["idus"]]
    return list(par_siren.values())


def afficher(res: dict) -> None:
    if res.get("statut") != "ok":
        print(f"✗ {res.get('siren')} : {res.get('erreur')}")
        return
    v4 = res["detail_axes"]["v4"]
    print(f"\n{'=' * 70}\n {res['denomination']} — SIREN {res['siren']}\n{'=' * 70}")
    print(f" SCORE {res['score_final']}/100 · {res['niveau_durete']} · confiance {res['confiance']} "
          f"({res['indice_confiance']}/100)\n")
    for k, a in v4["axes"].items():
        print(f"  {k}  {a['score']:>2}/{a['max']:<2}  [{a['confiance']:<7}] {a['note']}")
    for f in v4["surcharges"]:
        print(f"  surch {f['points']:+3d}          {f['libelle']}")
    if v4["signaux"]:
        print("\n Signaux :")
        for s in v4["signaux"]:
            print(f"  • {s['libelle']}")
    print("\n Synthèse :", res["explication"])
    print("\n Recommandations :")
    for r in res["recommandations"]:
        print(f"  → {r}")
    web = res.get("recherche_web") or {}
    if web.get("constats"):
        print("\n Web :")
        for c in web["constats"]:
            src = (c["sources"][0].get("titre") or c["sources"][0].get("url")) if c["sources"] else "?"
            print(f"  [{c['effet_durete']}] {c['constat']} ({src})")
    if res["avertissements"]:
        print("\n Avertissements :")
        for a in res["avertissements"]:
            print(f"  ⚠ {a}")


def main(argv=None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(prog="durete_v4", description="Dureté foncière Kerelia — V4")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def opts(p):
        p.add_argument("--no-rpg", action="store_true")
        p.add_argument("--no-gpu", action="store_true")
        p.add_argument("--web", action="store_true", help="recherche web sourcée (LLM)")
        p.add_argument("--no-llm", action="store_true", help="synthèse déterministe, aucun appel LLM")
        p.add_argument("--llm", default=None, help="gemini | mistral")
        p.add_argument("--model", default=None)
        p.add_argument("-v", "--verbose", action="store_true")

    a = sub.add_parser("analyse")
    a.add_argument("--siren", required=True)
    a.add_argument("--idus", nargs="*", default=None)
    a.add_argument("--json", action="store_true")
    opts(a)
    b = sub.add_parser("batch")
    b.add_argument("--input", required=True)
    b.add_argument("--output", required=True)
    b.add_argument("--workers", type=int, default=1)
    b.add_argument("--resume", action="store_true")
    b.add_argument("--dry-run", action="store_true")
    opts(b)
    r = sub.add_parser("resume")
    r.add_argument("jsonl")
    args = ap.parse_args(argv)

    if args.cmd == "resume":
        rows = [json.loads(l) for l in Path(args.jsonl).read_text(encoding="utf-8").splitlines() if l.strip()]
        ok = sorted((x for x in rows if x.get("statut") == "ok"), key=lambda x: x["score_final"])
        print(f"{'SIREN':<11} {'Score':>5} {'Conf.':>6}  {'Niveau':<26} Dénomination")
        for x in ok:
            print(f"{x['siren']:<11} {x['score_final']:>5} {x.get('indice_confiance', '?'):>6}  "
                  f"{x['niveau_durete']:<26} {x['denomination']}")
        print(f"\n{len(ok)} OK / {len(rows) - len(ok)} erreur(s)")
        return 0

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s — %(message)s", datefmt="%H:%M:%S")
    kw = dict(avec_rpg=not args.no_rpg, avec_gpu=not args.no_gpu, avec_recherche_web=args.web,
              avec_narration=not args.no_llm, llm_provider="none" if args.no_llm else args.llm,
              model=args.model, verbose=args.verbose)

    if args.cmd == "analyse":
        res = run_durete_for_siren(args.siren, args.idus, **kw)
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str)) if args.json else afficher(res)
        return 0 if res.get("statut") == "ok" else 1

    items = lire_input(args.input)
    if args.resume and Path(args.output).exists():
        faits = {json.loads(l)["siren"] for l in Path(args.output).read_text(encoding="utf-8").splitlines()
                 if l.strip() and json.loads(l).get("statut") == "ok"}
        items = [i for i in items if i["siren"] not in faits]
    if args.dry_run:
        for i, it in enumerate(items, 1):
            print(f"[{i:02d}] {it['siren']} {it['denomination']} idus={it['idus'] or '(fichier PM)'}")
        return 0
    from concurrent.futures import ThreadPoolExecutor
    with open(args.output, "a" if args.resume else "w", encoding="utf-8") as out, \
            ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = [ex.submit(run_durete_for_siren, it["siren"], it["idus"] or None,
                          denomination=it["denomination"], forme_juridique=it["forme_juridique"], **kw)
                for it in items]
        for n, f in enumerate(futs, 1):
            res = f.result()
            out.write(json.dumps(res, ensure_ascii=False, default=str) + "\n")
            out.flush()
            print(f"[{n}/{len(items)}] {res.get('siren')} → {res.get('score_final', res.get('erreur'))}",
                  file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
