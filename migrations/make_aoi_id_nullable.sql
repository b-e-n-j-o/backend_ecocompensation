-- Migration: rendre aoi_id nullable dans les tables ecocompensation_results
-- qui avaient été créées avec aoi_id NOT NULL (ancien schéma).
-- Le code n’insère que project_id ; sans cette migration, INSERT échoue avec
-- "null value in column aoi_id violates not-null constraint".
-- Si une table n’a pas la colonne aoi_id, l’instruction correspondante échouera :
-- ignorer ou commenter la ligne.

ALTER TABLE ecocompensation_results.zone_de_vegetation
  ALTER COLUMN aoi_id DROP NOT NULL;

ALTER TABLE ecocompensation_results.zone_humide
  ALTER COLUMN aoi_id DROP NOT NULL;

ALTER TABLE ecocompensation_results.troncons_hydro
  ALTER COLUMN aoi_id DROP NOT NULL;

ALTER TABLE ecocompensation_results.routes
  ALTER COLUMN aoi_id DROP NOT NULL;

ALTER TABLE ecocompensation_results.surfaces_hydro
  ALTER COLUMN aoi_id DROP NOT NULL;

ALTER TABLE ecocompensation_results.fragmentation_polygons
  ALTER COLUMN aoi_id DROP NOT NULL;

ALTER TABLE ecocompensation_results.ebc
  ALTER COLUMN aoi_id DROP NOT NULL;

ALTER TABLE ecocompensation_results.znieff
  ALTER COLUMN aoi_id DROP NOT NULL;

ALTER TABLE ecocompensation_results.frayeres
  ALTER COLUMN aoi_id DROP NOT NULL;



