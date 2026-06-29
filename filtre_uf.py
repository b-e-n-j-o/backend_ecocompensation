#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
filtre_uf.py
============

Filtre les sous-ensembles d'unités foncières stockés dans
ecocompensation_results.sous_ensembles, en appliquant les mêmes critères
que vrai_filtre.py sur les parcelles seules.

Pré-requis :
  - aoi_to_unites_foncieres.py  doit avoir tourné
  - aoi_to_sous_ensembles.py    doit avoir tourné

Les colonnes de sous_ensembles utilisées :
  - geom_2154      : géométrie union précalculée
  - surface_ha     : surface union en ha
  - miller         : coefficient de Miller
  - dist_centre_m  : distance au centre AOI
  - subset_id      : identifiant unique
  - uf_id          : identifiant de l'UF parente
  - idus           : liste des IDU membres
  - k              : nombre de parcelles du sous-ensemble

Filtres :
  0. Bruts
  1. Exclusion intersection zone projet (foncier / GPKG initial)
  2. Exclusion GEOMCE
  3. Natura 2000 (intersect / exclure / ignorer)
  4. Surface ≥ min_area_ha (pré-filtré à la création)
  5. Miller ≥ miller_threshold
  6. ZDV intersecte
  7. CESBIO intersecte
  8. Carhab (nom_eunis), 9. arrachage vignes, 10. zones humides, 11. remontée de nappes (classefiab),
  12. EBC, 13. réserves naturelles, 14. ZNIEFF, 15–16. Tronçon / surface hydro, 17. Faune, 18. distance (info)
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import text

from db import get_engine
from vrai_filtre import FiltreOptions
from layers.national_exclusions import national_exclusion_steps

# ─────────────────────────────────────────────
# Paramètres
# ─────────────────────────────────────────────

PROJECT_ID = "54987c59-ad94-46b2-9f20-aa679dbcf3a1"

TARGET_COUNT    = 50
RADIUS_START_KM = 10.0
RADIUS_MIN_KM   = 1.0


# ─────────────────────────────────────────────
# run()
# ─────────────────────────────────────────────

