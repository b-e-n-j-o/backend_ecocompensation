#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Options persistées d'un run de filtre — utilisées par les exports CSV/SHP.

Compatible avec le dict `last_filter` de filter_v2 (`pipeline = filter_v2`)
et avec d'anciens dumps (vegetation_hybride, modes hydro historiques).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from layers.national_exclusions import DEFAULT_EXCLUDED_LAYERS

HydroMode = Literal["none", "intersect", "within_radius", "ignore"]
ArrachageVignesMode = Literal["ignore", "intersect", "exclude"]
ZoneHumideMode = Literal["ignore", "intersect", "exclude"]
LayerIntersectMode = Literal["ignore", "intersect", "exclude"]


def _str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if x and str(x).strip()]


@dataclass
class FiltreOptions:
    vegetation_hybride: dict
    carhab_nom_eunis: list[str]
    excluded_layers: list[str]
    ebc_mode: LayerIntersectMode
    natura2000_mode: LayerIntersectMode
    reserves_naturelles_mode: LayerIntersectMode
    znieff_mode: LayerIntersectMode
    arrachage_vignes_mode: ArrachageVignesMode
    zone_humide_mode: ZoneHumideMode
    remontee_nappes_classefiab: list[str]
    troncon_hydro_mode: HydroMode
    troncon_hydro_radius_m: float
    surface_hydro_mode: HydroMode
    surface_hydro_radius_m: float
    faune_criteria: list[dict]

    @staticmethod
    def defaut() -> FiltreOptions:
        return FiltreOptions(
            vegetation_hybride={
                "zdv_natures": [],
                "cesbio_libelles": [],
                "mode": "OR",
            },
            carhab_nom_eunis=[],
            excluded_layers=list(DEFAULT_EXCLUDED_LAYERS),
            ebc_mode="ignore",
            natura2000_mode="exclude",
            reserves_naturelles_mode="ignore",
            znieff_mode="ignore",
            arrachage_vignes_mode="ignore",
            zone_humide_mode="ignore",
            remontee_nappes_classefiab=[],
            troncon_hydro_mode="ignore",
            troncon_hydro_radius_m=500.0,
            surface_hydro_mode="ignore",
            surface_hydro_radius_m=500.0,
            faune_criteria=[],
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> FiltreOptions:
        """Reconstruit les options depuis `projects.last_filter` (filter_v2 ou legacy)."""
        opts = cls.defaut()
        if not raw:
            return opts

        vh = raw.get("vegetation_hybride") if isinstance(raw.get("vegetation_hybride"), dict) else {}
        cesbio = _str_list(vh.get("cesbio_libelles") or raw.get("cesbio_libelles"))
        zdv = _str_list(vh.get("zdv_natures"))
        mode = str(vh.get("mode") or "OR").upper()
        if mode not in ("OR", "AND"):
            mode = "OR"
        opts.vegetation_hybride = {
            "zdv_natures": zdv,
            "cesbio_libelles": cesbio,
            "mode": mode,
        }
        opts.carhab_nom_eunis = _str_list(raw.get("carhab_nom_eunis"))
        excl = _str_list(raw.get("excluded_layers"))
        if excl:
            opts.excluded_layers = excl
        opts.ebc_mode = raw.get("ebc_mode", opts.ebc_mode) or "ignore"
        opts.natura2000_mode = raw.get("natura2000_mode", opts.natura2000_mode) or "exclude"
        opts.reserves_naturelles_mode = (
            raw.get("reserves_naturelles_mode", opts.reserves_naturelles_mode) or "ignore"
        )
        opts.znieff_mode = raw.get("znieff_mode", opts.znieff_mode) or "ignore"
        opts.arrachage_vignes_mode = (
            raw.get("arrachage_vignes_mode", opts.arrachage_vignes_mode) or "ignore"
        )
        opts.zone_humide_mode = raw.get("zone_humide_mode", opts.zone_humide_mode) or "ignore"
        opts.remontee_nappes_classefiab = _str_list(raw.get("remontee_nappes_classefiab"))

        is_v2 = raw.get("pipeline") == "filter_v2" or "troncons_hydros_max_dist_m" in raw
        if is_v2:
            tm = raw.get("troncons_hydros_max_dist_m")
            if tm is not None:
                opts.troncon_hydro_mode = "within_radius"
                opts.troncon_hydro_radius_m = float(tm)
            else:
                opts.troncon_hydro_mode = "ignore"
            sm = raw.get("surfaces_hydros_max_dist_m")
            if sm is not None:
                opts.surface_hydro_mode = "within_radius"
                opts.surface_hydro_radius_m = float(sm)
            else:
                opts.surface_hydro_mode = "ignore"
        else:
            opts.troncon_hydro_mode = raw.get("troncon_hydro_mode", opts.troncon_hydro_mode) or "ignore"
            opts.troncon_hydro_radius_m = float(raw.get("troncon_hydro_radius_m", opts.troncon_hydro_radius_m))
            opts.surface_hydro_mode = raw.get("surface_hydro_mode", opts.surface_hydro_mode) or "ignore"
            opts.surface_hydro_radius_m = float(raw.get("surface_hydro_radius_m", opts.surface_hydro_radius_m))

        faune = raw.get("fauna_criteria") or raw.get("faune_criteria") or []
        parsed: list[dict] = []
        if isinstance(faune, list):
            for c in faune:
                if not isinstance(c, dict):
                    continue
                name = str(c.get("tax_nom_val") or c.get("species") or "").strip()
                if not name:
                    continue
                parsed.append(
                    {
                        "tax_nom_val": name,
                        "species": name,
                        "mode": c.get("mode", "within_radius"),
                        "radius_m": float(c.get("radius_m", c.get("dist_m", 500.0))),
                        "dist_m": float(c.get("dist_m", c.get("radius_m", 500.0))),
                        "sources": [
                            s
                            for s in (c.get("sources") or ["pct", "lin", "surf"])
                            if s in ("pct", "lin", "surf")
                        ]
                        or ["pct", "lin", "surf"],
                    }
                )
        opts.faune_criteria = parsed
        return opts

    @property
    def zdv_natures(self) -> list[str]:
        return list(self.vegetation_hybride.get("zdv_natures", []))

    @property
    def cesbio_libelles(self) -> list[str]:
        return list(self.vegetation_hybride.get("cesbio_libelles", []))

    @property
    def vegetation_hybride_mode(self) -> str:
        return str(self.vegetation_hybride.get("mode", "OR")).upper()
