#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vrai_filtre.py
==============

Filtre "vrai" des parcelles avec critères optionnels ZDV et hydro, en vue
d'une interface qui enverra les choix au process métier.

Paramètres (dynamiques, surchargeables par une API plus tard) :
  - zdv_natures : liste de natures de zone de végétation (parcelle doit
    intersecter une ZDV avec l'une de ces natures). [] = pas de filtre ZDV.
  - troncon_hydro_mode : "none" | "intersect" | "within_radius"
  - troncon_hydro_radius_m : rayon (m) si mode "within_radius"
  - surface_hydro_mode : "none" | "intersect" | "within_radius"
  - surface_hydro_radius_m : rayon (m) si mode "within_radius"

Valeurs en dur pour l'instant :
  - ZDV : ['Forêt ouverte']
  - Tronçon hydro : intersect (cours d'eau qui intersecte la parcelle)
  - Surface hydro : within_radius 500 m (surface d'eau à moins de 500 m)

Conserve par ailleurs : GEOMCE, patrimoine naturel, Miller ≥ 0.39, superficie ≥ 7 ha,
puis descente du rayon jusqu'à ≤ TARGET_COUNT parcelles.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text

from db import get_engine

# Cible sortie (à rendre paramétrable par l'interface)
TARGET_COUNT = 50
RADIUS_START_KM = 10.0
RADIUS_MIN_KM = 1.0

# Natures ZDV possibles (alignées sur front.html)
ZDV_NATURES_DISPONIBLES = [
    "Bois",
    "Forêt fermée de conifères",
    "Forêt fermée de feuillus",
    "Forêt fermée mixte",
    "Forêt ouverte",
    "Haie",
    "Lande ligneuse",
    "Peupleraie",
    "Verger",
    "Vigne",
]

HydroMode = Literal["none", "intersect", "within_radius"]


@dataclass
class FiltreOptions:
    """Options du filtre (passables par l'interface plus tard)."""

    zdv_natures: list[str]
    troncon_hydro_mode: HydroMode
    troncon_hydro_radius_m: float
    surface_hydro_mode: HydroMode
    surface_hydro_radius_m: float

    @staticmethod
    def defaut() -> FiltreOptions:
        """Valeurs en dur pour l'instant."""
        return FiltreOptions(
            zdv_natures=["Forêt ouverte"],
            troncon_hydro_mode="intersect",
            troncon_hydro_radius_m=500.0,
            surface_hydro_mode="within_radius",
            surface_hydro_radius_m=500.0,
        )


def run(
    engine,
    aoi_id: str,
    cx: float,
    cy: float,
    options: FiltreOptions,
    *,
    return_parcelles: bool = False,
    miller_threshold: float = 0.39,
    min_area_ha: float = 7.0,
    target_count: int = 50,
    radius_start_km: float = 10.0,
    radius_min_km: float = 1.0,
):
    """
    Exécute le vrai filtre. Si return_parcelles=True, ne fait pas les prints
    et renvoie (liste de dicts, final_radius_km, funnel).
    Sinon affiche les effectifs et renvoie None.
    """
    min_area_m2 = min_area_ha * 10_000.0
    funnel: list[dict] = []

    tables_to_check = [
        "ecocompensation_results.mesures_compensatoire_surf",
        "ecocompensation_results.mesures_compensatoire_lin",
        "ecocompensation_results.mesures_compensatoire_pct",
        "ecocompensation_results.mesures_compensatoire_commune",
        "ecocompensation_results.patrimoine_naturel",
        "ecocompensation_results.zone_de_vegetation",
        "ecocompensation_results.troncons_hydro",
        "ecocompensation_results.surfaces_hydro",
    ]
    exists: dict[str, bool] = {}
    with engine.begin() as conn:
        for full in tables_to_check:
            exists[full] = conn.execute(
                text("SELECT to_regclass(:r) IS NOT NULL"),
                {"r": full},
            ).scalar_one()

    def has(table: str) -> bool:
        return exists.get(table, False)

    where_clauses = ["p.aoi_id = :aoi_id"]
    params = {
        "aoi_id": aoi_id,
        "cx": cx,
        "cy": cy,
        "radius_m": 0.0,
        "min_area_m2": min_area_m2,
        "miller_th": miller_threshold,
        "zdv_natures": options.zdv_natures,
        "troncon_hydro_radius_m": options.troncon_hydro_radius_m,
        "surface_hydro_radius_m": options.surface_hydro_radius_m,
    }

    def log(msg: str) -> None:
        if not return_parcelles:
            print(msg)

    def count_with_clauses(clauses: list[str], extra_params: dict | None = None) -> int:
        where_sql = " AND ".join(f"({c})" for c in clauses)
        p = {**params, **(extra_params or {})}
        with engine.begin() as conn:
            return conn.execute(
                text(
                    f"""
                    SELECT COUNT(*) AS n
                    FROM ecocompensation_results.parcelles p
                    WHERE {where_sql};
                    """
                ),
                p,
            ).scalar_one()

    # --- 0) Parcelles brutes
    n0 = count_with_clauses(where_clauses)
    log(f"0) Parcelles brutes (aoi_id seul)                    → {n0} parcelles")
    funnel.append({"step": 0, "label": "Parcelles brutes (aoi_id)", "count": n0})

    if n0 == 0:
        log("⚠️ Aucune parcelle pour cette AOI, arrêt.")
        return None if not return_parcelles else ([], 0.0, funnel)

    # --- 1) Exclusion GEOMCE
    if has("ecocompensation_results.mesures_compensatoire_surf"):
        where_clauses.append(
            """
            NOT EXISTS (
                SELECT 1 FROM ecocompensation_results.mesures_compensatoire_surf ms
                WHERE ms.aoi_id = p.aoi_id
                  AND ST_Intersects(p.geom_2154, ms.geom_2154)
            )
            """
        )
    if has("ecocompensation_results.mesures_compensatoire_lin"):
        where_clauses.append(
            """
            NOT EXISTS (
                SELECT 1 FROM ecocompensation_results.mesures_compensatoire_lin ml
                WHERE ml.aoi_id = p.aoi_id
                  AND ST_Intersects(p.geom_2154, ml.geom_2154)
            )
            """
        )
    if has("ecocompensation_results.mesures_compensatoire_pct"):
        where_clauses.append(
            """
            NOT EXISTS (
                SELECT 1 FROM ecocompensation_results.mesures_compensatoire_pct mp
                WHERE mp.aoi_id = p.aoi_id
                  AND ST_Intersects(p.geom_2154, mp.geom_2154)
            )
            """
        )
    if has("ecocompensation_results.mesures_compensatoire_commune"):
        where_clauses.append(
            """
            NOT EXISTS (
                SELECT 1 FROM ecocompensation_results.mesures_compensatoire_commune mc
                WHERE mc.aoi_id = p.aoi_id
                  AND ST_Intersects(p.geom_2154, mc.geom_2154)
            )
            """
        )
    n1 = count_with_clauses(where_clauses)
    log(f"1) Exclusion mesures compensatoires (GEOMCE)         → {n1} parcelles")
    funnel.append({"step": 1, "label": "Après exclusion GEOMCE", "count": n1})

    # --- 2) Exclusion patrimoine naturel
    if has("ecocompensation_results.patrimoine_naturel"):
        where_clauses.append(
            """
            NOT EXISTS (
                SELECT 1 FROM ecocompensation_results.patrimoine_naturel pn
                WHERE pn.aoi_id = p.aoi_id
                  AND ST_Intersects(p.geom_2154, pn.geom_2154)
            )
            """
        )
    n2 = count_with_clauses(where_clauses)
    log(f"2) Exclusion patrimoine naturel                      → {n2} parcelles")
    funnel.append({"step": 2, "label": "Après exclusion patrimoine naturel", "count": n2})

    # --- 3) ZDV : parcelle doit intersecter une zone de végétation (nature dans la liste)
    if options.zdv_natures and has("ecocompensation_results.zone_de_vegetation"):
        where_clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM ecocompensation_results.zone_de_vegetation z
                WHERE z.aoi_id = p.aoi_id
                  AND ST_Intersects(p.geom_2154, z.geom_2154)
                  AND z.nature = ANY(:zdv_natures)
            )
            """
        )
        n3 = count_with_clauses(where_clauses)
        log(f"3) ZDV intersecte (nature in {options.zdv_natures})     → {n3} parcelles")
    else:
        if not options.zdv_natures:
            log("3) ZDV (aucune nature demandée → étape neutre)        → —")
            n3 = count_with_clauses(where_clauses)
        else:
            n3 = count_with_clauses(where_clauses)
            log(f"3) ZDV (table absente → étape neutre)                 → {n3} parcelles")
    funnel.append({"step": 3, "label": "Après filtre ZDV", "count": n3})

    # --- 4) Tronçons hydro : intersect ou within_radius
    if options.troncon_hydro_mode != "none" and has("ecocompensation_results.troncons_hydro"):
        if options.troncon_hydro_mode == "intersect":
            where_clauses.append(
                """
                EXISTS (
                    SELECT 1
                    FROM ecocompensation_results.troncons_hydro t
                    WHERE t.aoi_id = p.aoi_id
                      AND ST_Intersects(p.geom_2154, t.geom_2154)
                )
                """
            )
            label = "4) Tronçon hydro intersecte la parcelle"
        else:
            where_clauses.append(
                """
                EXISTS (
                    SELECT 1
                    FROM ecocompensation_results.troncons_hydro t
                    WHERE t.aoi_id = p.aoi_id
                      AND ST_DWithin(p.geom_2154, t.geom_2154, :troncon_hydro_radius_m)
                )
                """
            )
            label = f"4) Tronçon hydro à ≤ {options.troncon_hydro_radius_m:.0f} m"
        n4 = count_with_clauses(where_clauses)
        log(f"{label:<60} → {n4} parcelles")
    else:
        n4 = count_with_clauses(where_clauses)
        log(f"4) Tronçon hydro (ignoré ou table absente)                → {n4} parcelles")
    funnel.append({"step": 4, "label": "Après filtre tronçon hydro", "count": n4})

    # --- 5) Surfaces hydro : intersect ou within_radius
    if options.surface_hydro_mode != "none" and has("ecocompensation_results.surfaces_hydro"):
        if options.surface_hydro_mode == "intersect":
            where_clauses.append(
                """
                EXISTS (
                    SELECT 1
                    FROM ecocompensation_results.surfaces_hydro sh
                    WHERE sh.aoi_id = p.aoi_id
                      AND ST_Intersects(p.geom_2154, sh.geom_2154)
                )
                """
            )
            label = "5) Surface hydro intersecte la parcelle"
        else:
            where_clauses.append(
                """
                EXISTS (
                    SELECT 1
                    FROM ecocompensation_results.surfaces_hydro sh
                    WHERE sh.aoi_id = p.aoi_id
                      AND ST_DWithin(p.geom_2154, sh.geom_2154, :surface_hydro_radius_m)
                )
                """
            )
            label = f"5) Surface hydro à ≤ {options.surface_hydro_radius_m:.0f} m"
        n5 = count_with_clauses(where_clauses)
        log(f"{label:<60} → {n5} parcelles")
    else:
        n5 = count_with_clauses(where_clauses)
        log(f"5) Surface hydro (ignorée ou table absente)                → {n5} parcelles")
    funnel.append({"step": 5, "label": "Après filtre surface hydro", "count": n5})

    # --- 6) Miller ≥ 0.39
    where_clauses.append(
        """
        (4 * PI() * ST_Area(p.geom_2154)) /
        NULLIF(ST_Perimeter(p.geom_2154)^2, 0)::double precision
        >= :miller_th
        """
    )
    n6 = count_with_clauses(where_clauses)
    log(f"6) Miller ≥ 0.39                                     → {n6} parcelles")
    funnel.append({"step": 6, "label": "Après Miller ≥ 0.39", "count": n6})

    # --- 7) Superficie ≥ 7 ha
    where_clauses.append("ST_Area(p.geom_2154) >= :min_area_m2")
    n7 = count_with_clauses(where_clauses)
    log(f"7) Superficie ≥ 7 ha                                 → {n7} parcelles")
    funnel.append({"step": 7, "label": "Après superficie ≥ 7 ha", "count": n7})

    if n7 == 0:
        log("⚠️ Aucune parcelle après Miller + superficie, arrêt.")
        return None if not return_parcelles else ([], 0.0, funnel)

    # --- 8) Distance dynamique jusqu'à ≤ TARGET_COUNT
    distance_clause = """
        ST_DWithin(
            p.geom_2154,
            ST_SetSRID(ST_MakePoint(:cx, :cy), 2154),
            :radius_m
        )
    """
    clauses_with_distance = where_clauses + [distance_clause]

    final_radius_km = None
    final_count = None
    radius_km = radius_start_km
    while radius_km >= radius_min_km:
        radius_m = radius_km * 1000.0
        n = count_with_clauses(clauses_with_distance, {"radius_m": radius_m})
        log(f"8) Distance ≤ {radius_km:.0f} km du centre AOI                  → {n} parcelles")
        if n <= target_count:
            final_radius_km = radius_km
            final_count = n
            break
        radius_km -= 1.0

    if final_radius_km is None:
        log(
            f"\n⚠️ Même à {radius_min_km:.0f} km on a encore plus de {target_count} parcelles. "
            f"Sortie avec rayon = {radius_min_km:.0f} km."
        )
        final_radius_km = radius_min_km
        final_count = count_with_clauses(
            clauses_with_distance,
            {"radius_m": final_radius_km * 1000.0},
        )

    funnel.append({"step": 8, "label": f"Distance ≤ {final_radius_km:.0f} km (final)", "count": final_count})
    log(f"\n🎯 Vrai filtre terminé : {final_count} parcelles dans un rayon de {final_radius_km:.0f} km (cible ≤ {target_count}).")

    if return_parcelles:
        # Récupérer les parcelles avec attributs pour le scoring (distance, surface, Miller, proximité hydro)
        where_sql = " AND ".join(f"({c})" for c in clauses_with_distance)
        select_radius = {"radius_m": final_radius_km * 1000.0}
        if has("ecocompensation_results.surfaces_hydro"):
            dist_hydro_sub = """
                (SELECT MIN(ST_Distance(sh.geom_2154, p.geom_2154))
                 FROM ecocompensation_results.surfaces_hydro sh
                 WHERE sh.aoi_id = p.aoi_id
                   AND ST_DWithin(p.geom_2154, sh.geom_2154, :surface_hydro_radius_m)
                ) AS dist_surface_hydro_m
            """
        else:
            dist_hydro_sub = "NULL::double precision AS dist_surface_hydro_m"
        sql = f"""
            SELECT
                p.idu,
                p.code_insee,
                p.section,
                p.numero,
                ST_Area(p.geom_2154) / 10000.0 AS surface_ha,
                (4 * PI() * ST_Area(p.geom_2154)) / NULLIF(ST_Perimeter(p.geom_2154)^2, 0)::double precision AS miller,
                ST_Distance(p.geom_2154, ST_SetSRID(ST_MakePoint(:cx, :cy), 2154)) AS distance_centre_m,
                {dist_hydro_sub}
            FROM ecocompensation_results.parcelles p
            WHERE {where_sql};
        """
        with engine.begin() as conn:
            rows = conn.execute(
                text(sql),
                {**params, **select_radius},
            ).mappings().all()
        parcelles = [dict(r) for r in rows]
        return (parcelles, final_radius_km, funnel)

    return None


def main():
    engine = get_engine()

    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                SELECT
                    id,
                    buffer_m,
                    ST_X(ST_Centroid(geom_2154)) AS cx,
                    ST_Y(ST_Centroid(geom_2154)) AS cy
                FROM ecocompensation.aoi
                ORDER BY created_at DESC
                LIMIT 1;
                """
            )
        ).mappings().one_or_none()

    if row is None:
        print("⚠️ Aucune AOI trouvée dans ecocompensation.aoi.")
        return

    aoi_id = str(row["id"])
    buffer_m = row["buffer_m"]
    cx = row["cx"]
    cy = row["cy"]

    print(f"🔗 AOI id        : {aoi_id}")
    print(f"   buffer_m     : {buffer_m} m")
    print(f"   centre 2154  : x={cx}, y={cy}")

    options = FiltreOptions.defaut()
    print(f"\n🌲 ZDV natures (défaut)     : {options.zdv_natures}")
    print(f"🫧 Tronçon hydro (défaut)   : {options.troncon_hydro_mode}")
    print(f"💧 Surface hydro (défaut)   : {options.surface_hydro_mode} (rayon {options.surface_hydro_radius_m:.0f} m)")
    print(f"⭕ Miller                   : 0.39  |  📐 Superficie min : 7 ha")
    print(f"🎯 Cible sortie             : ≤ {TARGET_COUNT} parcelles\n")

    print("=== Vrai filtre (ZDV + hydro + Miller + area + distance) ===\n")
    run(engine, aoi_id, cx, cy, options)


if __name__ == "__main__":
    main()
