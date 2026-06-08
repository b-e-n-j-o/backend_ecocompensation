#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
enrich_candidates.py (v3 — veg via staging, fauna optional)
===========================================================

Enrichit `ecocompensation_results.parcelles` avec :
  - veg_libelles    text[] : libellés végétation intersectant la parcelle
  - fauna_distances jsonb  : { "nom_vernaculaire": dist_m }

Pourquoi un staging veg ?
  `vegetation_sur_cesbio` est nationale (millions d'entités). Faire le join
  direct contre des milliers de parcelles candidates peut dépasser les timeouts
  du pooler Supabase. On pré-filtre donc la végétation sur l'emprise réelle des
  parcelles candidates (bbox + petite marge), pas sur l'AOI entière, puis on
  fait l'UPDATE contre cette table indexée + ANALYZE.

Contrainte Supabase/pgBouncer (transaction pooler) :
  éviter les TEMP TABLE ; on utilise une table “normale” nommée par project_id.
"""

from __future__ import annotations

from typing import Callable

from sqlalchemy import text

# ── Config ────────────────────────────────────────────────────────────────────

# Marge autour de la bbox des parcelles candidates (mètres, SRID 2154).
PARCEL_VEG_MARGIN_M = 500
STMT_TIMEOUT = "90s"

# ── DDL migration ─────────────────────────────────────────────────────────────

_ALTER_DDL = """
ALTER TABLE ecocompensation_results.parcelles
    ADD COLUMN IF NOT EXISTS veg_libelles    text[]  NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS fauna_distances jsonb   NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_parcelles_results_veg
    ON ecocompensation_results.parcelles USING GIN (veg_libelles);

CREATE INDEX IF NOT EXISTS idx_parcelles_results_fauna
    ON ecocompensation_results.parcelles USING GIN (fauna_distances);
"""


def ensure_columns(engine) -> None:
    """Migration one-shot. Appeler au démarrage de l'app (lifespan FastAPI)."""
    with engine.begin() as conn:
        conn.execute(text(_ALTER_DDL))


def _staging_table(project_id: str) -> str:
    # Identifiant SQL safe : uuid → on remplace '-' par '_' et on garde [0-9a-f_]
    # (project_id est un UUID côté app, donc pas de surface d'injection ici).
    safe = project_id.lower().replace("-", "_")
    return f'ecocompensation_staging.veg_{safe}'


_SQL_CREATE_VEG_STAGING = """
CREATE SCHEMA IF NOT EXISTS ecocompensation_staging;
"""

# Note: le nom de table est injecté (identifiant) → impossible via bind params.
_SQL_VEG_STAGING_DROP = """
DROP TABLE IF EXISTS {staging_table}
"""

_SQL_VEG_STAGING_CREATE = """
CREATE TABLE {staging_table} AS
SELECT v.libelle_prio, v.geom
FROM ecocompensation.vegetation_sur_cesbio v
WHERE v.libelle_prio IS NOT NULL
  AND ST_Intersects(
        v.geom,
        (
            SELECT ST_Buffer(
                ST_Envelope(ST_Collect(p.geom_2154)),
                :margin_m
            )
            FROM ecocompensation_results.parcelles p
            WHERE p.project_id = :pid
        )
      )
"""

_SQL_VEG_STAGING_INDEX = """
CREATE INDEX IF NOT EXISTS {staging_index}
    ON {staging_table} USING GIST (geom)
"""

_SQL_VEG_STAGING_ANALYZE = """
ANALYZE {staging_table}
"""

_SQL_UPDATE_VEG_FROM_STAGING_TEMPLATE = """
WITH hits AS (
    SELECT DISTINCT
        p.idu,
        v.libelle_prio
    FROM ecocompensation_results.parcelles p
    JOIN {staging_table} v
        ON v.geom && p.geom_2154
       AND ST_Intersects(v.geom, p.geom_2154)
    WHERE p.project_id = :pid
      AND v.libelle_prio IS NOT NULL
),
veg_agg AS (
    SELECT
        idu,
        array_agg(libelle_prio) AS veg_libelles
    FROM hits
    GROUP BY idu
)
UPDATE ecocompensation_results.parcelles p
SET    veg_libelles = COALESCE(va.veg_libelles, '{{}}')
FROM   veg_agg va
WHERE  p.project_id = :pid
  AND  p.idu        = va.idu;
"""

_SQL_DROP_STAGING_TEMPLATE = """
DROP TABLE IF EXISTS {staging_table}
"""

# Faune : distance KNN (opérateur <-> → GiST) par (parcelle, espèce).
_SQL_FAUNA = """
WITH fauna_agg AS (
    SELECT
        p.idu,
        jsonb_object_agg(
            sp,
            COALESCE(
                (
                    SELECT ROUND(ST_Distance(p.geom_2154, f.geometry))::int
                    FROM   ecocompensation.fauna f
                    WHERE  f.nom_vernaculaire = sp
                    ORDER  BY p.geom_2154 <-> f.geometry
                    LIMIT  1
                ),
                -1
            )
        ) AS fauna_distances
    FROM  ecocompensation_results.parcelles p
    CROSS JOIN unnest(:species_list::text[]) AS sp
    WHERE p.project_id = :pid
    GROUP BY p.idu
)
UPDATE ecocompensation_results.parcelles p
SET    fauna_distances = COALESCE(fa.fauna_distances, '{}')
FROM   fauna_agg fa
WHERE  p.project_id = :pid
  AND  p.idu        = fa.idu
"""


def run(
    engine,
    project_id: str,
    aoi_id: str,  # gardé pour compatibilité layer_runner
    cb: Callable[[str], None] | None = None,
    *,
    species_list: list[str] | None = None,
) -> int:
    """
    Enrichit les parcelles candidates du projet.
    Doit être appelé APRÈS aoi_to_parcelles_v2 dans le LAYER_REGISTRY.
    Retourne le nombre de candidates (parcelles du projet).
    """
    log = cb or (lambda msg: None)
    species = species_list or []

    # ── 1) Compter les candidates ────────────────────────────────────────
    with engine.begin() as conn:
        n = conn.execute(
            text(
                "SELECT COUNT(*) FROM ecocompensation_results.parcelles "
                "WHERE project_id = :pid"
            ),
            {"pid": project_id},
        ).scalar_one()

    if n == 0:
        log("[ENRICH] ⚠️  Aucune parcelle candidate — lancer d'abord la couche 'parcelles'.")
        return 0

    log(f"[ENRICH] {n:,} candidates à enrichir (project_id={project_id}).")

    # ── 2) Veg via staging ───────────────────────────────────────────────
    staging_table = _staging_table(project_id)
    staging_index = f"idx_{staging_table.split('.', 1)[1]}_geom"

    log(
        f"[ENRICH] 🌿 VEG staging (bbox parcelles + {PARCEL_VEG_MARGIN_M}m) → {staging_table}"
    )
    with engine.begin() as conn:
        conn.execute(text(f"SET LOCAL statement_timeout = '{STMT_TIMEOUT}'"))
        conn.execute(text(_SQL_CREATE_VEG_STAGING))
        # Important: ne pas envoyer plusieurs commandes SQL dans une seule
        # requête préparée (psycopg / pgBouncer). On exécute donc chaque
        # commande séparément.
        conn.execute(
            text(_SQL_VEG_STAGING_DROP.format(staging_table=staging_table)),
        )
        conn.execute(
            text(_SQL_VEG_STAGING_CREATE.format(staging_table=staging_table)),
            {"pid": project_id, "margin_m": PARCEL_VEG_MARGIN_M},
        )
        conn.execute(
            text(
                _SQL_VEG_STAGING_INDEX.format(
                    staging_index=staging_index,
                    staging_table=staging_table,
                )
            ),
        )
        conn.execute(
            text(_SQL_VEG_STAGING_ANALYZE.format(staging_table=staging_table)),
        )
    log("[ENRICH] 🌿 VEG staging créé + indexé.")

    try:
        log(f"[ENRICH] 🌿 UPDATE veg_libelles depuis staging — {n:,} parcelle(s), une passe…")
        with engine.begin() as conn:
            conn.execute(text(f"SET LOCAL statement_timeout = '{STMT_TIMEOUT}'"))
            conn.execute(
                text(
                    _SQL_UPDATE_VEG_FROM_STAGING_TEMPLATE.format(
                        staging_table=staging_table
                    )
                ),
                {"pid": project_id},
            )

        with engine.begin() as conn:
            n_veg_nonempty = conn.execute(
                text(
                    "SELECT COUNT(*) FROM ecocompensation_results.parcelles "
                    "WHERE project_id = :pid AND cardinality(veg_libelles) > 0"
                ),
                {"pid": project_id},
            ).scalar_one()
        log(f"[ENRICH] ✓ veg_libelles mis à jour (non-vide: {n_veg_nonempty:,}/{n:,}).")
    finally:
        with engine.begin() as conn:
            conn.execute(text(_SQL_DROP_STAGING_TEMPLATE.format(staging_table=staging_table)))
        log("[ENRICH] 🌿 VEG staging nettoyé.")

    # ── 3) Faune (optionnel) ─────────────────────────────────────────────
    if species:
        log(f"[ENRICH] 🦎 Faune — {len(species)} espèce(s) : {', '.join(species)}")
        with engine.begin() as conn:
            conn.execute(text(f"SET LOCAL statement_timeout = '{STMT_TIMEOUT}'"))
            conn.execute(text(_SQL_FAUNA), {"pid": project_id, "species_list": species})
        with engine.begin() as conn:
            n_fauna_nonempty = conn.execute(
                text(
                    "SELECT COUNT(*) FROM ecocompensation_results.parcelles "
                    "WHERE project_id = :pid AND fauna_distances <> '{}'::jsonb"
                ),
                {"pid": project_id},
            ).scalar_one()
        log(f"[ENRICH] ✓ fauna_distances mis à jour (non-vide: {n_fauna_nonempty:,}/{n:,}).")
    else:
        log("[ENRICH] Aucune espèce sélectionnée — fauna_distances ignorée.")

    return n
