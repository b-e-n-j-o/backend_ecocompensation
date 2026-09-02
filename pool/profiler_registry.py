"""
profiler_registry.py
====================

Associe chaque méthodologie d'étude aux profilers pool à exécuter.
Permet de séparer faune_buffer et zones_humides_intra sans dupliquer profiling_service.
"""

from __future__ import annotations

from sqlalchemy import text

from pool.profilers.base import BasePoolProfiler
from pool.profilers.hydros import HydrosProfiler
from pool.profilers.personnes_morales import PersonnesMoralesProfiler
from pool.profilers.score_eco import ScoreEcoProfiler
from pool.profilers.vegetation_hybride import VegetationHybrideProfiler

STUDY_TYPE_FAUNE = "faune_buffer"
STUDY_TYPE_ZH = "zones_humides_intra"
DEFAULT_STUDY_TYPE = STUDY_TYPE_FAUNE

# Ordre d'exécution : les profilers indépendants d'abord, score_eco en dernier (faune).
PROFILER_REGISTRY: dict[str, list[type[BasePoolProfiler]]] = {
    STUDY_TYPE_FAUNE: [
        PersonnesMoralesProfiler,
        ScoreEcoProfiler,
    ],
    STUDY_TYPE_ZH: [
        PersonnesMoralesProfiler,
        VegetationHybrideProfiler,
        HydrosProfiler,
    ],
}

PROFILING_LAYER_KEYS: dict[str, list[str]] = {
    STUDY_TYPE_ZH: ["bd_topo_et_cesbio"],
}


def resolve_study_type(conn, project_id: str, run_id: str | None = None) -> str:
    """Lit study_type depuis le run pool, sinon depuis le projet."""
    if run_id:
        row = conn.execute(
            text(
                """
                SELECT options_json->>'study_type' AS study_type
                FROM ecocompensation_results.parcelles_pool_runs
                WHERE project_id = CAST(:project_id AS uuid)
                  AND id = CAST(:run_id AS uuid)
                """
            ),
            {"project_id": project_id, "run_id": run_id},
        ).mappings().one_or_none()
        if row and row.get("study_type"):
            return str(row["study_type"])

    row = conn.execute(
        text(
            """
            SELECT COALESCE(study_type, :default) AS study_type
            FROM ecocompensation.projects
            WHERE id = CAST(:project_id AS uuid)
            """
        ),
        {"project_id": project_id, "default": DEFAULT_STUDY_TYPE},
    ).mappings().one_or_none()
    if row and row.get("study_type"):
        return str(row["study_type"])
    return DEFAULT_STUDY_TYPE


def get_profilers_for_study_type(study_type: str) -> list[BasePoolProfiler]:
    classes = PROFILER_REGISTRY.get(study_type) or PROFILER_REGISTRY[DEFAULT_STUDY_TYPE]
    return [cls() for cls in classes]


def profiling_layer_keys_for_study_type(study_type: str) -> list[str]:
    return list(PROFILING_LAYER_KEYS.get(study_type, []))


def profiling_summary_label(study_type: str) -> str:
    profilers = get_profilers_for_study_type(study_type)
    keys = ", ".join(p.metric_key for p in profilers)
    if study_type == STUDY_TYPE_ZH:
        return f"Profilage pool ZH ({keys})"
    return f"Profilage pool faune ({keys})"
