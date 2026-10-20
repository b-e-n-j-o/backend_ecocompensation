from __future__ import annotations

from .base import BasePoolProfiler


class ZoneHumideProfiler(BasePoolProfiler):
    metric_key = "zone_humide"

    def compute_for_run(self, conn, project_id: str, run_id: str) -> dict[str, dict]:
        # Placeholder: implémentation future
        return {}
