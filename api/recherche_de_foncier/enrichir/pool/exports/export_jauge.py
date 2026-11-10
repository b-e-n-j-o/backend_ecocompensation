"""Réduction d'un export au curseur distance / surface de l'écran résultats.

La jauge ne modifie pas le pool en base. Elle ne s'applique que si l'export
reçoit explicitement une borne (distance max au projet, surface minimale).
"""
from __future__ import annotations

from typing import Any


def _num(row: dict, *keys: str) -> float | None:
    for key in keys:
        raw = row.get(key)
        if raw is None or raw == "":
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def within_jauge(
    row: dict,
    *,
    max_distance_km: float | None,
    min_surface_ha: float | None,
    distance_keys: tuple[str, ...],
    surface_keys: tuple[str, ...],
) -> bool:
    if max_distance_km is not None:
        dist = _num(row, *distance_keys)
        if dist is None or dist > max_distance_km + 1e-6:
            return False
    if min_surface_ha is not None and min_surface_ha > 0:
        surf = _num(row, *surface_keys)
        if surf is None or surf + 1e-9 < min_surface_ha:
            return False
    return True


def filter_parcelles(parcelles: list, max_distance_km: float | None, min_surface_ha: float | None) -> list:
    return [
        p for p in parcelles
        if isinstance(p, dict) and within_jauge(
            p,
            max_distance_km=max_distance_km,
            min_surface_ha=min_surface_ha,
            distance_keys=("distance_km",),
            surface_keys=("surface_ha",),
        )
    ]


def filter_results_uf(results_uf: dict | None, max_distance_km: float | None, min_surface_ha: float | None) -> dict | None:
    if not isinstance(results_uf, dict):
        return results_uf
    kept = [
        uf for uf in (results_uf.get("unites_foncieres") or [])
        if isinstance(uf, dict) and within_jauge(
            uf,
            max_distance_km=max_distance_km,
            min_surface_ha=min_surface_ha,
            distance_keys=("distance_centre_km",),
            surface_keys=("surface_ha_parcelles", "surface_ha"),
        )
    ]
    out = dict(results_uf)
    out["unites_foncieres"] = kept
    out["total_uf"] = len(kept)
    return out


def filter_ilots(ilots: list[dict[str, Any]], max_distance_km: float | None, min_surface_ha: float | None) -> list[dict[str, Any]]:
    return [
        il for il in ilots
        if within_jauge(
            il,
            max_distance_km=max_distance_km,
            min_surface_ha=min_surface_ha,
            distance_keys=("distance_centre_km",),
            surface_keys=("surface_ha",),
        )
    ]
