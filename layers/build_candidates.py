#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_candidates.py
===================

Construit ecocompensation_results.candidate_parcelles pour un couple
(project_id, aoi_id) en UNE SEULE passe SQL.

Remplace l'ensemble des aoi_to_*.py in-DB (fast=True) du LAYER_REGISTRY :
  - cesbio / bd_topo_et_cesbio → veg_libelles  (text[])
  - fauna                      → fauna_distances (jsonb)
  + colonnes à étendre au fur et à mesure (zone_humide, carhab, etc.)

Pas de copie de couche, pas de ST_Intersection, pas de table résultat par couche.

Intégration dans layer_runner.py :
  - Remplace les entrées fast=True du LAYER_REGISTRY par UNE entrée "candidates".
  - Signature compatible : run(engine, project_id, aoi_id, cb, **kwargs)

DDL :
  - ensure_table(engine) → appeler UNE FOIS au démarrage de l'app (lifespan FastAPI).
  - Jamais dans une transaction par projet.
"""

from __future__ import annotations
from typing import Callable

from sqlalchemy import text

# ---------------------------------------------------------------------------
# DDL — migration one-shot, pas runtime.
# ---------------------------------------------------------------------------
CANDIDATE_DDL = """
CREATE TABLE IF NOT EXISTS ecocompensation_results.candidate_parcelles (
    id              uuid            NOT NULL DEFAULT gen_random_uuid(),
    project_id      uuid            NOT NULL,
    aoi_id          uuid            NOT NULL,

    -- Identifiant cadastral source (DGFiP / Etalab)
    code_parcelle   text            NOT NULL,

    -- Géométrie + surface
    geom_2154       geometry(Geometry, 2154) NOT NULL,
    surface_ha      numeric(12, 4)  NOT NULL,

    -- ── Flags végétation ────────────────────────────────────────────────
    -- Array des libelle_prio (COALESCE(nature, libelle)) intersectant
    -- la parcelle dans vegetation_sur_cesbio.
    -- Filtre : 'Forêt fermée' = ANY(veg_libelles)
    --          veg_libelles && ARRAY['Forêt fermée', 'Forêt ouverte']
    veg_libelles    text[]          NOT NULL DEFAULT '{}',

    -- ── Distances faune ──────────────────────────────────────────────────
    -- { "Starier pâtre": 200, "Cisticole des joncs": 0 }
    -- -1 = aucune observation connue pour cette espèce dans la DB.
    -- Filtre : (fauna_distances->>'Starier pâtre')::int <= 500
    fauna_distances jsonb           NOT NULL DEFAULT '{}',

    created_at      timestamptz     NOT NULL DEFAULT now(),

    PRIMARY KEY (id)
);

-- Index pour les JOINs project/aoi
CREATE INDEX IF NOT EXISTS idx_cand_project
    ON ecocompensation_results.candidate_parcelles (project_id);
CREATE INDEX IF NOT EXISTS idx_cand_aoi
    ON ecocompensation_results.candidate_parcelles (aoi_id);

-- Index spatial (affichage pool sur carte, intersection UF)
CREATE INDEX IF NOT EXISTS idx_cand_geom
    ON ecocompensation_results.candidate_parcelles USING GIST (geom_2154);

-- Index GIN pour les filtres sur veg_libelles (ANY / &&)
CREATE INDEX IF NOT EXISTS idx_cand_veg
    ON ecocompensation_results.candidate_parcelles USING GIN (veg_libelles);

-- Index GIN pour les filtres jsonb fauna
-- Utile pour @> ; pour les comparaisons numériques (->>'espece')::int <= N
-- un index d'expression par espèce serait plus ciblé, mais pas pratique
-- avec des espèces dynamiques → GIN généraliste pour le prototype.
CREATE INDEX IF NOT EXISTS idx_cand_fauna
    ON ecocompensation_results.candidate_parcelles USING GIN (fauna_distances);
"""


def ensure_table(engine) -> None:
    """
    Crée candidate_parcelles + ses index si inexistants.
    Appeler dans le lifespan FastAPI, pas à chaque run projet.

    Exemple dans main.py :
        @asynccontextmanager
        async def lifespan(app):
            from layers.build_candidates import ensure_table
            ensure_table(get_engine())
            yield
    """
    with engine.begin() as conn:
        conn.execute(text(CANDIDATE_DDL))


# ---------------------------------------------------------------------------
# SQL principal
# ---------------------------------------------------------------------------
_SQL_BUILD = """
WITH

-- ── 1. Buffer AOI (matérialisé une fois) ────────────────────────────────
aoi_buffer AS MATERIALIZED (
    SELECT ST_Buffer(geom_2154, :buffer_m) AS buf
    FROM ecocompensation.aoi
    WHERE id = :aoi_id
),

