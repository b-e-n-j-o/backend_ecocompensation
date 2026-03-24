-- Migration: ajouter la colonne project_id à toutes les tables ecocompensation_results
-- à exécuter si les tables ont été créées avant l'introduction de project_id.
-- PostgreSQL 11+ pour ADD COLUMN IF NOT EXISTS.

-- Liste alignée sur main.py RESULT_TABLES et delete_project_data_cli.py

ALTER TABLE ecocompensation_results.parcelles
  ADD COLUMN IF NOT EXISTS project_id uuid;

ALTER TABLE ecocompensation_results.ebc
  ADD COLUMN IF NOT EXISTS project_id uuid;

ALTER TABLE ecocompensation_results.mesures_compensatoire_surf
  ADD COLUMN IF NOT EXISTS project_id uuid;

ALTER TABLE ecocompensation_results.mesures_compensatoire_lin
  ADD COLUMN IF NOT EXISTS project_id uuid;

ALTER TABLE ecocompensation_results.mesures_compensatoire_pct
  ADD COLUMN IF NOT EXISTS project_id uuid;

ALTER TABLE ecocompensation_results.mesures_compensatoire_commune
  ADD COLUMN IF NOT EXISTS project_id uuid;

ALTER TABLE ecocompensation_results.patrimoine_naturel
  ADD COLUMN IF NOT EXISTS project_id uuid;

ALTER TABLE ecocompensation_results.zone_de_vegetation
  ADD COLUMN IF NOT EXISTS project_id uuid;

ALTER TABLE ecocompensation_results.zone_humide
  ADD COLUMN IF NOT EXISTS project_id uuid;

ALTER TABLE ecocompensation_results.troncons_hydro
  ADD COLUMN IF NOT EXISTS project_id uuid;

ALTER TABLE ecocompensation_results.surfaces_hydro
  ADD COLUMN IF NOT EXISTS project_id uuid;

ALTER TABLE ecocompensation_results.surfaces_elementaires
  ADD COLUMN IF NOT EXISTS project_id uuid;

ALTER TABLE ecocompensation_results.routes
  ADD COLUMN IF NOT EXISTS project_id uuid;

ALTER TABLE ecocompensation_results.voies_ferrees
  ADD COLUMN IF NOT EXISTS project_id uuid;

ALTER TABLE ecocompensation_results.fragmentation_polygons
  ADD COLUMN IF NOT EXISTS project_id uuid;

ALTER TABLE ecocompensation_results.zones_humides_probables
  ADD COLUMN IF NOT EXISTS project_id uuid;

ALTER TABLE ecocompensation_results.znieff
  ADD COLUMN IF NOT EXISTS project_id uuid;

ALTER TABLE ecocompensation_results.frayeres
  ADD COLUMN IF NOT EXISTS project_id uuid;

ALTER TABLE ecocompensation_results.arrachage_vignes
  ADD COLUMN IF NOT EXISTS project_id uuid;
