"""
visuels/basemap_utils.py
========================
Helpers robustes pour ajouter un fond de carte contextily avec retry.
"""
from __future__ import annotations

import logging
import time
from typing import Optional


def add_basemap_with_retry(
    ax,
    crs_str: str,
    logger: Optional[logging.Logger] = None,
    retries_per_provider: int = 2,
    retry_delay_s: float = 0.8,
) -> bool:
    """
    Ajoute un basemap contextily avec retry + fallback provider.

    Retourne True si un raster a bien été injecté dans l'axe, False sinon.
    """
    log = logger or logging.getLogger(__name__)

    try:
        import contextily as ctx
    except Exception as exc:
        log.warning("Basemap indisponible : contextily non importable (%s)", exc)
        return False

    providers = [
        ("Esri.WorldImagery", ctx.providers.Esri.WorldImagery),
        ("OpenStreetMap.Mapnik", ctx.providers.OpenStreetMap.Mapnik),
    ]
    initial_images = len(ax.images)

    for provider_name, provider in providers:
        for attempt in range(1, max(int(retries_per_provider), 1) + 1):
            try:
                before = len(ax.images)
                ctx.add_basemap(
                    ax,
                    crs=crs_str,
                    source=provider,
                    zoom="auto",
                    attribution=False,
                    zorder=0,
                )
                after = len(ax.images)
                if after > before or after > initial_images:
                    if attempt > 1:
                        log.info("Basemap %s chargé après retry #%d", provider_name, attempt)
                    return True
                raise RuntimeError("Aucune tuile raster ajoutée à l'axe")
            except Exception as exc:
                is_last = attempt >= max(int(retries_per_provider), 1)
                level = log.warning if is_last else log.info
                level(
                    "Échec basemap %s tentative %d/%d (%s)",
                    provider_name,
                    attempt,
                    max(int(retries_per_provider), 1),
                    exc,
                )
                if not is_last:
                    time.sleep(max(float(retry_delay_s), 0.0) * attempt)

    log.warning("Basemap non chargé après tous les retries (providers Esri/OSM).")
    return False
