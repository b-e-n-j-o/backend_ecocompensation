-- Migration pool pour le pipeline filter_v2
-- À exécuter une fois sur Supabase / Postgres si les colonnes n'existent pas encore.
-- Les migrations légères sont aussi appliquées au runtime via pool_service.ensure_tables().

-- 1. parcelles_pool_runs : résumé + suivi profiling
ALTER TABLE ecocompensation_results.parcelles_pool_runs
    ADD COLUMN IF NOT EXISTS result_summary jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE ecocompensation_results.parcelles_pool_runs
    ADD COLUMN IF NOT EXISTS profiling_progress jsonb NOT NULL DEFAULT '{}'::jsonb;

-- 2. parcelles_pool : distance hydro (nullable)
ALTER TABLE ecocompensation_results.parcelles_pool
    ADD COLUMN IF NOT EXISTS dist_hydro_m double precision NULL;

-- 3. parcelles (staging géométrie) : enrichissement léger filter_v2
ALTER TABLE ecocompensation_results.parcelles
    ADD COLUMN IF NOT EXISTS veg_libelles    text[]  NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS fauna_distances jsonb   NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_parcelles_results_veg
    ON ecocompensation_results.parcelles USING GIN (veg_libelles);

CREATE INDEX IF NOT EXISTS idx_parcelles_results_fauna
    ON ecocompensation_results.parcelles USING GIN (fauna_distances);

-- 4. projects : critères de filtrage persistés
ALTER TABLE ecocompensation.projects
    ADD COLUMN IF NOT EXISTS filter_config jsonb NOT NULL DEFAULT '{}'::jsonb;

-- Vérification
SELECT
    'parcelles_pool_runs' AS table_name,
    column_name,
    data_type
FROM information_schema.columns
WHERE table_schema = 'ecocompensation_results'
  AND table_name = 'parcelles_pool_runs'
  AND column_name IN ('result_summary', 'profiling_progress')
UNION ALL
SELECT
    'parcelles_pool',
    column_name,
    data_type
FROM information_schema.columns
WHERE table_schema = 'ecocompensation_results'
  AND table_name = 'parcelles_pool'
  AND column_name = 'dist_hydro_m'
ORDER BY 1, 2;
