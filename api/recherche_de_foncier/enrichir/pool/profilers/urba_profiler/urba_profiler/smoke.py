"""
smoke.py — Test réel contre le WFS GPU, en ligne de commande (à lancer en dev).

    # parcelle récupérée via l'API Carto cadastre (IGN)
    python -m urba_profiler.smoke --insee 33234 --section AB --numero 0123

    # ou une géométrie GeoJSON locale (Feature, FeatureCollection ou geometry), en 4326
    python -m urba_profiler.smoke --geojson ma_parcelle.geojson

    options : --voisinage 50  --layers zonage_plu,prescriptions_surf  --json sortie.json
              --cache-dir .cache_wfs  (réponses WFS mises en cache disque)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

import requests


def _parcelle_apicarto(insee: str, section: str, numero: str) -> dict:
    r = requests.get(
        "https://apicarto.ign.fr/api/cadastre/parcelle",
        params={"code_insee": insee, "section": section.upper(), "numero": numero.zfill(4)},
        timeout=30,
    )
    r.raise_for_status()
    feats = r.json().get("features") or []
    if not feats:
        sys.exit(f"Parcelle introuvable : {insee} {section} {numero}")
    return feats[0]["geometry"]


def _load_geojson(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        gj = json.load(f)
    if gj.get("type") == "FeatureCollection":
        gj = gj["features"][0]
    return gj.get("geometry", gj)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--insee")
    ap.add_argument("--section")
    ap.add_argument("--numero")
    ap.add_argument("--geojson")
    ap.add_argument("--voisinage", type=float, default=0)
    ap.add_argument("--layers")
    ap.add_argument("--json")
    ap.add_argument("--cache-dir")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.cache_dir:
        os.environ["URBA_WFS_CACHE_DIR"] = args.cache_dir

    from . import UrbaProfilRequest, run_urba_profile  # après la var d'env (cache)

    if args.geojson:
        geom, pid = _load_geojson(args.geojson), os.path.basename(args.geojson)
    elif args.insee and args.section and args.numero:
        geom, pid = _parcelle_apicarto(args.insee, args.section, args.numero), f"{args.insee}{args.section}{args.numero}"
    else:
        ap.error("--geojson ou --insee/--section/--numero requis")

    opts = {"voisinage_m": args.voisinage}
    if args.layers:
        opts["layers"] = args.layers.split(",")
    res = run_urba_profile(UrbaProfilRequest(parcelles=[{"id": pid, "geometry": geom, "srid": 4326}], options=opts))

    c = res.parcelles[pid]
    print(f"\n=== {pid} — {c.surface_m2:.0f} m² — flags={c.flags}")
    for d in c.documents:
        print(f"  doc {d.idurba} ({d.typedoc} {d.date}) partition={d.partition}")
    for cl in c.couches:
        print(f"\n[{cl.status:15}] {cl.display_name} ({cl.typename})" + (f" ERREUR: {cl.error}" if cl.error else ""))
        for e in cl.elements:
            m = e.mesure
            mes = (f"{m.pct_cible}%" if m.pct_cible is not None else
                   f"{m.longueur_m} m" if m.longueur_m is not None else f"{m.nb_points} pt")
            lib = e.attributs.get("libelle") or e.attributs.get("nomsuplitt") or e.attributs.get("nomass")
            print(f"   + {e.code or '':6} {str(lib)[:60]:60} {mes:>10} {m.position:9} {e.libelle_cnig or ''}"[:200]
                  + (f"  flags={e.flags}" if e.flags else ""))
        for e in cl.ecartes:
            print(f"   - écarté ({e.raison_ecart}) {e.attributs.get('libelle') or e.attributs.get('nomass')}")
        for e in cl.voisinage:
            print(f"   ~ voisinage {e.mesure.distance_m} m : {e.attributs.get('libelle') or e.attributs.get('nomass')}")
    m = res.meta
    print(f"\nWFS: {m.wfs_requests} requêtes, {m.wfs_cache_hits} depuis le cache, {m.duration_s}s, erreurs={m.errors}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            f.write(res.model_dump_json(indent=1, exclude_none=True))
        print(f"JSON écrit : {args.json}")


if __name__ == "__main__":
    main()
