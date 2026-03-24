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

Conserve par ailleurs : GEOMCE, exclusion Natura 2000 (patrimoine), Miller ≥ 0.39, superficie ≥ 7 ha,
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
    project_id: str,
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
    Les tables ecocompensation_results.* sont filtrées par project_id.
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
                text("SELECT to_regclass(:r) IS NOT NULL").execution_options(no_prepare=True),
                {"r": full},
            ).scalar_one()

    def has(table: str) -> bool:
        return exists.get(table, False)

    where_clauses = [
        """
        (p.project_id = :project_id
         OR p.aoi_id = :aoi_id_str)
        """.strip()
    ]
    params = {
        "project_id": project_id,
        "aoi_id_str": aoi_id,  # colonne historique (texte) selon les données
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
    log(f"0) Parcelles brutes (project_id)                    → {n0} parcelles")
    funnel.append({"step": 0, "label": "Parcelles brutes (project_id)", "count": n0})

    if n0 == 0:
        log("⚠️ Aucune parcelle pour cette AOI, arrêt.")
        return None if not return_parcelles else ([], 0.0, funnel)

    # --- 1) Exclusion GEOMCE
    if has("ecocompensation_results.mesures_compensatoire_surf"):
        where_clauses.append(
            """
            NOT EXISTS (
                SELECT 1 FROM ecocompensation_results.mesures_compensatoire_surf ms
                WHERE ST_Intersects(p.geom_2154, ms.geom_2154)
            )
            """
        )
    if has("ecocompensation_results.mesures_compensatoire_lin"):
        where_clauses.append(
            """
            NOT EXISTS (
                SELECT 1 FROM ecocompensation_results.mesures_compensatoire_lin ml
                WHERE ST_Intersects(p.geom_2154, ml.geom_2154)
            )
            """
        )
    if has("ecocompensation_results.mesures_compensatoire_pct"):
        where_clauses.append(
            """
            NOT EXISTS (
                SELECT 1 FROM ecocompensation_results.mesures_compensatoire_pct mp
                WHERE ST_Intersects(p.geom_2154, mp.geom_2154)
            )
            """
        )
    if has("ecocompensation_results.mesures_compensatoire_commune"):
        where_clauses.append(
            """
            NOT EXISTS (
                SELECT 1 FROM ecocompensation_results.mesures_compensatoire_commune mc
                WHERE ST_Intersects(p.geom_2154, mc.geom_2154)
            )
            """
        )
    n1 = count_with_clauses(where_clauses)
    log(f"1) Exclusion mesures compensatoires (GEOMCE)         → {n1} parcelles")
    funnel.append({"step": 1, "label": "Après exclusion GEOMCE", "count": n1})

    # --- 2) Exclusion patrimoine : uniquement Natura 2000 (aligné sur filtre_uf.py)
    if has("ecocompensation_results.patrimoine_naturel"):
        where_clauses.append(
            """
            NOT EXISTS (
                SELECT 1 FROM ecocompensation_results.patrimoine_naturel pn
                WHERE pn.project_id = :project_id
                  AND pn.type_patrimoine = 'Natura 2000'
                  AND p.geom_2154 && pn.geom_2154
                  AND ST_Intersects(p.geom_2154, pn.geom_2154)
            )
            """
        )
    n2 = count_with_clauses(where_clauses)
    log(f"2) Exclusion patrimoine (Natura 2000)                 → {n2} parcelles")
    funnel.append({"step": 2, "label": "Après exclusion patrimoine Natura 2000", "count": n2})

    # --- 3) Superficie ≥ 7 ha (comparaison numérique, pas de spatial)
    area_clause = "ST_Area(p.geom_2154) >= :min_area_m2"
    n3 = count_with_clauses(where_clauses + [area_clause])
    log(f"3) Superficie ≥ 7 ha                                 → {n3} parcelles")
    funnel.append({"step": 3, "label": "Après superficie ≥ 7 ha", "count": n3})

    # --- 4) Miller ≥ 0.39 (comparaison numérique, pas de spatial)
    miller_clause = """
        (4 * PI() * ST_Area(p.geom_2154)) /
        NULLIF(ST_Perimeter(p.geom_2154)^2, 0)::double precision
        >= :miller_th
    """
    n4 = count_with_clauses(where_clauses + [area_clause, miller_clause])
    log(f"4) Miller ≥ 0.39                                     → {n4} parcelles")
    funnel.append({"step": 4, "label": "Après Miller ≥ 0.39", "count": n4})

    if n4 == 0:
        log("⚠️ Aucune parcelle après Miller + superficie, arrêt.")
        return None if not return_parcelles else ([], 0.0, funnel)

    numeric_clauses = [area_clause, miller_clause]

    # --- 5) ZDV : parcelle doit intersecter une zone de végétation (nature dans la liste)
    if options.zdv_natures and has("ecocompensation_results.zone_de_vegetation"):
        where_clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM ecocompensation_results.zone_de_vegetation z
                WHERE ST_Intersects(p.geom_2154, z.geom_2154)
                  AND z.nature = ANY(:zdv_natures)
            )
            """
        )
        n_zdv = count_with_clauses(where_clauses + numeric_clauses)
        log(f"5) ZDV intersecte (nature in {options.zdv_natures})     → {n_zdv} parcelles")
    else:
        n_zdv = count_with_clauses(where_clauses + numeric_clauses)
        if not options.zdv_natures:
            log("5) ZDV (aucune nature demandée → étape neutre)        → —")
        else:
            log(f"5) ZDV (table absente → étape neutre)                 → {n_zdv} parcelles")
    funnel.append({"step": 5, "label": "Après filtre ZDV", "count": n_zdv})

    # --- 6) Tronçons hydro : intersect ou within_radius
    if options.troncon_hydro_mode != "none" and has("ecocompensation_results.troncons_hydro"):
        if options.troncon_hydro_mode == "intersect":
            where_clauses.append(
                """
                EXISTS (
                    SELECT 1
                    FROM ecocompensation_results.troncons_hydro t
                    WHERE ST_Intersects(p.geom_2154, t.geom_2154)
                )
                """
            )
        else:
            where_clauses.append(
                """
                EXISTS (
                    SELECT 1
                    FROM ecocompensation_results.troncons_hydro t
                    WHERE ST_DWithin(p.geom_2154, t.geom_2154, :troncon_hydro_radius_m)
                )
                """
            )
        n_troncon = count_with_clauses(where_clauses + numeric_clauses)
        label = (
            "6) Tronçon hydro intersecte la parcelle"
            if options.troncon_hydro_mode == "intersect"
            else f"6) Tronçon hydro à ≤ {options.troncon_hydro_radius_m:.0f} m"
        )
        log(f"{label:<60} → {n_troncon} parcelles")
    else:
        n_troncon = count_with_clauses(where_clauses + numeric_clauses)
        log(f"6) Tronçon hydro (ignoré ou table absente)                → {n_troncon} parcelles")
    funnel.append({"step": 6, "label": "Après filtre tronçon hydro", "count": n_troncon})

    # --- 7) Surfaces hydro : intersect ou within_radius
    if options.surface_hydro_mode != "none" and has("ecocompensation_results.surfaces_hydro"):
        if options.surface_hydro_mode == "intersect":
            where_clauses.append(
                """
                EXISTS (
                    SELECT 1
                    FROM ecocompensation_results.surfaces_hydro sh
                    WHERE ST_Intersects(p.geom_2154, sh.geom_2154)
                )
                """
            )
        else:
            where_clauses.append(
                """
                EXISTS (
                    SELECT 1
                    FROM ecocompensation_results.surfaces_hydro sh
                    WHERE ST_DWithin(p.geom_2154, sh.geom_2154, :surface_hydro_radius_m)
                )
                """
            )
        n_surface_hydro = count_with_clauses(where_clauses + numeric_clauses)
        label = (
            "7) Surface hydro intersecte la parcelle"
            if options.surface_hydro_mode == "intersect"
            else f"7) Surface hydro à ≤ {options.surface_hydro_radius_m:.0f} m"
        )
        log(f"{label:<60} → {n_surface_hydro} parcelles")
    else:
        n_surface_hydro = count_with_clauses(where_clauses + numeric_clauses)
        log(f"7) Surface hydro (ignorée ou table absente)                → {n_surface_hydro} parcelles")
    funnel.append({"step": 7, "label": "Après filtre surface hydro", "count": n_surface_hydro})

    # --- 8) Candidats finaux (dans l'AOI)
    # Le rayon dynamique est supprimé : l'AOI (buffer) est définie en amont via ecocompensation.aoi.
    final_radius_km = 0.0
    final_count = n_surface_hydro

    funnel.append({"step": 8, "label": "Candidats finaux (dans l'AOI)", "count": final_count})
    log(f"\n🎯 Vrai filtre terminé : {final_count} parcelles dans l'AOI.")

    final_where_sql = " AND ".join(f"({c})" for c in (where_clauses + numeric_clauses))

    if return_parcelles:
        # Récupérer les parcelles avec attributs pour le scoring (distance, surface, Miller, proximité hydro)
        if has("ecocompensation_results.surfaces_hydro"):
            dist_hydro_sub = """
                (SELECT MIN(ST_Distance(sh.geom_2154, p.geom_2154))
                 FROM ecocompensation_results.surfaces_hydro sh
                 WHERE ST_DWithin(p.geom_2154, sh.geom_2154, :surface_hydro_radius_m)
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
            WHERE {final_where_sql};
        """
        with engine.begin() as conn:
            rows = conn.execute(
                text(sql),
                params,
            ).mappings().all()
        parcelles = [dict(r) for r in rows]
        return (parcelles, final_radius_km, funnel)

    return None


def _score_with_weights(p: dict, opts_dto) -> tuple[int, list[dict]]:
    """
    Scoring avec poids et seuils configurables (passés via FiltreOptionsDTO).
    Anciennement dans main.py — déplacé ici pour garder main.py sans logique métier.
    """
    dist_m = p.get("distance_centre_m") or 999999.0
    surface_ha = float(p.get("surface_ha") or 0)
    miller = float(p.get("miller") or 0)
    dist_hydro_m = p.get("dist_surface_hydro_m")

    thr_dist_2_m = opts_dto.score_threshold_dist_2km * 1000.0
    thr_dist_5_m = opts_dto.score_threshold_dist_5km * 1000.0

    pts = 0
    score_details: list[dict] = []

    if dist_m < thr_dist_2_m:
        pts += opts_dto.score_dist_lt2km
        score_details.append(
            {
                "critere": "Distance au centre AOI",
                "points": opts_dto.score_dist_lt2km,
                "raison": f"< {opts_dto.score_threshold_dist_2km} km ({dist_m/1000:.1f} km)",
            }
        )
    elif dist_m < thr_dist_5_m:
        pts += opts_dto.score_dist_lt5km
        score_details.append(
            {
                "critere": "Distance au centre AOI",
                "points": opts_dto.score_dist_lt5km,
                "raison": f"{opts_dto.score_threshold_dist_2km}–{opts_dto.score_threshold_dist_5km} km ({dist_m/1000:.1f} km)",
            }
        )
    else:
        pts += opts_dto.score_dist_lt10km
        score_details.append(
            {
                "critere": "Distance au centre AOI",
                "points": opts_dto.score_dist_lt10km,
                "raison": f"{opts_dto.score_threshold_dist_5km}–10 km ({dist_m/1000:.1f} km)",
            }
        )

    if surface_ha >= opts_dto.score_threshold_surface_ha:
        pts += opts_dto.score_surface_ge20ha
        score_details.append(
            {
                "critere": "Surface",
                "points": opts_dto.score_surface_ge20ha,
                "raison": f"≥ {opts_dto.score_threshold_surface_ha} ha ({surface_ha:.1f} ha)",
            }
        )
    else:
        score_details.append(
            {
                "critere": "Surface",
                "points": 0,
                "raison": f"< {opts_dto.score_threshold_surface_ha} ha ({surface_ha:.1f} ha)",
            }
        )

    if miller >= opts_dto.score_threshold_miller:
        pts += opts_dto.score_miller_ge05
        score_details.append(
            {
                "critere": "Coefficient de Miller",
                "points": opts_dto.score_miller_ge05,
                "raison": f"≥ {opts_dto.score_threshold_miller} ({miller:.2f})",
            }
        )
    else:
        score_details.append(
            {
                "critere": "Coefficient de Miller",
                "points": 0,
                "raison": f"< {opts_dto.score_threshold_miller} ({miller:.2f})",
            }
        )

    if dist_hydro_m is not None and dist_hydro_m < opts_dto.score_threshold_hydro_m:
        pts += opts_dto.score_hydro_lt100m
        score_details.append(
            {
                "critere": "Proximité hydro",
                "points": opts_dto.score_hydro_lt100m,
                "raison": f"< {opts_dto.score_threshold_hydro_m:.0f} m ({dist_hydro_m:.0f} m)",
            }
        )
    else:
        score_details.append(
            {
                "critere": "Proximité hydro",
                "points": 0,
                "raison": f"{dist_hydro_m:.0f} m" if dist_hydro_m else "—",
            }
        )

    return pts, score_details


def run_filter_and_score(
    engine,
    project_id: str,
    aoi_id: str,
    cx: float,
    cy: float,
    options: "FiltreOptions",
    opts_dto,
) -> dict:
    """
    Enchaîne run() + scoring. Retourne le dict complet attendu par la route /filter.
    Anciennement inline dans main.py/run_filter().

    :param opts_dto: FiltreOptionsDTO (poids + seuils du scoring).
    :return: {total, final_radius_km, parcelles, funnel} ou vide si pas de résultat.
    """
    result = run(
        engine,
        project_id,
        aoi_id,
        cx,
        cy,
        options,
        return_parcelles=True,
        miller_threshold=opts_dto.miller_threshold,
        min_area_ha=opts_dto.min_area_ha,
        radius_start_km=opts_dto.radius_start_km,
        radius_min_km=opts_dto.radius_min_km,
        target_count=opts_dto.target_count,
    )

    if result is None:
        return {"parcelles": [], "final_radius_km": 0, "total": 0, "funnel": []}

    parcelles_raw, final_radius_km, funnel = result

    scored = []
    for p in parcelles_raw:
        pts, score_details = _score_with_weights(p, opts_dto)
        scored.append(
            {
                "idu": p.get("idu"),
                "code_insee": p.get("code_insee"),
                "section": p.get("section"),
                "numero": p.get("numero"),
                "surface_ha": round(float(p.get("surface_ha") or 0), 2),
                "miller": round(float(p.get("miller") or 0), 4),
                "distance_km": round((p.get("distance_centre_m") or 0) / 1000, 2),
                "dist_hydro_m": p.get("dist_surface_hydro_m"),
                "score": pts,
                "score_details": score_details,
            }
        )

    scored.sort(key=lambda x: (-x["score"], x["distance_km"]))

    # Limite : les X meilleures parcelles (après scoring). target_count ≤ 0 = pas de limite.
    # Le tri « meilleur » est calculé en Python ; on tronque la liste triée (pas de LIMIT SQL ici).
    limit = opts_dto.target_count
    if limit > 0:
        scored = scored[:limit]

    for i, p in enumerate(scored, 1):
        p["rank"] = i

    return {
        "total": len(scored),
        "final_radius_km": final_radius_km,
        "parcelles": scored,
        "funnel": funnel,
    }


def main():
    engine = get_engine()

    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                SELECT p.id, p.aoi_id,
                       ST_X(ST_Centroid(a.geom_2154)) AS cx,
                       ST_Y(ST_Centroid(a.geom_2154)) AS cy
                FROM ecocompensation.projects p
                JOIN ecocompensation.aoi a ON a.id = p.aoi_id
                ORDER BY p.created_at DESC
                LIMIT 1;
                """
            )
        ).mappings().one_or_none()

    if row is None:
        print("⚠️ Aucun projet avec AOI trouvé.")
        return

    project_id = str(row["id"])
    aoi_id = str(row["aoi_id"])
    cx = row["cx"]
    cy = row["cy"]

    print(f"🔗 Project id     : {project_id}")
    print(f"   AOI id        : {aoi_id}")

    options = FiltreOptions.defaut()
    print(f"\n🌲 ZDV natures (défaut)     : {options.zdv_natures}")
    print(f"🫧 Tronçon hydro (défaut)   : {options.troncon_hydro_mode}")
    print(f"💧 Surface hydro (défaut)   : {options.surface_hydro_mode} (rayon {options.surface_hydro_radius_m:.0f} m)")
    print(f"⭕ Miller                   : 0.39  |  📐 Superficie min : 7 ha")
    print(f"🎯 Cible sortie             : ≤ {TARGET_COUNT} parcelles\n")

    print("=== Vrai filtre (ZDV + hydro + Miller + area + distance) ===\n")
    run(engine, project_id, aoi_id, cx, cy, options)


if __name__ == "__main__":
    main()
