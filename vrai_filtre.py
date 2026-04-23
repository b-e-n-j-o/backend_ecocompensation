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
  - cesbio_libelles : libellés CESBIO (couverture sol). [] = pas de filtre CESBIO.
  - carhab_nom_eunis : libellés EUNIS Carhab (nom_eunis). [] = pas de filtre Carhab.
  - troncon_hydro_mode : "none" | "intersect" | "within_radius"
  - troncon_hydro_radius_m : rayon (m) si mode "within_radius"
  - surface_hydro_mode : "none" | "intersect" | "within_radius"
  - surface_hydro_radius_m : rayon (m) si mode "within_radius"

Valeurs en dur pour l'instant :
  - ZDV : ['Forêt ouverte']
  - Tronçon hydro : intersect (cours d'eau qui intersecte la parcelle)
  - Surface hydro : within_radius 500 m (surface d'eau à moins de 500 m)

Conserve par ailleurs : GEOMCE, Natura 2000 (table natura2000, mode intersect / exclure / ignorer par défaut),
Miller ≥ 0.39, superficie ≥ 7 ha,
puis descente du rayon jusqu'à ≤ TARGET_COUNT parcelles.

Exclusion systématique (sans option UI) : les parcelles qui intersectent la géométrie du projet à compenser
(``ecocompensation.foncier`` via ``projects.foncier_id``) sont exclues — sauf si aucun foncier n'est lié au projet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

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
ArrachageVignesMode = Literal["ignore", "intersect", "exclude"]
ZoneHumideMode = Literal["ignore", "intersect", "exclude"]
LayerIntersectMode = Literal["ignore", "intersect", "exclude"]


@dataclass
class FiltreOptions:
    """Options du filtre (passables par l'interface plus tard)."""

    vegetation_hybride: dict
    carhab_nom_eunis: list[str]
    excluded_layers: list[str]
    ebc_mode: LayerIntersectMode
    natura2000_mode: LayerIntersectMode
    reserves_naturelles_mode: LayerIntersectMode
    znieff_mode: LayerIntersectMode
    arrachage_vignes_mode: ArrachageVignesMode
    zone_humide_mode: ZoneHumideMode
    remontee_nappes_classefiab: list[str]
    troncon_hydro_mode: HydroMode
    troncon_hydro_radius_m: float
    surface_hydro_mode: HydroMode
    surface_hydro_radius_m: float
    faune_criteria: list[dict]

    @staticmethod
    def defaut() -> FiltreOptions:
        """Valeurs en dur pour l'instant."""
        return FiltreOptions(
            vegetation_hybride={
                "zdv_natures": [],
                "cesbio_libelles": [],
                "mode": "OR",
            },
            carhab_nom_eunis=[],
            excluded_layers=["geomce"],
            ebc_mode="ignore",
            natura2000_mode="exclude",
            reserves_naturelles_mode="ignore",
            znieff_mode="ignore",
            arrachage_vignes_mode="ignore",
            zone_humide_mode="ignore",
            remontee_nappes_classefiab=[],
            troncon_hydro_mode="intersect",
            troncon_hydro_radius_m=500.0,
            surface_hydro_mode="within_radius",
            surface_hydro_radius_m=500.0,
            faune_criteria=[],
        )

    @property
    def zdv_natures(self) -> list[str]:
        return list(self.vegetation_hybride.get("zdv_natures", []))

    @property
    def cesbio_libelles(self) -> list[str]:
        return list(self.vegetation_hybride.get("cesbio_libelles", []))

    @property
    def vegetation_hybride_mode(self) -> str:
        return str(self.vegetation_hybride.get("mode", "OR")).upper()


def run(
    engine,
    project_id: str,
    aoi_id: str,
    cx: float,
    cy: float,
    options: FiltreOptions,
    *,
    return_parcelles: bool = False,
    funnel_mode: bool = False,
    miller_threshold: float = 0.39,
    min_area_ha: float = 7.0,
    target_count: int = 50,
    radius_start_km: float = 10.0,
    radius_min_km: float = 1.0,
    progress_callback: Callable[[str, int], None] | None = None,
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
        "ecocompensation_results.natura2000",
        "ecocompensation_results.ebc",
        "ecocompensation_results.reserves_naturelles",
        "ecocompensation_results.znieff",
        "ecocompensation_results.bd_topo_et_cesbio",
        "ecocompensation_results.remontee_de_nappes",
        "ecocompensation_results.troncons_hydro",
        "ecocompensation_results.surfaces_hydro",
        "ecocompensation_results.fauna",
        "ecocompensation_results.parcelles_project_indesirables",
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

    def _resolve_taxnomval_column(conn, full_table: str) -> str | None:
        if "." not in full_table:
            return None
        schema, table = full_table.split(".", 1)
        # Priorité : ancienne convention taxnomval, puis colonnes présentes
        # (ex: nom_vernaculaire dans ecocompensation.fauna).
        candidates = ["taxnomval", "nom_vernaculaire", "nom_taxref", "tax_nom_val", "nom_vern"]
        for cand in candidates:
            row = conn.execute(
                text(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = :schema
                      AND table_name = :table
                      AND lower(column_name) = :col
                    LIMIT 1
                    """
                ),
                {"schema": schema, "table": table, "col": cand},
            ).mappings().one_or_none()
            if row and row.get("column_name"):
                col = str(row["column_name"])
                return f'"{col}"' if col != col.lower() else col
        return None

    with engine.begin() as conn:
        fauna_tax_col: dict[str, str | None] = {
            t: _resolve_taxnomval_column(conn, t)
            for t in (
                "ecocompensation_results.fauna",
            )
        }

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
        "cesbio_libelles": options.cesbio_libelles,
        "carhab_nom_eunis": options.carhab_nom_eunis,
        "remontee_nappes_classefiab": options.remontee_nappes_classefiab,
        "troncon_hydro_radius_m": options.troncon_hydro_radius_m,
        "surface_hydro_radius_m": options.surface_hydro_radius_m,
    }
    excluded_layers = {str(x).strip() for x in getattr(options, "excluded_layers", []) if str(x).strip()}

    def log(msg: str) -> None:
        if not return_parcelles:
            print(msg)

    def emit_progress(label: str, count: int) -> None:
        if progress_callback is not None:
            progress_callback(label, int(count))

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

    def maybe_count(clauses: list[str], extra_params: dict | None = None) -> int:
        if not funnel_mode:
            return -1
        return count_with_clauses(clauses, extra_params)

    # --- 0) Parcelles brutes
    n0 = count_with_clauses(where_clauses)
    log(f"0) Parcelles brutes (project_id)                    → {n0} parcelles")
    emit_progress("Parcelles brutes (project_id)", n0)
    step_idx = 0
    if funnel_mode:
        funnel.append({"step": step_idx, "label": "Parcelles brutes (project_id)", "count": n0})

    if n0 == 0:
        log("⚠️ Aucune parcelle pour cette AOI, arrêt.")
        return None if not return_parcelles else ([], 0.0, funnel)

    # --- 0b) Exclusion foncier du projet (si présent)
    where_clauses.append(
        """
        NOT EXISTS (
            SELECT 1
            FROM ecocompensation.projects pr
            JOIN ecocompensation.foncier f ON f.id = pr.foncier_id
            WHERE pr.id = CAST(:project_id AS uuid)
              AND p.geom_2154 && f.geom_2154
              AND ST_Intersects(p.geom_2154, f.geom_2154)
        )
        """
    )
    n0b = maybe_count(where_clauses)
    log(f"0b) Exclusion parcelles du foncier projet            → {n0b} parcelles")
    emit_progress("Après exclusion foncier projet", n0b)
    if funnel_mode:
        step_idx += 1
        funnel.append({"step": step_idx, "label": "Après exclusion foncier projet", "count": n0b})

    if funnel_mode and n0b == 0:
        log("⚠️ Aucune parcelle après exclusion du foncier projet, arrêt.")
        return None if not return_parcelles else ([], 0.0, funnel)

    # --- 1) Superficie ≥ min_area_ha (numérique)
    area_clause = "ST_Area(p.geom_2154) >= :min_area_m2"
    where_clauses.append(area_clause)
    n1 = maybe_count(where_clauses)
    log(f"1) Superficie ≥ {min_area_ha} ha                         → {n1} parcelles")
    emit_progress(f"Après superficie ≥ {min_area_ha} ha", n1)
    if funnel_mode:
        step_idx += 1
        funnel.append({"step": step_idx, "label": f"Après superficie ≥ {min_area_ha} ha", "count": n1})

    # --- 2) Miller ≥ seuil (numérique)
    miller_clause = """
        (4 * PI() * ST_Area(p.geom_2154)) /
        NULLIF(ST_Perimeter(p.geom_2154)^2, 0)::double precision
        >= :miller_th
    """
    where_clauses.append(miller_clause)
    n2 = maybe_count(where_clauses)
    log(f"2) Miller ≥ {miller_threshold}                             → {n2} parcelles")
    emit_progress(f"Après Miller ≥ {miller_threshold}", n2)
    if funnel_mode:
        step_idx += 1
        funnel.append({"step": step_idx, "label": f"Après Miller ≥ {miller_threshold}", "count": n2})

    if funnel_mode and n2 == 0:
        log("⚠️ Aucune parcelle après Miller + superficie, arrêt.")
        return None if not return_parcelles else ([], 0.0, funnel)

    # --- 3) Exclusion GEOMCE (pilotée par excluded_layers)
    if "geomce" in excluded_layers and has("ecocompensation_results.mesures_compensatoire_surf"):
        where_clauses.append(
            """
            NOT EXISTS (
                SELECT 1 FROM ecocompensation_results.mesures_compensatoire_surf ms
                WHERE ST_Intersects(p.geom_2154, ms.geom_2154)
            )
            """
        )
    if "geomce" in excluded_layers and has("ecocompensation_results.mesures_compensatoire_lin"):
        where_clauses.append(
            """
            NOT EXISTS (
                SELECT 1 FROM ecocompensation_results.mesures_compensatoire_lin ml
                WHERE ST_Intersects(p.geom_2154, ml.geom_2154)
            )
            """
        )
    if "geomce" in excluded_layers and has("ecocompensation_results.mesures_compensatoire_pct"):
        where_clauses.append(
            """
            NOT EXISTS (
                SELECT 1 FROM ecocompensation_results.mesures_compensatoire_pct mp
                WHERE ST_Intersects(p.geom_2154, mp.geom_2154)
            )
            """
        )
    if "geomce" in excluded_layers and has("ecocompensation_results.mesures_compensatoire_commune"):
        where_clauses.append(
            """
            NOT EXISTS (
                SELECT 1 FROM ecocompensation_results.mesures_compensatoire_commune mc
                WHERE ST_Intersects(p.geom_2154, mc.geom_2154)
            )
            """
        )
    geomce_applied = "geomce" in excluded_layers and (
        has("ecocompensation_results.mesures_compensatoire_surf")
        or has("ecocompensation_results.mesures_compensatoire_lin")
        or has("ecocompensation_results.mesures_compensatoire_pct")
        or has("ecocompensation_results.mesures_compensatoire_commune")
    )
    n3 = maybe_count(where_clauses)
    log(f"3) Exclusion mesures compensatoires (GEOMCE)         → {n3} parcelles")
    emit_progress("Après exclusion GEOMCE", n3)
    if funnel_mode and geomce_applied:
        step_idx += 1
        funnel.append({"step": step_idx, "label": "Après exclusion GEOMCE", "count": n3})

    # --- 3b) Exclusion du pool indésirable projet (pilotée par excluded_layers)
    if "project_indesirables" in excluded_layers and has("ecocompensation_results.parcelles_project_indesirables"):
        where_clauses.append(
            """
            NOT EXISTS (
                SELECT 1
                FROM ecocompensation_results.parcelles_project_indesirables pi
                WHERE pi.project_id = CAST(:project_id AS uuid)
                  AND pi.idu = p.idu
            )
            """
        )
    n3b = maybe_count(where_clauses)
    log(f"3b) Exclusion parcelles indésirables projet               → {n3b} parcelles")
    emit_progress("Après exclusion indésirables projet", n3b)
    if funnel_mode and "project_indesirables" in excluded_layers and has("ecocompensation_results.parcelles_project_indesirables"):
        step_idx += 1
        funnel.append({"step": step_idx, "label": "Après exclusion indésirables projet", "count": n3b})

    # --- 4) Natura 2000 (SIC / ZPS) : intersect / exclure / ignorer
    if options.natura2000_mode == "intersect" and has("ecocompensation_results.natura2000"):
        where_clauses.append(
            """
            EXISTS (
                SELECT 1 FROM ecocompensation_results.natura2000 n2
                WHERE n2.project_id = CAST(:project_id AS uuid)
                  AND p.geom_2154 && n2.geom_2154
                  AND ST_Intersects(p.geom_2154, n2.geom_2154)
            )
            """
        )
    elif options.natura2000_mode == "exclude" and has("ecocompensation_results.natura2000"):
        where_clauses.append(
            """
            NOT EXISTS (
                SELECT 1 FROM ecocompensation_results.natura2000 n2
                WHERE n2.project_id = CAST(:project_id AS uuid)
                  AND p.geom_2154 && n2.geom_2154
                  AND ST_Intersects(p.geom_2154, n2.geom_2154)
            )
            """
        )
    n4 = maybe_count(where_clauses)
    _natura_label = (
        "intersection"
        if options.natura2000_mode == "intersect"
        else ("exclusion" if options.natura2000_mode == "exclude" else "ignoré")
    )
    log(f"4) Natura 2000 ({_natura_label})                          → {n4} parcelles")
    emit_progress(f"Après filtre Natura 2000 ({_natura_label})", n4)
    natura_applied = options.natura2000_mode in ("intersect", "exclude") and has("ecocompensation_results.natura2000")
    if funnel_mode and natura_applied:
        step_idx += 1
        funnel.append({"step": step_idx, "label": f"Après filtre Natura 2000 ({_natura_label})", "count": n4})

    # --- 11) Remontée de nappes (filtrage sur CLASSEFIAB)
    if options.remontee_nappes_classefiab and has("ecocompensation_results.remontee_de_nappes"):
        where_clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM ecocompensation_results.remontee_de_nappes rdn
                WHERE rdn.project_id = CAST(:project_id AS uuid)
                  AND rdn.geom_2154 IS NOT NULL
                  AND p.geom_2154 && rdn.geom_2154
                  AND ST_Intersects(p.geom_2154, rdn.geom_2154)
                  AND rdn.classefiab = ANY(:remontee_nappes_classefiab)
            )
            """
        )
        n_rdn = maybe_count(where_clauses)
        log(f"9) Remontée de nappes (classefiab sélectionnés)            → {n_rdn} parcelles")
        emit_progress("Après filtre remontée de nappes", n_rdn)
    else:
        n_rdn = maybe_count(where_clauses)
        if not options.remontee_nappes_classefiab:
            log(f"11) Remontée de nappes (aucune classe → neutre)         → {n_rdn} parcelles")
        else:
            log(f"9) Remontée de nappes (table absente → neutre)        → {n_rdn} parcelles")
        emit_progress("Après étape remontée de nappes", n_rdn)
    if funnel_mode and options.remontee_nappes_classefiab and has("ecocompensation_results.remontee_de_nappes"):
        step_idx += 1
        funnel.append({"step": step_idx, "label": "Après filtre remontée de nappes", "count": n_rdn})

    # --- 12) Espaces boisés classés (EBC)
    if options.ebc_mode == "intersect" and has("ecocompensation_results.ebc"):
        where_clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM ecocompensation_results.ebc e
                WHERE e.project_id = CAST(:project_id AS uuid)
                  AND e.geom_2154 IS NOT NULL
                  AND p.geom_2154 && e.geom_2154
                  AND ST_Intersects(p.geom_2154, e.geom_2154)
            )
            """
        )
        n_ebc = maybe_count(where_clauses)
        log(f"10) EBC — doit intersecter                                  → {n_ebc} parcelles")
        emit_progress("Après filtre EBC (intersect)", n_ebc)
    elif options.ebc_mode == "exclude" and has("ecocompensation_results.ebc"):
        where_clauses.append(
            """
            NOT EXISTS (
                SELECT 1
                FROM ecocompensation_results.ebc e
                WHERE e.project_id = CAST(:project_id AS uuid)
                  AND e.geom_2154 IS NOT NULL
                  AND p.geom_2154 && e.geom_2154
                  AND ST_Intersects(p.geom_2154, e.geom_2154)
            )
            """
        )
        n_ebc = maybe_count(where_clauses)
        log(f"10) EBC — ne doit pas intersecter                           → {n_ebc} parcelles")
        emit_progress("Après filtre EBC (exclude)", n_ebc)
    else:
        n_ebc = maybe_count(where_clauses)
        if options.ebc_mode == "ignore":
            log(f"12) EBC (ignoré)                                         → {n_ebc} parcelles")
        else:
            log(f"10) EBC (table absente → neutre)                         → {n_ebc} parcelles")
        emit_progress("Après étape EBC", n_ebc)
    ebc_applied = options.ebc_mode in ("intersect", "exclude") and has("ecocompensation_results.ebc")
    if funnel_mode and ebc_applied:
        step_idx += 1
        funnel.append({"step": step_idx, "label": "Après filtre espaces boisés classés (EBC)", "count": n_ebc})

    # --- 13) Réserves naturelles
    if options.reserves_naturelles_mode == "intersect" and has("ecocompensation_results.reserves_naturelles"):
        where_clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM ecocompensation_results.reserves_naturelles r
                WHERE r.project_id = CAST(:project_id AS uuid)
                  AND r.geom_2154 IS NOT NULL
                  AND p.geom_2154 && r.geom_2154
                  AND ST_Intersects(p.geom_2154, r.geom_2154)
            )
            """
        )
        n_rn = maybe_count(where_clauses)
        log(f"11) Réserves naturelles — doit intersecter                  → {n_rn} parcelles")
        emit_progress("Après filtre réserves naturelles (intersect)", n_rn)
    elif options.reserves_naturelles_mode == "exclude" and has("ecocompensation_results.reserves_naturelles"):
        where_clauses.append(
            """
            NOT EXISTS (
                SELECT 1
                FROM ecocompensation_results.reserves_naturelles r
                WHERE r.project_id = CAST(:project_id AS uuid)
                  AND r.geom_2154 IS NOT NULL
                  AND p.geom_2154 && r.geom_2154
                  AND ST_Intersects(p.geom_2154, r.geom_2154)
            )
            """
        )
        n_rn = maybe_count(where_clauses)
        log(f"11) Réserves naturelles — ne doit pas intersecter           → {n_rn} parcelles")
        emit_progress("Après filtre réserves naturelles (exclude)", n_rn)
    else:
        n_rn = maybe_count(where_clauses)
        if options.reserves_naturelles_mode == "ignore":
            log(f"13) Réserves naturelles (ignoré)                          → {n_rn} parcelles")
        else:
            log(f"11) Réserves naturelles (table absente → neutre)         → {n_rn} parcelles")
        emit_progress("Après étape réserves naturelles", n_rn)
    rn_applied = options.reserves_naturelles_mode in ("intersect", "exclude") and has("ecocompensation_results.reserves_naturelles")
    if funnel_mode and rn_applied:
        step_idx += 1
        funnel.append({"step": step_idx, "label": "Après filtre réserves naturelles", "count": n_rn})

    # --- 14) ZNIEFF (types 1 / 2 — toute la couche)
    if options.znieff_mode == "intersect" and has("ecocompensation_results.znieff"):
        where_clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM ecocompensation_results.znieff z
                WHERE z.project_id = CAST(:project_id AS uuid)
                  AND z.geom_2154 IS NOT NULL
                  AND p.geom_2154 && z.geom_2154
                  AND ST_Intersects(p.geom_2154, z.geom_2154)
            )
            """
        )
        n_znieff = maybe_count(where_clauses)
        log(f"12) ZNIEFF — doit intersecter                                  → {n_znieff} parcelles")
        emit_progress("Après filtre ZNIEFF (intersect)", n_znieff)
    elif options.znieff_mode == "exclude" and has("ecocompensation_results.znieff"):
        where_clauses.append(
            """
            NOT EXISTS (
                SELECT 1
                FROM ecocompensation_results.znieff z
                WHERE z.project_id = CAST(:project_id AS uuid)
                  AND z.geom_2154 IS NOT NULL
                  AND p.geom_2154 && z.geom_2154
                  AND ST_Intersects(p.geom_2154, z.geom_2154)
            )
            """
        )
        n_znieff = maybe_count(where_clauses)
        log(f"12) ZNIEFF — ne doit pas intersecter                           → {n_znieff} parcelles")
        emit_progress("Après filtre ZNIEFF (exclude)", n_znieff)
    else:
        n_znieff = maybe_count(where_clauses)
        if options.znieff_mode == "ignore":
            log(f"14) ZNIEFF (ignoré)                                         → {n_znieff} parcelles")
        else:
            log(f"12) ZNIEFF (table absente → neutre)                         → {n_znieff} parcelles")
        emit_progress("Après étape ZNIEFF", n_znieff)
    znieff_applied = options.znieff_mode in ("intersect", "exclude") and has("ecocompensation_results.znieff")
    if funnel_mode and znieff_applied:
        step_idx += 1
        funnel.append({"step": step_idx, "label": "Après filtre ZNIEFF", "count": n_znieff})

    # --- 15) Tronçons hydro : intersect ou within_radius
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
        n_troncon = maybe_count(where_clauses)
        label = (
            "13) Tronçon hydro intersecte la parcelle"
            if options.troncon_hydro_mode == "intersect"
            else f"13) Tronçon hydro à ≤ {options.troncon_hydro_radius_m:.0f} m"
        )
        log(f"{label:<60} → {n_troncon} parcelles")
        emit_progress("Après filtre tronçon hydro", n_troncon)
    else:
        n_troncon = maybe_count(where_clauses)
        log(f"13) Tronçon hydro (ignoré ou table absente)                → {n_troncon} parcelles")
        emit_progress("Après étape tronçon hydro", n_troncon)
    troncon_applied = options.troncon_hydro_mode != "none" and has("ecocompensation_results.troncons_hydro")
    if funnel_mode and troncon_applied:
        step_idx += 1
        funnel.append({"step": step_idx, "label": "Après filtre tronçon hydro", "count": n_troncon})

    # --- 16) Surfaces hydro : intersect ou within_radius
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
        n_surface_hydro = maybe_count(where_clauses)
        label = (
            "14) Surface hydro intersecte la parcelle"
            if options.surface_hydro_mode == "intersect"
            else f"14) Surface hydro à ≤ {options.surface_hydro_radius_m:.0f} m"
        )
        log(f"{label:<60} → {n_surface_hydro} parcelles")
        emit_progress("Après filtre surface hydro", n_surface_hydro)
    else:
        n_surface_hydro = maybe_count(where_clauses)
        log(f"14) Surface hydro (ignorée ou table absente)                → {n_surface_hydro} parcelles")
        emit_progress("Après étape surface hydro", n_surface_hydro)
    surface_applied = options.surface_hydro_mode != "none" and has("ecocompensation_results.surfaces_hydro")
    if funnel_mode and surface_applied:
        step_idx += 1
        funnel.append({"step": step_idx, "label": "Après filtre surface hydro", "count": n_surface_hydro})

    # --- 17) Faune (par espèce) : intersect ou within_radius
    fauna_table_ok = has("ecocompensation_results.fauna")
    if options.faune_criteria and fauna_table_ok:
        for i, crit in enumerate(options.faune_criteria):
            tax = str(crit.get("tax_nom_val", "")).strip()
            mode = str(crit.get("mode", "intersect")).strip()
            radius = float(crit.get("radius_m", 500.0) or 0.0)
            selected_sources = crit.get("sources", ["pct", "lin", "surf"])
            selected_sources = [s for s in selected_sources if s in ("pct", "lin", "surf")]
            if not selected_sources:
                selected_sources = ["pct", "lin", "surf"]
            if not tax or mode not in ("intersect", "within_radius"):
                continue

            params[f"faune_tax_{i}"] = tax
            params[f"faune_radius_{i}"] = radius

            # Mapping "sources" -> type de géométrie (approx. via geom_type).
            src_clauses: list[str] = []
            if "pct" in selected_sources:
                src_clauses.append("(f.geom_type ILIKE '%POINT%' OR ST_GeometryType(f.geom_2154) ILIKE '%POINT%')")
            if "lin" in selected_sources:
                src_clauses.append("(f.geom_type ILIKE '%LINE%' OR ST_GeometryType(f.geom_2154) ILIKE '%LINE%')")
            if "surf" in selected_sources:
                src_clauses.append("(f.geom_type ILIKE '%POLYGON%' OR ST_GeometryType(f.geom_2154) ILIKE '%POLYGON%')")
            geom_sources_sql = f"({' OR '.join(src_clauses)})" if src_clauses else "TRUE"

            if mode == "intersect":
                where_clauses.append(
                    f"""
                    EXISTS (
                        SELECT 1
                        FROM ecocompensation_results.fauna f
                        WHERE f.project_id = :project_id
                          AND lower(btrim(f.nom_vernaculaire::text)) = lower(btrim(CAST(:faune_tax_{i} AS text)))
                          AND {geom_sources_sql}
                          AND ST_Intersects(p.geom_2154, f.geom_2154)
                    )
                    """
                )
            else:
                where_clauses.append(
                    f"""
                    EXISTS (
                        SELECT 1
                        FROM ecocompensation_results.fauna f
                        WHERE f.project_id = :project_id
                          AND lower(btrim(f.nom_vernaculaire::text)) = lower(btrim(CAST(:faune_tax_{i} AS text)))
                          AND {geom_sources_sql}
                          AND ST_DWithin(p.geom_2154, f.geom_2154, :faune_radius_{i})
                    )
                    """
                )

        n_faune = maybe_count(where_clauses)
        log(f"15) Faune (espèces sélectionnées)                        → {n_faune} parcelles")
        emit_progress("Après filtre faune", n_faune)
    else:
        n_faune = maybe_count(where_clauses)
        if options.faune_criteria and not fauna_table_ok:
            log(f"15) Faune (ignorée — table absente)                      → {n_faune} parcelles")
        else:
            log(f"15) Faune (ignorée)                                      → {n_faune} parcelles")
        emit_progress("Après étape faune", n_faune)
    faune_applied = bool(options.faune_criteria) and fauna_table_ok
    if funnel_mode and faune_applied:
        step_idx += 1
        funnel.append({"step": step_idx, "label": "Après filtre faune", "count": n_faune})

    # --- 18) Candidats finaux (dans l'AOI)
    # Le rayon dynamique est supprimé : l'AOI (buffer) est définie en amont via ecocompensation.aoi.
    final_radius_km = 0.0
    final_count = count_with_clauses(where_clauses)
    if funnel_mode:
        step_idx += 1
        funnel.append({"step": step_idx, "label": "Candidats finaux (dans l'AOI)", "count": final_count})
    log(f"\n🎯 Vrai filtre terminé : {final_count} parcelles dans l'AOI.")
    emit_progress("Candidats finaux (dans l'AOI)", final_count)

    final_where_sql = " AND ".join(f"({c})" for c in where_clauses)

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


def run_filter_and_score(
    engine,
    project_id: str,
    aoi_id: str,
    cx: float,
    cy: float,
    options: "FiltreOptions",
    opts_dto,
    progress_callback: Callable[[str, int], None] | None = None,
) -> dict:
    """
    Exécute run() puis construit le pool classé (stratégie faune-first si critères faune
    exploitables, sinon tri distance au centre puis surface).

    :param opts_dto: FiltreOptionsDTO (conserve target_count ; ≤ 0 = toutes les survivantes).
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
        funnel_mode=(getattr(opts_dto, "funnel_mode", False) or getattr(opts_dto, "log_progress", False)),
        miller_threshold=opts_dto.miller_threshold,
        min_area_ha=opts_dto.min_area_ha,
        radius_start_km=opts_dto.radius_start_km,
        radius_min_km=opts_dto.radius_min_km,
        target_count=opts_dto.target_count,
        progress_callback=progress_callback,
    )

    if result is None:
        return {"parcelles": [], "final_radius_km": 0, "total": 0, "funnel": []}

    parcelles_raw, final_radius_km, funnel = result

    from pool.pool_builder import build_faune_first_pool

    limit = int(getattr(opts_dto, "target_count", 50))
    ranked, pool_meta = build_faune_first_pool(
        engine,
        project_id,
        aoi_id,
        cx,
        cy,
        parcelles_raw,
        options,
        limit,
    )

    for i, p in enumerate(ranked, 1):
        p["rank"] = i

    return {
        "total": len(ranked),
        "final_radius_km": final_radius_km,
        "parcelles": ranked,
        "funnel": funnel,
        "pool_build": pool_meta,
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
    print(f"\n🌿 Vegetation hybride       : {options.vegetation_hybride}")
    print(f"🫧 Tronçon hydro (défaut)   : {options.troncon_hydro_mode}")
    print(f"💧 Surface hydro (défaut)   : {options.surface_hydro_mode} (rayon {options.surface_hydro_radius_m:.0f} m)")
    print(f"⭕ Miller                   : 0.39  |  📐 Superficie min : 7 ha")
    print(f"🎯 Cible sortie             : ≤ {TARGET_COUNT} parcelles\n")

    print("=== Vrai filtre (ZDV + hydro + Miller + area + distance) ===\n")
    run(engine, project_id, aoi_id, cx, cy, options)


if __name__ == "__main__":
    main()
