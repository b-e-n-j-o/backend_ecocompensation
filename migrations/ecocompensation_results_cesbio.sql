-- Table résultats CESBIO (créée aussi par aoi_to_cesbio.py si absente).
-- Libellés alignés sur ajouter_nomenclature_cesbio.sql.

CREATE SCHEMA IF NOT EXISTS ecocompensation_results;

CREATE TABLE IF NOT EXISTS ecocompensation_results.cesbio (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    project_id uuid NULL,
    aoi_id uuid NULL,
    classe smallint NULL,
    libelle_classe text NULL,
    geom_2154 geometry(Geometry, 2154) NOT NULL,
    created_at timestamptz NULL DEFAULT now(),
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS idx_ecocomp_results_cesbio_geom
    ON ecocompensation_results.cesbio USING GIST (geom_2154);

CREATE INDEX IF NOT EXISTS idx_ecocomp_results_cesbio_project
    ON ecocompensation_results.cesbio (project_id);
