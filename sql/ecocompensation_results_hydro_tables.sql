-- Tables projet pour le pipeline zones humides (filter_v2)
-- Clip national → ecocompensation_results.* par project_id + AOI

CREATE SCHEMA IF NOT EXISTS ecocompensation_results;

-- ── Tronçons hydrographiques (BD TOPO national) ───────────────────────────────
CREATE TABLE IF NOT EXISTS ecocompensation_results.troncons_hydros (
    project_id uuid NOT NULL,
    cleabs text NOT NULL,
    code_hydrographique text NULL,
    nature text NULL,
    persistance text NULL,
    fosse boolean NULL,
    navigabilite boolean NULL,
    salinite boolean NULL,
    numero_d_ordre integer NULL,
    origine text NULL,
    sens_de_l_ecoulement text NULL,
    classe_de_largeur text NULL,
    type_de_bras text NULL,
    nom text NULL,
    geom_2154 geometry(Geometry, 2154) NOT NULL,
    created_at timestamptz NULL DEFAULT now(),
    CONSTRAINT troncons_hydros_results_pkey PRIMARY KEY (project_id, cleabs)
);

CREATE INDEX IF NOT EXISTS idx_ecocomp_results_troncons_hydros_geom
    ON ecocompensation_results.troncons_hydros USING GIST (geom_2154);

CREATE INDEX IF NOT EXISTS idx_ecocomp_results_troncons_hydros_project
    ON ecocompensation_results.troncons_hydros (project_id);

-- ── Surfaces hydrographiques (BD TOPO national) ───────────────────────────────
CREATE TABLE IF NOT EXISTS ecocompensation_results.surfaces_hydros (
    project_id uuid NOT NULL,
    cleabs text NOT NULL,
    code_hydrographique text NULL,
    nature text NULL,
    position_par_rapport_au_sol text NULL,
    persistance text NULL,
    salinite boolean NULL,
    origine text NULL,
    statut text NULL,
    commentaire_sur_l_objet_hydro text NULL,
    nom text NULL,
    geom_2154 geometry(Geometry, 2154) NOT NULL,
    created_at timestamptz NULL DEFAULT now(),
    CONSTRAINT surfaces_hydros_results_pkey PRIMARY KEY (project_id, cleabs)
);

CREATE INDEX IF NOT EXISTS idx_ecocomp_results_surfaces_hydros_geom
    ON ecocompensation_results.surfaces_hydros USING GIST (geom_2154);

CREATE INDEX IF NOT EXISTS idx_ecocomp_results_surfaces_hydros_project
    ON ecocompensation_results.surfaces_hydros (project_id);

-- ── Colonnes enrichissement parcelles (filter_v2) ─────────────────────────────
ALTER TABLE ecocompensation_results.parcelles
    ADD COLUMN IF NOT EXISTS dist_hydro_m double precision NULL,
    ADD COLUMN IF NOT EXISTS troncons_hydro_info jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS dist_surface_hydro_m double precision NULL,
    ADD COLUMN IF NOT EXISTS surface_hydro_ha double precision NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS surfaces_hydro_info jsonb NOT NULL DEFAULT '[]'::jsonb;
