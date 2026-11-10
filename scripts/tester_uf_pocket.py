#!/usr/bin/env python3
"""Teste uf_pocket sur le bloc contigu réel d'une personne morale.

Depuis backend/ :

  python scripts/tester_uf_pocket.py --denomination "DOMAINE DES PINS"

Le SQL qui isole les parcelles du propriétaire dans l'emprise est SQL_BLOC.
Le regroupement en blocs qui se touchent se fait ensuite en Python (même règle
que aoi_to_sub_uf : ST_Touches ou contact de bord). Le plus gros bloc est celui
des 127. Une seconde requête évalue excluded, anchor, occ_ok_area, area.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
UF = BACKEND / "api" / "recherche_de_foncier" / "filtrage" / "unites_foncieres"
for p in (str(BACKEND), str(UF)):
    if p not in sys.path:
        sys.path.insert(0, p)

from sqlalchemy import text

from db import get_engine
from uf_pocket import adjacency_from_geojson, parcels_from_geojson, search_block

PROJECT_ID = "a9ff15e2-020f-419b-bc1f-4c815dc3ba09"
# Racine du bloc contigu de 127 (min idu de la composante).
IDU_RACINE = "331220000D2305"
WANT = {
    "331220000D2368",
    "331220000D2369",
    "331220000D2370",
    "331220000D2381",
    "331220000D2384",
}
CESBIO = [
    "Maïs", "Pelouses", "Prairies", "Protéagineux", "Soja", "Tournesol",
    "Tubercules/racines", "Verger", "Vergers", "Vigne", "Vignes",
    "Céréales à pailles", "Colza", "Lande herbacée", "Lande ligneuse",
    "Landes ligneuses", "Forêts de conifères", "Forêt fermée de conifères",
    "Forêt fermée mixte",
]

# Parcelles PPM de la société dans l'AOI du projet. À lancer seule pour voir le bloc.
SQL_BLOC = """
SELECT
    p.idu,
    p.siren,
    p.denomination,
    ST_Area(p.geom_2154) AS area,
    ST_AsGeoJSON(p.geom_2154)::json AS geometry
FROM public.parcelles_personnes_morales p
JOIN ecocompensation.projects pr ON pr.id = CAST(:project_id AS uuid)
JOIN ecocompensation.aoi a ON a.id = pr.aoi_id
WHERE p.denomination ILIKE :denom
  AND p.geom_2154 && a.geom_2154
  AND ST_Intersects(p.geom_2154, a.geom_2154)
  AND p.geom_2154 IS NOT NULL
"""

SQL_TOUCH = """
SELECT a.idu, b.idu
FROM public.parcelles_personnes_morales a
JOIN public.parcelles_personnes_morales b
  ON a.idu < b.idu
 AND a.geom_2154 && b.geom_2154
 AND (ST_Touches(a.geom_2154, b.geom_2154)
      OR ST_Relate(a.geom_2154, b.geom_2154, 'F***1****'))
WHERE a.idu = ANY(:idus) AND b.idu = ANY(:idus)
"""

SQL_CRITERES = """
WITH src AS (
    SELECT COALESCE(
        (SELECT f.geom_2154 FROM ecocompensation.projects pr
         JOIN ecocompensation.foncier f ON f.id = pr.foncier_id
         WHERE pr.id = CAST(:project_id AS uuid) LIMIT 1),
        (SELECT ST_Union(pp.geom_2154) FROM ecocompensation.project_parcelles pp
         WHERE pp.project_id = CAST(:project_id AS uuid))
    ) AS geom
),
base AS (
    SELECT idu, geom_2154, ST_Area(geom_2154) AS area
    FROM public.parcelles_personnes_morales
    WHERE idu = ANY(:idus)
)
SELECT
    b.idu,
    b.area,
    (
        EXISTS (SELECT 1 FROM src s WHERE s.geom IS NOT NULL AND b.geom_2154 && s.geom AND ST_Intersects(b.geom_2154, s.geom))
        OR EXISTS (SELECT 1 FROM ecocompensation.geomce_surf g WHERE g.deleted_at IS NULL AND g.geom_2154 IS NOT NULL AND b.geom_2154 && g.geom_2154 AND ST_Intersects(b.geom_2154, g.geom_2154))
        OR EXISTS (SELECT 1 FROM ecocompensation.geomce_lin g WHERE g.deleted_at IS NULL AND g.geom_2154 IS NOT NULL AND b.geom_2154 && g.geom_2154 AND ST_Intersects(b.geom_2154, g.geom_2154))
        OR EXISTS (SELECT 1 FROM ecocompensation.geomce_pct g WHERE g.deleted_at IS NULL AND g.geom_2154 IS NOT NULL AND b.geom_2154 && g.geom_2154 AND ST_Intersects(b.geom_2154, g.geom_2154))
        OR EXISTS (SELECT 1 FROM ecocompensation.preemption_espaces_naturels_sensibles g WHERE g.geom_2154 IS NOT NULL AND b.geom_2154 && g.geom_2154 AND ST_Intersects(b.geom_2154, g.geom_2154))
        OR EXISTS (SELECT 1 FROM ecocompensation.espaces_naturels_sensibles_ens g WHERE g.geom_2154 IS NOT NULL AND b.geom_2154 && g.geom_2154 AND ST_Intersects(b.geom_2154, g.geom_2154))
        OR EXISTS (SELECT 1 FROM ecocompensation.natura_2000 g WHERE g.geom_2154 IS NOT NULL AND b.geom_2154 && g.geom_2154 AND ST_Intersects(b.geom_2154, g.geom_2154))
    ) AS excluded,
    (
        EXISTS (
            SELECT 1 FROM ecocompensation.fauna f
            WHERE lower(btrim(f.nom_vernaculaire)) IN ('crapaud calamite (le)', 'crapaud calamite')
              AND f.geometry IS NOT NULL
              AND ST_DWithin(b.geom_2154, f.geometry, :radius_m)
        )
        OR EXISTS (
            SELECT 1 FROM ecocompensation.observation_faune o
            WHERE lower(btrim(o.nom_espece)) IN ('crapaud calamite (le)', 'crapaud calamite')
              AND o.geom IS NOT NULL
              AND ST_DWithin(b.geom_2154, o.geom, :radius_m)
        )
    ) AS anchor,
    COALESCE((
        SELECT SUM(
            CASE WHEN ST_CoveredBy(v.geom, b.geom_2154) THEN ST_Area(v.geom)
                 ELSE ST_Area(ST_Intersection(b.geom_2154, v.geom)) END
        )
        FROM ecocompensation.vegetation_sur_cesbio v
        WHERE v.libelle_prio = ANY(:libelles)
          AND b.geom_2154 && v.geom
          AND ST_Intersects(b.geom_2154, v.geom)
    ), 0) AS occ_ok_area
