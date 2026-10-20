#!/usr/bin/env python3
"""
Exploration de l'API Recherche d'entreprises (DINUM / annuaire-entreprises.data.gouv.fr)
+ check fraîcheur BODACC, sur un SIREN de test.

Usage :
    pip install requests
    python explore_recherche_entreprises.py                 # SIREN par défaut 433365079
    python explore_recherche_entreprises.py 433365079 --no-bodacc

Sorties dans ./explore_out/<siren>_<timestamp>/ :
    00_calls.log              -> journal de chaque appel (url, status, latence, headers rate-limit)
    01_siren_full.json        -> réponse brute recherche par SIREN (mode complet)
    02_siren_minimal.json     -> réponse brute mode minimal + include=...
    03_by_name.json           -> recherche par dénomination (test du matching par nom)
    04_dirigeants_pm/*.json   -> fiches des dirigeants personnes morales (holdings, 1 niveau)
    05_near_point.json        -> entreprises autour du siège (endpoint géo)
    06_population_filter.json -> nb d'entités même nature juridique dans la commune du siège
    07_bodacc_siren.json      -> annonces BODACC du SIREN
    08_bodacc_latest.json     -> dernière annonce BODACC publiée (fraîcheur globale)
    schema_paths.txt          -> inventaire de tous les champs (chemin, type, exemple)
    flat_record.json          -> proposition de ligne "à plat" pour le pipeline de scoring
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

API = "https://recherche-entreprises.api.gouv.fr"
BODACC = "https://bodacc-datadila.opendatasoft.com/api/explore/v2.1/catalog/datasets/annonces-commerciales/records"
UA = {"User-Agent": "kerelia-explore/0.1"}
MIN_INTERVAL = 0.2  # ~5 req/s, sous la limite publique (~7 req/s, à vérifier dans la doc)

_last_call = 0.0


def log(out: Path, msg: str):
    print(msg)
    with open(out / "00_calls.log", "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def get(out: Path, url: str, params: dict, label: str, max_retry: int = 4):
    """GET avec throttle + retry sur 429/5xx, et log des headers intéressants."""
    global _last_call
    for attempt in range(max_retry):
        wait = MIN_INTERVAL - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        t0 = time.time()
        r = requests.get(url, params=params, headers=UA, timeout=30)
        _last_call = time.time()
        dt = (time.time() - t0) * 1000
        rl = {k: v for k, v in r.headers.items()
              if k.lower().startswith(("x-ratelimit", "ratelimit", "retry-after", "x-total"))}
        log(out, f"[{label}] {r.status_code} {dt:.0f}ms {r.url}  rate-limit-headers={rl}")
        if r.status_code == 429 or r.status_code >= 500:
            sleep = float(r.headers.get("Retry-After", 2 ** attempt))
            log(out, f"   -> retry dans {sleep}s")
            time.sleep(sleep)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Echec après {max_retry} essais : {label}")


def save(out: Path, name: str, data):
    p = out / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def walk_schema(obj, prefix="", acc=None):
    """Liste tous les chemins de champs avec type + exemple (listes : 1er élément)."""
    if acc is None:
        acc = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            walk_schema(v, f"{prefix}.{k}" if prefix else k, acc)
    elif isinstance(obj, list):
        acc.setdefault(prefix + "[]", ("list", f"len={len(obj)}"))
        if obj:
            walk_schema(obj[0], prefix + "[]", acc)
    else:
        acc.setdefault(prefix, (type(obj).__name__, repr(obj)[:80]))
    return acc


def pick_result(data: dict, siren: str):
    for r in data.get("results", []):
        if r.get("siren") == siren:
            return r
    return None


def flatten_for_pipeline(e: dict) -> dict:
    """Proposition de colonnes utiles au scoring dureté foncière (à ajuster)."""
    siege = e.get("siege") or {}
    dirigeants = e.get("dirigeants") or []
    finances = e.get("finances") or {}
    last_year = max(finances) if finances else None
    comp = e.get("complements") or {}
    pp = [d for d in dirigeants if d.get("type_dirigeant") == "personne physique"]
    pm = [d for d in dirigeants if d.get("type_dirigeant") == "personne morale"]
    annees = [int(d["annee_de_naissance"]) for d in pp
              if str(d.get("annee_de_naissance") or "").isdigit()]
    return {
        "siren": e.get("siren"),
        "nom_complet": e.get("nom_complet"),
        "nature_juridique": e.get("nature_juridique"),
        "activite_principale": e.get("activite_principale"),
        "section_activite_principale": e.get("section_activite_principale"),
        "etat_administratif": e.get("etat_administratif"),
        "date_creation": e.get("date_creation"),
        "date_fermeture": e.get("date_fermeture"),
        "categorie_entreprise": e.get("categorie_entreprise"),
        "tranche_effectif_salarie": e.get("tranche_effectif_salarie"),
        "nombre_etablissements_ouverts": e.get("nombre_etablissements_ouverts"),
        "siege_code_commune": siege.get("commune"),
        "siege_code_postal": siege.get("code_postal"),
        "siege_lat": siege.get("latitude"),
        "siege_lon": siege.get("longitude"),
        "nb_dirigeants_pp": len(pp),
        "nb_dirigeants_pm": len(pm),
        "sirens_dirigeants_pm": [d.get("siren") for d in pm],
        "annee_naissance_dirigeant_min": min(annees) if annees else None,
        "finances_annee": last_year,
        "ca": (finances.get(last_year) or {}).get("ca") if last_year else None,
        "resultat_net": (finances.get(last_year) or {}).get("resultat_net") if last_year else None,
        "est_association": comp.get("est_association"),
        "est_service_public": comp.get("est_service_public"),
        "collectivite_territoriale": bool(comp.get("collectivite_territoriale")),
        "est_ess": comp.get("est_ess"),
        "date_mise_a_jour": e.get("date_mise_a_jour"),
        "date_mise_a_jour_insee": e.get("date_mise_a_jour_insee"),
        "date_mise_a_jour_rne": e.get("date_mise_a_jour_rne"),
        "statut_diffusion": e.get("statut_diffusion"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("siren", nargs="?", default="433365079")
    ap.add_argument("--no-bodacc", action="store_true")
    args = ap.parse_args()
    siren = args.siren.replace(" ", "")

    out = Path("explore_out") / f"{siren}_{datetime.now():%Y%m%d_%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    log(out, f"=== Exploration SIREN {siren} — {datetime.now().isoformat()} ===")

    # 1) Recherche par SIREN, réponse complète (pas d'endpoint "GET /siren", on passe par q)
    full = get(out, f"{API}/search", {"q": siren, "per_page": 5}, "siren_full")
    save(out, "01_siren_full.json", full)
    e = pick_result(full, siren)
    if not e:
        log(out, "!! SIREN introuvable (non diffusible, cessé ancien, ou absent de l'index).")
        sys.exit(1)

    # 2) Mode minimal + include explicite + matching_etablissements étendu
    minimal = get(out, f"{API}/search", {
        "q": siren, "minimal": "true",
        "include": "siege,dirigeants,finances,complements,matching_etablissements,score",
        "limite_matching_etablissements": 100,
    }, "siren_minimal")
    save(out, "02_siren_minimal.json", minimal)

    # 3) Recherche par nom : rang du bon SIREN parmi les résultats ?
    name = e.get("nom_raison_sociale") or e.get("nom_complet")
    by_name = get(out, f"{API}/search", {"q": name, "per_page": 25}, "by_name")
    save(out, "03_by_name.json", by_name)
    rank = next((i for i, r in enumerate(by_name.get("results", []), 1) if r.get("siren") == siren), None)
    log(out, f"   recherche par nom '{name}' : total={by_name.get('total_results')} rang_du_siren={rank}")

    # 4) Dirigeants personnes morales -> holdings (1 niveau)
    for d in e.get("dirigeants") or []:
        if d.get("type_dirigeant") == "personne morale" and d.get("siren"):
            sub = get(out, f"{API}/search", {"q": d["siren"], "per_page": 1}, f"dirigeant_pm_{d['siren']}")
            save(out, f"04_dirigeants_pm/{d['siren']}.json", sub)

    # 5) Endpoint géographique autour du siège (radius en km)
    siege = e.get("siege") or {}
    if siege.get("latitude") and siege.get("longitude"):
        near = get(out, f"{API}/near_point", {
            "lat": siege["latitude"], "long": siege["longitude"], "radius": 0.5, "per_page": 25,
        }, "near_point")
        save(out, "05_near_point.json", near)
        log(out, f"   near_point 500m : total={near.get('total_results')}")

    # 6) Filtres structurés : taille de la population "même nature juridique, même commune"
    if siege.get("commune") and e.get("nature_juridique"):
        pop = get(out, f"{API}/search", {
            "code_commune": siege["commune"], "nature_juridique": e["nature_juridique"],
            "etat_administratif": "A", "per_page": 1,
        }, "population_filter")
        save(out, "06_population_filter.json", pop)
        log(out, f"   nature_juridique={e['nature_juridique']} actives dans {siege['commune']} : "
                 f"total={pop.get('total_results')}")

    # 7) BODACC : annonces du SIREN + fraîcheur globale
    if not args.no_bodacc:
        try:
            b = get(out, BODACC, {"where": f'"{siren}"', "order_by": "dateparution desc", "limit": 20},
                    "bodacc_siren")
            save(out, "07_bodacc_siren.json", b)
            log(out, f"   BODACC annonces pour {siren} : total={b.get('total_count')}")
            for rec in b.get("results", [])[:10]:
                log(out, f"     {rec.get('dateparution')} | {rec.get('familleavis_lib')} | "
                         f"{rec.get('typeavis_lib')} | {rec.get('tribunal')}")
            latest = get(out, BODACC, {"order_by": "dateparution desc", "limit": 1}, "bodacc_latest")
            save(out, "08_bodacc_latest.json", latest)
            last = (latest.get("results") or [{}])[0].get("dateparution")
            log(out, f"   BODACC dernière parution indexée : {last} (aujourd'hui {datetime.now():%Y-%m-%d})")
        except Exception as ex:  # noqa: BLE001
            log(out, f"   BODACC KO : {ex}")

    # 8) Inventaire des champs + ligne à plat
    schema = walk_schema(e)
    lines = [f"{k:70s} {t:8s} {ex}" for k, (t, ex) in sorted(schema.items())]
    (out / "schema_paths.txt").write_text("\n".join(lines), encoding="utf-8")
    flat = flatten_for_pipeline(e)
    save(out, "flat_record.json", flat)

    log(out, "\n=== Résumé ===")
    for k, v in flat.items():
        log(out, f"  {k:32s} {v}")
    log(out, f"\n  nb champs distincts dans la fiche : {len(schema)}")
    log(out, f"  diff clés mode complet vs minimal : "
             f"{sorted(set(e) ^ set(pick_result(minimal, siren) or {}))}")
    log(out, f"\nSorties : {out.resolve()}")


if __name__ == "__main__":
    main()