def run(
    engine,
    project_id: str,
    aoi_id: str,
    cx: float,
    cy: float,
    options: FiltreOptions,
    *,
    return_results: bool = False,
    funnel_mode: bool = False,
    miller_threshold: float = 0.39,
    min_area_ha: float = 7.0,
    target_count: int = TARGET_COUNT,
    radius_start_km: float = RADIUS_START_KM,
    radius_min_km: float = RADIUS_MIN_KM,
):
    funnel: list[dict] = []

    tables_to_check = [
        "ecocompensation_results.sous_ensembles",
        "ecocompensation_results.mesures_compensatoire_surf",
        "ecocompensation_results.mesures_compensatoire_lin",
        "ecocompensation_results.mesures_compensatoire_pct",
        "ecocompensation_results.mesures_compensatoire_commune",
        "ecocompensation_results.natura2000",
        "ecocompensation_results.ebc",
        "ecocompensation_results.reserves_naturelles",
        "ecocompensation_results.znieff",
        "ecocompensation_results.bd_topo_et_cesbio",
        "ecocompensation_results.carhab",
        "ecocompensation_results.arrachage_vignes",
        "ecocompensation_results.zone_humide",
        "ecocompensation_results.remontee_de_nappes",
        "ecocompensation_results.troncons_hydro",
        "ecocompensation_results.surfaces_hydro",
        "ecocompensation_results.fauna",
    ]
    exists: dict[str, bool] = {}
    with engine.begin() as conn:
        for t in tables_to_check:
            exists[t] = conn.execute(
                text("SELECT to_regclass(:r) IS NOT NULL").execution_options(no_prepare=True),
                {"r": t},
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

    if not has("ecocompensation_results.sous_ensembles"):
        print("❌ Table sous_ensembles introuvable. Lance d'abord aoi_to_sous_ensembles.py.")
        return None if not return_results else ([], 0.0, funnel)

    where_clauses = ["s.project_id = :project_id"]
    params = {
        "project_id":             project_id,
        "aoi_id_str":             aoi_id,
        "cx":                     cx,
        "cy":                     cy,
        "radius_m":               0.0,
        "min_area_ha":            min_area_ha,
        "miller_th":              miller_threshold,
        "zdv_natures":            options.zdv_natures,
        "cesbio_libelles":        options.cesbio_libelles,
        "carhab_nom_eunis":       options.carhab_nom_eunis,
        "remontee_nappes_classefiab": options.remontee_nappes_classefiab,
        "troncon_hydro_radius_m": options.troncon_hydro_radius_m,
        "surface_hydro_radius_m": options.surface_hydro_radius_m,
    }

    def log(msg: str) -> None:
        if not return_results:
            print(msg)

    def count_with_clauses(clauses: list[str], extra_params: dict | None = None) -> int:
        where_sql = " AND ".join(f"({c})" for c in clauses)
        p = {**params, **(extra_params or {})}
        with engine.begin() as conn:
            return conn.execute(
                text(f"SELECT COUNT(*) FROM ecocompensation_results.sous_ensembles s WHERE {where_sql}"),
                p,
            ).scalar_one()

    def maybe_count(clauses: list[str], extra_params: dict | None = None) -> int:
        if not funnel_mode:
            return -1
        return count_with_clauses(clauses, extra_params)

    # 0) Bruts
    n0 = count_with_clauses(where_clauses)
    log(f"0) Sous-ensembles bruts                             → {n0}")
    step_idx = 0
    if funnel_mode:
        funnel.append({"step": step_idx, "label": "Sous-ensembles bruts", "count": n0})
    if n0 == 0:
        log("⚠️ Aucun sous-ensemble. Lance aoi_to_sous_ensembles.py.")
        return None if not return_results else ([], 0.0, funnel)

    # 1) Miller (numérique)
    miller_clause = "s.miller >= :miller_th"
    where_clauses.append(miller_clause)
    n1 = maybe_count(where_clauses)
    log(f"1) Miller ≥ {miller_threshold}                             → {n1}")
    if funnel_mode:
        step_idx += 1
        funnel.append({"step": step_idx, "label": f"Après Miller ≥ {miller_threshold}", "count": n1})
    if funnel_mode and n1 == 0:
        log("⚠️ Aucun sous-ensemble après Miller, arrêt.")
        return None if not return_results else ([], 0.0, funnel)

    # 2) Exclusions nationales (GEOMCE, préemption ENS, ENS)
    excluded_layers = {
        str(x).strip()
        for x in getattr(options, "excluded_layers", []) or []
        if str(x).strip()
    }
    geomce_applied = False
    for _label, clause in national_exclusion_steps(excluded_layers, geom_alias="s"):
        where_clauses.append(clause)
        geomce_applied = True
    n2 = maybe_count(where_clauses)
    log(f"2) Exclusions nationales (GEOMCE / préemption ENS / ENS)  → {n2}")
    if funnel_mode and geomce_applied:
        step_idx += 1
        funnel.append({"step": step_idx, "label": "Après exclusions nationales", "count": n2})

    # 2) Natura 2000 (SIC / ZPS) : intersect / exclure / ignorer
    if options.natura2000_mode == "intersect" and has("ecocompensation_results.natura2000"):
        where_clauses.append("""
            EXISTS (
                SELECT 1 FROM ecocompensation_results.natura2000 n2
                WHERE n2.project_id = CAST(:project_id AS uuid)
                  AND s.geom_2154 && n2.geom_2154
                  AND ST_Intersects(s.geom_2154, n2.geom_2154)
            )
        """)
    elif options.natura2000_mode == "exclude":
        where_clauses.append("""
            NOT EXISTS (
                SELECT 1 FROM ecocompensation.natura_2000 n2
                WHERE n2.geom_2154 IS NOT NULL
                  AND s.geom_2154 && n2.geom_2154
                  AND ST_Intersects(s.geom_2154, n2.geom_2154)
            )
        """)
    n3 = maybe_count(where_clauses)
    _natura_lbl = (
        "intersection"
        if options.natura2000_mode == "intersect"
        else ("exclusion" if options.natura2000_mode == "exclude" else "ignoré")
    )
    log(f"3) Natura 2000 ({_natura_lbl})                            → {n3}")
    natura_applied = options.natura2000_mode == "exclude" or (
        options.natura2000_mode == "intersect" and has("ecocompensation_results.natura2000")
    )
    if funnel_mode and natura_applied:
        step_idx += 1
        funnel.append({"step": step_idx, "label": f"Après filtre Natura 2000 ({_natura_lbl})", "count": n3})

    # 3) Surface — informatif (déjà pré-filtré à la création)
    n3 = count_with_clauses(where_clauses)
    log(f"4) Surface ≥ {min_area_ha} ha (pré-filtré)                → {n3} (informatif)")
    if funnel_mode:
        step_idx += 1
        funnel.append({"step": step_idx, "label": f"Surface ≥ {min_area_ha} ha (pré-filtré)", "count": n3})

    # 5) Miller : déjà appliqué en étape 1
    n4 = maybe_count(where_clauses)
    log(f"5) Miller (déjà appliqué)                                 → {n4}")
    if funnel_mode:
        step_idx += 1
        funnel.append({"step": step_idx, "label": f"Après Miller ≥ {miller_threshold}", "count": n4})

    # 6) Vegetation hybride (BD TOPO + CESBIO)
    has_hybrid = has("ecocompensation_results.bd_topo_et_cesbio")
    has_zdv = bool(options.zdv_natures)
    has_cesbio = bool(options.cesbio_libelles)
    mode = options.vegetation_hybride_mode
    if has_hybrid and (has_zdv or has_cesbio):
        if mode == "AND" and has_zdv and has_cesbio:
            where_clauses.append("""
                EXISTS (
                    SELECT 1 FROM ecocompensation_results.bd_topo_et_cesbio v
                    WHERE v.project_id = CAST(:project_id AS uuid)
                      AND v.nature = ANY(:zdv_natures)
                      AND s.geom_2154 && v.geom_2154
                      AND ST_Intersects(s.geom_2154, v.geom_2154)
                )
            """)
            where_clauses.append("""
                EXISTS (
                    SELECT 1 FROM ecocompensation_results.bd_topo_et_cesbio v
                    WHERE v.project_id = CAST(:project_id AS uuid)
                      AND v.libelle = ANY(:cesbio_libelles)
                      AND s.geom_2154 && v.geom_2154
                      AND ST_Intersects(s.geom_2154, v.geom_2154)
                )
            """)
        else:
            where_clauses.append("""
                EXISTS (
                    SELECT 1 FROM ecocompensation_results.bd_topo_et_cesbio v
                    WHERE v.project_id = CAST(:project_id AS uuid)
                      AND s.geom_2154 && v.geom_2154
                      AND ST_Intersects(s.geom_2154, v.geom_2154)
                      AND (
                          v.nature = ANY(:zdv_natures)
                          OR v.libelle = ANY(:cesbio_libelles)
                      )
                )
            """)
        n6 = maybe_count(where_clauses)
        log(f"6) Vegetation hybride ({mode})                        → {n6}")
    else:
        n6 = maybe_count(where_clauses)
        log(f"6) Vegetation hybride (ignoree ou table absente)      → {n6}")
    if funnel_mode and has_hybrid and (has_zdv or has_cesbio):
        step_idx += 1
        funnel.append({"step": step_idx, "label": "Après filtre vegetation hybride", "count": n6})

    # 8) Carhab (nom_eunis)
    if options.carhab_nom_eunis and has("ecocompensation_results.carhab"):
        where_clauses.append("""
            EXISTS (
                SELECT 1 FROM ecocompensation_results.carhab ch
                WHERE (ch.project_id = CAST(:project_id AS uuid) OR ch.aoi_id = CAST(:aoi_id_str AS uuid))
                  AND s.geom_2154 && ch.geom_2154
                  AND ST_Intersects(s.geom_2154, ch.geom_2154)
                  AND ch.nom_eunis = ANY(:carhab_nom_eunis)
            )
        """)
        n_carhab = maybe_count(where_clauses)
        log(f"8) Carhab intersecte (nom_eunis sélectionnés)              → {n_carhab}")
    else:
        n_carhab = maybe_count(where_clauses)
        if not options.carhab_nom_eunis:
            log(f"8) Carhab (aucun libellé demandé → étape neutre)       → {n_carhab}")
        else:
            log(f"8) Carhab (table absente → étape neutre)                → {n_carhab}")
    if funnel_mode and options.carhab_nom_eunis and has("ecocompensation_results.carhab"):
        step_idx += 1
        funnel.append({"step": step_idx, "label": "Après filtre Carhab", "count": n_carhab})

    # 9) Arrachage de vignes
    if options.arrachage_vignes_mode == "intersect" and has("ecocompensation_results.arrachage_vignes"):
        where_clauses.append("""
            EXISTS (
                SELECT 1 FROM ecocompensation_results.arrachage_vignes av
                WHERE av.project_id = CAST(:project_id AS uuid)
                  AND av.geom_2154 IS NOT NULL
                  AND s.geom_2154 && av.geom_2154
                  AND ST_Intersects(s.geom_2154, av.geom_2154)
            )
        """)
        n_av = maybe_count(where_clauses)
        log(f"9) Arrachage vignes — doit intersecter                  → {n_av}")
    elif options.arrachage_vignes_mode == "exclude" and has("ecocompensation_results.arrachage_vignes"):
        where_clauses.append("""
            NOT EXISTS (
                SELECT 1 FROM ecocompensation_results.arrachage_vignes av
                WHERE av.project_id = CAST(:project_id AS uuid)
                  AND av.geom_2154 IS NOT NULL
                  AND s.geom_2154 && av.geom_2154
                  AND ST_Intersects(s.geom_2154, av.geom_2154)
            )
        """)
        n_av = maybe_count(where_clauses)
        log(f"9) Arrachage vignes — ne doit pas intersecter           → {n_av}")
    else:
        n_av = maybe_count(where_clauses)
        log(f"9) Arrachage vignes (ignoré ou table absente)           → {n_av}")
    av_applied = options.arrachage_vignes_mode in ("intersect", "exclude") and has("ecocompensation_results.arrachage_vignes")
    if funnel_mode and av_applied:
        step_idx += 1
        funnel.append({"step": step_idx, "label": "Après filtre arrachage vignes", "count": n_av})

    # 10) Zones humides
    if options.zone_humide_mode == "intersect" and has("ecocompensation_results.zone_humide"):
        where_clauses.append("""
            EXISTS (
                SELECT 1 FROM ecocompensation_results.zone_humide zh
                WHERE zh.project_id = CAST(:project_id AS uuid)
                  AND zh.geom_2154 IS NOT NULL
                  AND s.geom_2154 && zh.geom_2154
                  AND ST_Intersects(s.geom_2154, zh.geom_2154)
            )
        """)
        n_zh = maybe_count(where_clauses)
        log(f"10) Zones humides — doit intersecter                    → {n_zh}")
    elif options.zone_humide_mode == "exclude" and has("ecocompensation_results.zone_humide"):
        where_clauses.append("""
            NOT EXISTS (
                SELECT 1 FROM ecocompensation_results.zone_humide zh
                WHERE zh.project_id = CAST(:project_id AS uuid)
                  AND zh.geom_2154 IS NOT NULL
                  AND s.geom_2154 && zh.geom_2154
                  AND ST_Intersects(s.geom_2154, zh.geom_2154)
            )
        """)
        n_zh = maybe_count(where_clauses)
        log(f"10) Zones humides — ne doit pas intersecter             → {n_zh}")
    else:
        n_zh = maybe_count(where_clauses)
        log(f"10) Zones humides (ignoré ou table absente)             → {n_zh}")
    zh_applied = options.zone_humide_mode in ("intersect", "exclude") and has("ecocompensation_results.zone_humide")
    if funnel_mode and zh_applied:
        step_idx += 1
        funnel.append({"step": step_idx, "label": "Après filtre zones humides", "count": n_zh})

    # 11) Remontée de nappes (CLASSEFIAB)
    if options.remontee_nappes_classefiab and has("ecocompensation_results.remontee_de_nappes"):
        where_clauses.append("""
            EXISTS (
                SELECT 1 FROM ecocompensation_results.remontee_de_nappes rdn
                WHERE rdn.project_id = CAST(:project_id AS uuid)
                  AND rdn.geom_2154 IS NOT NULL
                  AND s.geom_2154 && rdn.geom_2154
                  AND ST_Intersects(s.geom_2154, rdn.geom_2154)
                  AND rdn.classefiab = ANY(:remontee_nappes_classefiab)
            )
        """)
        n_rdn = maybe_count(where_clauses)
        log(f"11) Remontée de nappes (classefiab sélectionnés)        → {n_rdn}")
    else:
        n_rdn = maybe_count(where_clauses)
        if not options.remontee_nappes_classefiab:
            log(f"11) Remontée de nappes (aucune classe → neutre)     → {n_rdn}")
        else:
            log(f"11) Remontée de nappes (table absente → neutre)      → {n_rdn}")
    if funnel_mode and options.remontee_nappes_classefiab and has("ecocompensation_results.remontee_de_nappes"):
        step_idx += 1
        funnel.append({"step": step_idx, "label": "Après filtre remontée de nappes", "count": n_rdn})

    # 12) Espaces boisés classés (EBC)
    if options.ebc_mode == "intersect" and has("ecocompensation_results.ebc"):
        where_clauses.append("""
            EXISTS (
                SELECT 1 FROM ecocompensation_results.ebc e
                WHERE e.project_id = CAST(:project_id AS uuid)
                  AND e.geom_2154 IS NOT NULL
                  AND s.geom_2154 && e.geom_2154
                  AND ST_Intersects(s.geom_2154, e.geom_2154)
            )
        """)
        n_ebc = maybe_count(where_clauses)
        log(f"12) EBC — doit intersecter                             → {n_ebc}")
    elif options.ebc_mode == "exclude" and has("ecocompensation_results.ebc"):
        where_clauses.append("""
            NOT EXISTS (
                SELECT 1 FROM ecocompensation_results.ebc e
                WHERE e.project_id = CAST(:project_id AS uuid)
                  AND e.geom_2154 IS NOT NULL
                  AND s.geom_2154 && e.geom_2154
                  AND ST_Intersects(s.geom_2154, e.geom_2154)
            )
        """)
        n_ebc = maybe_count(where_clauses)
        log(f"12) EBC — ne doit pas intersecter                      → {n_ebc}")
    else:
        n_ebc = maybe_count(where_clauses)
        log(f"12) EBC (ignoré ou table absente)                     → {n_ebc}")
    ebc_applied = options.ebc_mode in ("intersect", "exclude") and has("ecocompensation_results.ebc")
    if funnel_mode and ebc_applied:
        step_idx += 1
        funnel.append({"step": step_idx, "label": "Après filtre espaces boisés classés (EBC)", "count": n_ebc})

    # 13) Réserves naturelles
    if options.reserves_naturelles_mode == "intersect" and has("ecocompensation_results.reserves_naturelles"):
        where_clauses.append("""
            EXISTS (
                SELECT 1 FROM ecocompensation_results.reserves_naturelles r
                WHERE r.project_id = CAST(:project_id AS uuid)
                  AND r.geom_2154 IS NOT NULL
                  AND s.geom_2154 && r.geom_2154
                  AND ST_Intersects(s.geom_2154, r.geom_2154)
            )
        """)
        n_rn = maybe_count(where_clauses)
        log(f"13) Réserves naturelles — doit intersecter            → {n_rn}")
    elif options.reserves_naturelles_mode == "exclude" and has("ecocompensation_results.reserves_naturelles"):
        where_clauses.append("""
            NOT EXISTS (
                SELECT 1 FROM ecocompensation_results.reserves_naturelles r
                WHERE r.project_id = CAST(:project_id AS uuid)
                  AND r.geom_2154 IS NOT NULL
                  AND s.geom_2154 && r.geom_2154
                  AND ST_Intersects(s.geom_2154, r.geom_2154)
            )
        """)
        n_rn = maybe_count(where_clauses)
        log(f"13) Réserves naturelles — ne doit pas intersecter           → {n_rn}")
    else:
        n_rn = maybe_count(where_clauses)
        log(f"13) Réserves naturelles (ignoré ou table absente)           → {n_rn}")
    rn_applied = options.reserves_naturelles_mode in ("intersect", "exclude") and has("ecocompensation_results.reserves_naturelles")
    if funnel_mode and rn_applied:
        step_idx += 1
        funnel.append({"step": step_idx, "label": "Après filtre réserves naturelles", "count": n_rn})

    # 14) ZNIEFF
    if options.znieff_mode == "intersect" and has("ecocompensation_results.znieff"):
        where_clauses.append("""
            EXISTS (
                SELECT 1 FROM ecocompensation_results.znieff z
                WHERE z.project_id = CAST(:project_id AS uuid)
                  AND z.geom_2154 IS NOT NULL
                  AND s.geom_2154 && z.geom_2154
                  AND ST_Intersects(s.geom_2154, z.geom_2154)
            )
        """)
        n_znieff = maybe_count(where_clauses)
        log(f"14) ZNIEFF — doit intersecter                             → {n_znieff}")
    elif options.znieff_mode == "exclude" and has("ecocompensation_results.znieff"):
        where_clauses.append("""
            NOT EXISTS (
                SELECT 1 FROM ecocompensation_results.znieff z
                WHERE z.project_id = CAST(:project_id AS uuid)
                  AND z.geom_2154 IS NOT NULL
                  AND s.geom_2154 && z.geom_2154
                  AND ST_Intersects(s.geom_2154, z.geom_2154)
            )
        """)
        n_znieff = maybe_count(where_clauses)
        log(f"14) ZNIEFF — ne doit pas intersecter                      → {n_znieff}")
    else:
        n_znieff = maybe_count(where_clauses)
        log(f"14) ZNIEFF (ignoré ou table absente)                     → {n_znieff}")
    znieff_applied = options.znieff_mode in ("intersect", "exclude") and has("ecocompensation_results.znieff")
    if funnel_mode and znieff_applied:
        step_idx += 1
        funnel.append({"step": step_idx, "label": "Après filtre ZNIEFF", "count": n_znieff})

    # 15) Tronçon hydro
    if options.troncon_hydro_mode != "none" and has("ecocompensation_results.troncons_hydro"):
        if options.troncon_hydro_mode == "intersect":
            where_clauses.append("""
                EXISTS (
                    SELECT 1 FROM ecocompensation_results.troncons_hydro t
                    WHERE (t.project_id = :project_id OR t.aoi_id = :aoi_id_str)
                      AND s.geom_2154 && t.geom_2154
                      AND ST_Intersects(s.geom_2154, t.geom_2154)
                )
            """)
            label7 = "15) Tronçon hydro intersecte"
        else:
            where_clauses.append("""
                EXISTS (
                    SELECT 1 FROM ecocompensation_results.troncons_hydro t
                    WHERE (t.project_id = :project_id OR t.aoi_id = :aoi_id_str)
                      AND ST_DWithin(s.geom_2154, t.geom_2154, :troncon_hydro_radius_m)
                )
            """)
            label7 = f"15) Tronçon hydro ≤ {options.troncon_hydro_radius_m:.0f} m"
        n7 = maybe_count(where_clauses)
        log(f"{label7:<55} → {n7}")
    else:
        n7 = maybe_count(where_clauses)
        log(f"15) Tronçon hydro (ignoré ou table absente)           → {n7}")
    troncon_applied = options.troncon_hydro_mode != "none" and has("ecocompensation_results.troncons_hydro")
    if funnel_mode and troncon_applied:
        step_idx += 1
        funnel.append({"step": step_idx, "label": "Après filtre tronçon hydro", "count": n7})

    # 16) Surface hydro
    if options.surface_hydro_mode != "none" and has("ecocompensation_results.surfaces_hydro"):
        if options.surface_hydro_mode == "intersect":
            where_clauses.append("""
                EXISTS (
                    SELECT 1 FROM ecocompensation_results.surfaces_hydro sh
                    WHERE (sh.project_id = :project_id OR sh.aoi_id = :aoi_id_str)
                      AND s.geom_2154 && sh.geom_2154
                      AND ST_Intersects(s.geom_2154, sh.geom_2154)
                )
            """)
            label8 = "16) Surface hydro intersecte"
        else:
            where_clauses.append("""
                EXISTS (
                    SELECT 1 FROM ecocompensation_results.surfaces_hydro sh
                    WHERE (sh.project_id = :project_id OR sh.aoi_id = :aoi_id_str)
                      AND ST_DWithin(s.geom_2154, sh.geom_2154, :surface_hydro_radius_m)
                )
            """)
            label8 = f"16) Surface hydro ≤ {options.surface_hydro_radius_m:.0f} m"
        n8 = maybe_count(where_clauses)
        log(f"{label8:<55} → {n8}")
    else:
        n8 = maybe_count(where_clauses)
        log(f"16) Surface hydro (ignorée ou table absente)          → {n8}")
    surface_applied = options.surface_hydro_mode != "none" and has("ecocompensation_results.surfaces_hydro")
    if funnel_mode and surface_applied:
        step_idx += 1
        funnel.append({"step": step_idx, "label": "Après filtre surface hydro", "count": n8})

    # 17) Faune (par espèce) : intersect ou within_radius
    if options.faune_criteria and has("ecocompensation_results.fauna"):
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
                          AND ST_Intersects(s.geom_2154, f.geom_2154)
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
                          AND ST_DWithin(s.geom_2154, f.geom_2154, :faune_radius_{i})
                    )
                    """
                )

        n9 = maybe_count(where_clauses)
        log(f"17) Faune (espèces sélectionnées)                    → {n9}")
    else:
        n9 = maybe_count(where_clauses)
        if options.faune_criteria and not has("ecocompensation_results.fauna"):
            log(f"17) Faune (ignorée — table absente)               → {n9}")
        else:
            log(f"17) Faune (ignorée)                                  → {n9}")
    faune_applied = bool(options.faune_criteria) and has("ecocompensation_results.fauna")
    if funnel_mode and faune_applied:
        step_idx += 1
        funnel.append({"step": step_idx, "label": "Après filtre faune", "count": n9})

    # 18) Distance — informatif uniquement
    n10 = count_with_clauses(where_clauses)
    log(f"18) Distance (informative)                           → {n10} candidats retenus")
    if funnel_mode:
        step_idx += 1
        funnel.append({"step": step_idx, "label": "Candidats retenus", "count": n10})
    final_radius_km = 0.0
    log(f"\n🎯 Filtre UF terminé : {n10} sous-ensembles candidats.")

    if not return_results:
        return None

    # Récupération résultats
    spatial_filter_sql = " AND ".join(f"({c})" for c in where_clauses[1:])
    if not spatial_filter_sql.strip():
        spatial_filter_sql = "TRUE"

    if has("ecocompensation_results.surfaces_hydro"):
        dist_hydro_sub = """
            (SELECT MIN(ST_Distance(sh.geom_2154, s.geom_2154))
             FROM ecocompensation_results.surfaces_hydro sh
             WHERE sh.project_id = :project_id
               AND ST_DWithin(s.geom_2154, sh.geom_2154, :surface_hydro_radius_m)
            ) AS dist_surface_hydro_m
        """
    else:
        dist_hydro_sub = "NULL::double precision AS dist_surface_hydro_m"

    sql_final = f"""
        SELECT
            s.subset_id,
            s.uf_id,
            s.idus,
            s.k,
            s.surface_ha,
            s.miller,
            s.dist_centre_m AS distance_centre_m,
            s.denomination,
            s.siren,
            {dist_hydro_sub}
        FROM ecocompensation_results.sous_ensembles s
        WHERE s.project_id = :project_id
          AND {spatial_filter_sql};
    """
    with engine.begin() as conn:
        rows = conn.execute(text(sql_final), params).mappings().all()

    resultats = [dict(r) for r in rows]
    return resultats, final_radius_km, funnel


def run_filter_uf_and_score(
    engine,
    project_id: str,
    aoi_id: str,
    cx: float,
    cy: float,
    options: "FiltreOptions",
    opts_dto,
) -> dict:
    """
    Enchaîne run() (filtre UF) + classement groupé par UF.
    Retourne le dict complet attendu par la route POST /filter/uf.

    :param opts_dto: FiltreOptionsDTO — target_count utilisé comme limite.
    :return: {total_uf, total_sous_ensembles, unites_foncieres, funnel}
    """
    result = run(
        engine,
        project_id,
        aoi_id,
        cx,
        cy,
        options,
        return_results=True,
        funnel_mode=getattr(opts_dto, "funnel_mode", False),
        miller_threshold=opts_dto.miller_threshold,
        min_area_ha=opts_dto.min_area_ha,
    )

    if result is None:
        return {"unites_foncieres": [], "total_uf": 0, "total_sous_ensembles": 0, "funnel": []}

    resultats, final_radius_km, funnel = result

    if not resultats:
        return {"unites_foncieres": [], "total_uf": 0, "total_sous_ensembles": 0, "funnel": funnel}

    # Groupement par UF + classement simple (distance, puis surface)
    by_uf: dict[str, list[dict]] = defaultdict(list)
    for r in resultats:
        by_uf[r["uf_id"]].append(
            {
                "subset_id": r["subset_id"],
                "k": r["k"],
                "idus": r["idus"],
                "surface_ha": round(float(r["surface_ha"] or 0), 2),
                "miller": round(float(r["miller"] or 0), 4),
                "distance_centre_km": round(float(r["distance_centre_m"] or 0) / 1000, 3),
                "dist_hydro_m": float(r["dist_surface_hydro_m"])
                if r.get("dist_surface_hydro_m") is not None
                else None,
                "denomination": r.get("denomination"),
                "siren": r.get("siren"),
            }
        )

    # Trier sous-ensembles de chaque UF : distance asc, surface desc
    for uf_id in by_uf:
        by_uf[uf_id].sort(key=lambda x: (x["distance_centre_km"], -x["surface_ha"]))

    # Trier UF par meilleur sous-ensemble (mêmes critères)
    uf_sorted = sorted(by_uf.items(), key=lambda kv: (kv[1][0]["distance_centre_km"], -kv[1][0]["surface_ha"]))

    unites_foncieres = []
    for rang, (uf_id, subsets) in enumerate(uf_sorted, 1):
        all_idus = sorted({idu for s in subsets for idu in s["idus"]})
        best = subsets[0]
        unites_foncieres.append(
            {
                "rang": rang,
                "uf_id": uf_id,
                "nb_parcelles": len(all_idus),
                "idus": all_idus,
                "best_surface_ha": best["surface_ha"],
                "best_miller": best["miller"],
                "distance_centre_km": best["distance_centre_km"],
                "denomination": best.get("denomination"),
                "siren": best.get("siren"),
                "sous_ensembles": subsets,
            }
        )

    # Les X premières UF classées (distance, puis surface).
    # target_count ≤ 0 = pas de limite.
    limit = opts_dto.target_count
    if limit > 0:
        unites_foncieres = unites_foncieres[:limit]
        for r, uf in enumerate(unites_foncieres, 1):
            uf["rang"] = r

    total_ss = sum(len(uf["sous_ensembles"]) for uf in unites_foncieres)

    return {
        "total_uf": len(unites_foncieres),
        "total_sous_ensembles": total_ss,
        "unites_foncieres": unites_foncieres,
        "funnel": funnel,
    }


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    load_dotenv(Path(__file__).parent / ".env")
    engine = get_engine()

    with engine.begin() as conn:
        row = conn.execute(text("""
            SELECT ST_X(ST_Centroid(a.geom_2154)) AS cx,
                   ST_Y(ST_Centroid(a.geom_2154)) AS cy,
                   p.aoi_id
            FROM ecocompensation.projects p
            JOIN ecocompensation.aoi a ON a.id = p.aoi_id
            WHERE p.id = :pid
        """), {"pid": PROJECT_ID}).one_or_none()

    if not row:
        print("❌ Projet introuvable.")
        return

    cx, cy = float(row[0]), float(row[1])
    aoi_id = str(row[2])
    options = FiltreOptions.defaut()

    print(f"🔗 Projet : {PROJECT_ID}")
    print(f"   Centre AOI : x={cx:.0f}, y={cy:.0f}")
    print(f"   Vegetation hybride : {options.vegetation_hybride}")
    print(f"   Tronçon    : {options.troncon_hydro_mode}")
    print(f"   Surface    : {options.surface_hydro_mode} ({options.surface_hydro_radius_m:.0f} m)")
    print(f"   Miller ≥   : 0.39  |  Surface ≥ : 7 ha\n")

    # Diagnostic
    with engine.begin() as conn:
        zdv_rows = conn.execute(text("""
            SELECT DISTINCT nature, COUNT(*) AS n
            FROM ecocompensation_results.zone_de_vegetation
            WHERE project_id = :project_id
            GROUP BY nature ORDER BY n DESC
        """), {"project_id": PROJECT_ID}).all()
        n_troncons = conn.execute(text("""
            SELECT COUNT(*) FROM ecocompensation_results.troncons_hydro
            WHERE project_id = :project_id
        """), {"project_id": PROJECT_ID}).scalar_one()
        n_ss = conn.execute(text("""
            SELECT COUNT(*) FROM ecocompensation_results.sous_ensembles
            WHERE project_id = :project_id
        """), {"project_id": PROJECT_ID}).scalar_one()

    print("🌲 Natures ZDV en base :")
    for nat, n in zdv_rows:
        marker = " ✓ (filtre actif)" if nat in options.zdv_natures else ""
        print(f"   '{nat}' ({n} entités){marker}")
    if not zdv_rows:
        print("   ⚠️  Aucune ZDV en base pour ce projet.")
    print(f"\n💧 Tronçons hydro en base : {n_troncons}")
    print(f"📦 Sous-ensembles en base : {n_ss}\n")
    print("=== Filtre unités foncières ===\n")

    result = run(engine, PROJECT_ID, aoi_id, cx, cy, options, return_results=True)
    if result is None:
        return

    resultats, final_radius_km, funnel = result
    if not resultats:
        print("Aucun sous-ensemble candidat.")
        return

    # ── Groupement par UF ────────────────────────────────────────────────
    by_uf: dict[str, list[dict]] = defaultdict(list)
    for r in resultats:
        by_uf[r["uf_id"]].append(r)

    # Trier les sous-ensembles de chaque UF : distance asc, surface desc
    for uf_id in by_uf:
        by_uf[uf_id].sort(
            key=lambda r: (
                float((r.get("distance_centre_m") or 0.0)),
                -float((r.get("surface_ha") or 0.0)),
            )
        )

    # Trier les UF par meilleur sous-ensemble (mêmes critères)
    uf_sorted = sorted(
        by_uf.items(),
        key=lambda kv: (
            float((kv[1][0].get("distance_centre_m") or 0.0)),
            -float((kv[1][0].get("surface_ha") or 0.0)),
        ),
    )

    print(f"\n{'='*110}")
    print(f"CLASSEMENT DES UNITÉS FONCIÈRES CANDIDATES")
    print(f"{'='*110}")
    print(f"UF distinctes : {len(uf_sorted)}  |  Sous-ensembles totaux : {len(resultats)}\n")

    all_subset_ids_for_geojson = []

    for rang_uf, (uf_id, subsets) in enumerate(uf_sorted, 1):
        best_r = subsets[0]
        best_dist = float(best_r.get("distance_centre_m") or 0.0)

        # Toutes les parcelles membres (union de tous les sous-ensembles de cette UF)
        all_idus = sorted({idu for r in subsets for idu in r["idus"]})

        print(f"{'─'*110}")
        print(
            f"#{rang_uf:<3} UF {uf_id}"
            f"  |  {len(all_idus)} parcelles membres"
            f"  |  Dist centre : {best_dist/1000:.2f} km"
            f"  |  {len(subsets)} sous-ensembles candidats"
        )
        print(f"     Parcelles : {', '.join(all_idus)}")
        print()
        print(f"     {'k':<5} {'Dist(km)':<9} {'Surf (ha)':<11} {'Miller':<8} {'Hydro':<10} IDUs")
        print(f"     {'─'*95}")

        for r in subsets:
            dist_km = float(r.get("distance_centre_m") or 0.0) / 1000.0
            h = f"{r['dist_surface_hydro_m']:.0f} m" if r.get("dist_surface_hydro_m") is not None else "—"
            idus_str = ", ".join(r["idus"][:4])
            if len(r["idus"]) > 4:
                idus_str += f" +{len(r['idus'])-4}"
            print(f"     k={r['k']:<4} {dist_km:<9.3f} {r['surface_ha']:<11.2f} {r['miller']:<8.3f} {h:<10} {idus_str}")
            all_subset_ids_for_geojson.append(r["subset_id"])

        print()

    print(f"{'='*110}")
    print(f"Total : {len(uf_sorted)} UF candidates  |  {len(resultats)} sous-ensembles éligibles")

    # ── Export GeoJSON (tous les sous-ensembles candidats) ───────────────
    if all_subset_ids_for_geojson:
        with engine.begin() as conn:
            geom_rows = conn.execute(
                text("""
                    SELECT subset_id, ST_AsGeoJSON(geom_2154) AS geom_geojson
                    FROM ecocompensation_results.sous_ensembles
                    WHERE project_id = :pid
                      AND subset_id = ANY(:subset_ids)
                """),
                {"pid": PROJECT_ID, "subset_ids": all_subset_ids_for_geojson},
            ).mappings().all()

        geom_by_id = {row["subset_id"]: row["geom_geojson"] for row in geom_rows}
        features = []
        for rang_uf, (uf_id, subsets) in enumerate(uf_sorted, 1):
            for rank_ss, r in enumerate(subsets, 1):
                sid = r["subset_id"]
                geom_json = geom_by_id.get(sid)
                if not geom_json:
                    continue
                features.append({
                    "type": "Feature",
                    "geometry": json.loads(geom_json),
                    "properties": {
                        "rang_uf":             rang_uf,
                        "rang_ss":             rank_ss,
                        "subset_id":           sid,
                        "uf_id":               uf_id,
                        "k":                   r["k"],
                        "surface_ha":          float(r["surface_ha"]),
                        "miller":              float(r["miller"]),
                        "distance_centre_km":  round(float(r["distance_centre_m"]) / 1000, 3),
                        "dist_hydro_m":        float(r["dist_surface_hydro_m"]) if r.get("dist_surface_hydro_m") is not None else None,
                        "idus":                r["idus"],
                        "idus_str":            ", ".join(r["idus"]),
                    },
                })

        fc = {"type": "FeatureCollection", "features": features}
        out_path = Path(__file__).parent / "filtre_uf_resultats.geojson"
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(fc, f, ensure_ascii=False)
        print(f"\n📁 GeoJSON exporté : {out_path}  ({len(features)} features)")


if __name__ == "__main__":
    main()