FROM base b
"""


class _UF:
    def __init__(self) -> None:
        self.p: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-id", default=PROJECT_ID)
    ap.add_argument("--denomination", default="DOMAINE DES PINS")
    ap.add_argument("--idu-racine", default=IDU_RACINE, help="Une parcelle du bloc contigu à tester")
    ap.add_argument("--kmax", type=int, default=5)
    ap.add_argument("--min-area", type=float, default=10_000, help="m²")
    ap.add_argument("--radius-m", type=float, default=600)
    ap.add_argument("--quota", type=int, default=20_000)
    ap.add_argument("--sql", action="store_true", help="affiche SQL_BLOC et s'arrête")
    args = ap.parse_args()

    if args.sql:
        print(SQL_BLOC)
        return

    engine = get_engine()
    denom = f"%{args.denomination}%"
    with engine.connect() as conn:
        rows = conn.execute(
            text(SQL_BLOC),
            {"project_id": args.project_id, "denom": denom},
        ).mappings().all()
        if not rows:
            raise SystemExit(f"Aucune parcelle pour {args.denomination!r}")
        print(f"{rows[0]['denomination']}  SIREN {rows[0]['siren']}  {len(rows)} parcelles dans l'emprise")
        idus = [r["idu"] for r in rows]
        pairs = conn.execute(text(SQL_TOUCH), {"idus": idus}).all()

        uf = _UF()
        for i in idus:
            uf.find(i)
        for a, b in pairs:
            uf.union(a, b)
        groups: dict[str, list] = {}
        for i in idus:
            groups.setdefault(uf.find(i), []).append(i)
        if args.idu_racine not in idus:
            raise SystemExit(f"{args.idu_racine} absent des parcelles de {args.denomination}")
        block = groups[uf.find(args.idu_racine)]
        print(f"bloc de {args.idu_racine} : {len(block)} parcelles")

        crit = conn.execute(
            text(SQL_CRITERES),
            {
                "project_id": args.project_id,
                "idus": block,
                "radius_m": args.radius_m,
                "libelles": CESBIO,
            },
        ).mappings().all()

    by_idu = {r["idu"]: r for r in rows}
    feats = []
    n_excl = n_anchor = 0
    for c in crit:
        if c["excluded"]:
            n_excl += 1
        if c["anchor"]:
            n_anchor += 1
        feats.append({
            "type": "Feature",
            "properties": {
                "idu": c["idu"],
                "area": float(c["area"] or 0),
                "excluded": bool(c["excluded"]),
                "anchor": bool(c["anchor"]),
                "occ_ok_area": float(c["occ_ok_area"] or 0),
            },
            "geometry": by_idu[c["idu"]]["geometry"],
        })
    print(f"critères : {n_excl} exclues, {n_anchor} ancres faune")

    parcels = parcels_from_geojson(feats)
    adj = adjacency_from_geojson(feats)
    res = search_block(
        parcels, adj, kmax=args.kmax, min_area=args.min_area, quota=args.quota,
    )
    print(
        f"poche {len(res.pocket)} | explorés {res.n_explored} | "
        f"candidats {len(res.candidates)} | truncated={res.truncated} | {res.seconds:.2f}s"
    )
    hit = [c for c in res.candidates if WANT <= set(c.idus)]
    print("cinq parcelles D 2368–2384 :", "trouvées" if hit else "absentes")
    if hit:
        c = hit[0]
        print(f"  {c.area / 10000:.2f} ha  occupation {100 * c.occ_ratio:.0f} %  ancres {c.n_anchor}")


if __name__ == "__main__":
    main()
