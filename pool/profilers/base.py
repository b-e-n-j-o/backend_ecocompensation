from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BasePoolProfiler(ABC):
    metric_key: str

    @abstractmethod
    def compute_for_run(self, conn, project_id: str, run_id: str) -> dict[str, dict[str, Any]]:
        """
        Retourne un mapping:
          { idu: { ...metric payload... } }
        """
        raise NotImplementedError
