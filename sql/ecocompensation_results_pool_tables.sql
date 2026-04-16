CREATE TABLE IF NOT EXISTS ecocompensation_results.parcelles_pool_runs (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL,
    scope text NOT NULL DEFAULT 'parcelles',
    options_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    total_count integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    result_summary jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_pool_runs_project_created
ON ecocompensation_results.parcelles_pool_runs(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS ecocompensation_results.parcelles_pool (
    run_id uuid NOT NULL,
    project_id uuid NOT NULL,
    idu text NOT NULL,
    rank integer NULL,
    surface_ha double precision NULL,
    miller double precision NULL,
    distance_km double precision NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, idu)
);

CREATE INDEX IF NOT EXISTS idx_pool_project_run
ON ecocompensation_results.parcelles_pool(project_id, run_id);

CREATE TABLE IF NOT EXISTS ecocompensation_results.parcelles_pool_metrics (
    run_id uuid NOT NULL,
    project_id uuid NOT NULL,
    idu text NOT NULL,
    metric_key text NOT NULL,
    metric_value_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, idu, metric_key)
);

CREATE INDEX IF NOT EXISTS idx_pool_metrics_project_run_idu
ON ecocompensation_results.parcelles_pool_metrics(project_id, run_id, idu);

CREATE TABLE IF NOT EXISTS ecocompensation_results.parcelles_pool_indesirables (
    run_id uuid NOT NULL,
    project_id uuid NOT NULL,
    idu text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, idu)
);

CREATE INDEX IF NOT EXISTS idx_pool_indesirables_project_run
ON ecocompensation_results.parcelles_pool_indesirables(project_id, run_id);