-- ── 2. Parcelles candidates ──────────────────────────────────────────────
-- Source : parcelles.parcelles, partitionnée par code_dep.
--
-- ⚠  COLONNES À VÉRIFIER dans ta DDL réelle (aoi_to_parcelles_v2.py) :
--    - Nom de la colonne identifiant : p.id ? p.id_parcelle ? p.idu ?
--    - Nom de la colonne géom       : p.geom ? p.geom_2154 ?
--    - SRID de la géométrie source  : doit être 2154 pour que ST_Distance
--      fauna soit en mètres (sinon ajouter ST_Transform).
--
-- ⚠  Si fauna.geometry n'est pas en 2154, wrapper avec ST_Transform(f.geometry, 2154)
--    dans la sous-requête KNN ci-dessous.
candidates AS MATERIALIZED (
    SELECT
        p.id          AS code_parcelle,   -- ← À VÉRIFIER
        p.geom        AS geom_2154,       -- ← À VÉRIFIER
        ROUND((ST_Area(p.geom) / 10_000.0)::numeric, 4) AS surface_ha
    FROM parcelles.parcelles p            -- partitionnée par code_dep
    JOIN aoi_buffer b ON ST_Intersects(p.geom, b.buf)
),

-- ── 3. Flags végétation ──────────────────────────────────────────────────
-- Array distinct des libelle_prio (col générée = COALESCE(nature, libelle))
-- pour toutes les entités vegetation_sur_cesbio qui croisent la parcelle.
-- ST_Intersects utilise l'index GiST vegetation_sur_cesbio_geom_idx.
veg_flags AS (
    SELECT
        c.code_parcelle,
        array_agg(DISTINCT v.libelle_prio)
            FILTER (WHERE v.libelle_prio IS NOT NULL) AS veg_libelles
    FROM candidates c
    JOIN ecocompensation.vegetation_sur_cesbio v
        ON ST_Intersects(v.geom, c.geom_2154)
    GROUP BY c.code_parcelle
),

-- ── 4. Distances faune ───────────────────────────────────────────────────
-- Pour chaque (parcelle candidate × espèce sélectionnée), distance en mètres
-- à l'observation la plus proche, via KNN GiST (<->).
--
-- Si :species_list est vide ([]) :
--   CROSS JOIN unnest donne 0 lignes → fauna_flags vide
--   → LEFT JOIN dans le SELECT final → NULL → COALESCE → '{}'
--
-- Valeur -1 : aucune observation recensée pour cette espèce dans la DB.
fauna_flags AS (
    SELECT
        c.code_parcelle,
        jsonb_object_agg(
            sp,
            COALESCE(
                (
                    SELECT ROUND(ST_Distance(c.geom_2154, f.geometry))::int
                    FROM ecocompensation.fauna f
                    WHERE f.nom_vernaculaire = sp
                    ORDER BY c.geom_2154 <-> f.geometry  -- KNN via GiST idx_fauna_geometry
                    LIMIT 1
                ),
                -1
            )
        ) AS fauna_distances
    FROM candidates c
    CROSS JOIN unnest(:species_list::text[]) AS sp
    GROUP BY c.code_parcelle
)

-- ── 5. INSERT ────────────────────────────────────────────────────────────
INSERT INTO ecocompensation_results.candidate_parcelles (
    project_id, aoi_id,
    code_parcelle, geom_2154, surface_ha,
    veg_libelles, fauna_distances
)
SELECT
    :project_id ::uuid,
    :aoi_id     ::uuid,
    c.code_parcelle,
    c.geom_2154,
    c.surface_ha,
    COALESCE(vf.veg_libelles,    '{}'::text[]),
    COALESCE(ff.fauna_distances, '{}'::jsonb)
FROM candidates c
LEFT JOIN veg_flags   vf ON vf.code_parcelle = c.code_parcelle
LEFT JOIN fauna_flags ff ON ff.code_parcelle = c.code_parcelle
RETURNING id
"""


def run(
    engine,
    project_id:   str,
    aoi_id:       str,
    cb:           Callable[[str], None] | None = None,
    *,
    species_list: list[str] | None = None,
    buffer_m:     int = 15_000,
) -> int:
    """
    Calcule et insère les parcelles candidates pour (project_id, aoi_id).

    Signature compatible avec les wrappers _make_* de layer_runner.py.
    Retourne le nombre de candidates insérées.

    Idempotent : supprime les candidates existantes du projet avant insertion.
    """
    log = cb or (lambda msg: None)
    species = species_list or []

    log(f"[CANDIDATES] buffer={buffer_m / 1000:.0f} km | "
        f"espèces fauna : {', '.join(species) or 'aucune'}")

    # Idempotence : supprimer les candidates du projet si re-run
    with engine.begin() as conn:
        deleted = conn.execute(
            text("DELETE FROM ecocompensation_results.candidate_parcelles "
                 "WHERE project_id = :pid"),
            {"pid": project_id},
        ).rowcount
        if deleted:
            log(f"[CANDIDATES] {deleted} candidates existantes supprimées (re-run).")

    with engine.begin() as conn:
        rows = conn.execute(
            text(_SQL_BUILD),
            {
                "project_id":   project_id,
                "aoi_id":       aoi_id,
                "buffer_m":     buffer_m,
                "species_list": species,   # psycopg3 sérialise list → text[]
            },
        ).fetchall()

    n = len(rows)
    log(f"[CANDIDATES] {n:,} parcelles candidates insérées.")
    return n