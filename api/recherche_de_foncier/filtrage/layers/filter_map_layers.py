"""Couches de carte alignées sur filter_pipeline / national_exclusions.

Chaque entrée active selon FilterConfig. `sql` sélectionne `g2154` et les propriétés,
déjà restreintes à la table et aux conditions de la clause (pas encore à l'AOI).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import text

from api.recherche_de_foncier.filtrage.layers.filter_pipeline import FilterConfig
from api.recherche_de_foncier.filtrage.layers.modules.espaces_boises_classe import EBC_CONTRAINTE_CLASSES

CAP = 20_000

Popup = list[dict[str, str]]


@dataclass
class FilterMapLayer:
    key: str
    label: str
    groupe: str
    geom_type: str
    color: str
    discriminant: str | None
    popup_fields: Popup
    active: Callable[[FilterConfig], bool]
    sql: Callable[[FilterConfig], tuple[str, dict]]


def _popups(*pairs: tuple[str, str]) -> Popup:
    return [{"field": f, "label": lab} for f, lab in pairs]


def _in_excluded(key: str) -> Callable[[FilterConfig], bool]:
    return lambda c: key in set(c.excluded_layers or [])


def _geomce_sql(_c: FilterConfig) -> tuple[str, dict]:
    parts = []
    for table, kind in (
        ("geomce_surf", "surf"),
        ("geomce_lin", "lin"),
        ("geomce_pct", "pct"),
    ):
        parts.append(f"""
            SELECT g.geom_2154 AS g2154, '{kind}' AS type
            FROM ecocompensation.{table} g
            WHERE g.geom_2154 IS NOT NULL AND g.deleted_at IS NULL
        """)
    return " UNION ALL ".join(parts), {}


def _national(table: str, cols: str, where: str = "TRUE") -> Callable[[FilterConfig], tuple[str, dict]]:
    def _sql(_c: FilterConfig) -> tuple[str, dict]:
        return f"""
            SELECT g.geom_2154 AS g2154, {cols}
            FROM {table} g
            WHERE g.geom_2154 IS NOT NULL AND ({where})
        """, {}
    return _sql


def _znieff(type_code: str) -> Callable[[FilterConfig], tuple[str, dict]]:
    def _sql(_c: FilterConfig) -> tuple[str, dict]:
        return """
            SELECT z.geom_2154 AS g2154, z.type_znieff
            FROM ecocompensation.znieff z
            WHERE z.geom_2154 IS NOT NULL AND z.type_znieff = :ztype
        """, {"ztype": type_code}
    return _sql


def _ebc_sql(_c: FilterConfig) -> tuple[str, dict]:
    return """
        SELECT e.geom_2154 AS g2154, e.classe AS classe
        FROM ecocompensation.v_ebc e
        WHERE e.geom_2154 IS NOT NULL AND e.classe = ANY(:ebc_classes)
    """, {"ebc_classes": list(EBC_CONTRAINTE_CLASSES)}


def _zh_group(mode: str) -> str:
    return "exclusion" if mode == "exclude" else "critere"


def _cesbio_sql(c: FilterConfig) -> tuple[str, dict]:
    return """
        SELECT v.geom AS g2154, v.libelle_prio, v.libelle, v.nature, v.source
        FROM ecocompensation.vegetation_sur_cesbio v
        WHERE v.geom IS NOT NULL AND v.libelle_prio = ANY(:cesbio_libelles)
    """, {"cesbio_libelles": list(c.cesbio_libelles or [])}


def _flora_sql(buffer: bool) -> Callable[[FilterConfig], tuple[str, dict]]:
    def _sql(c: FilterConfig) -> tuple[str, dict]:
        geom = "ST_Buffer(o.geom, :flora_dist)" if buffer else "o.geom"
        dist = max((fc.dist_m for fc in c.flora_criteria), default=0)
        return f"""
            SELECT {geom} AS g2154, o.taxon_key, o.taxon_key AS nom
            FROM ecocompensation.flore_observation o
            WHERE o.geom IS NOT NULL AND o.taxon_key = ANY(:flora_keys)
        """, {"flora_keys": [fc.taxon_key for fc in c.flora_criteria], "flora_dist": dist}
    return _sql


def _arrachage_sql(c: FilterConfig) -> tuple[str, dict]:
    extra = ""
    params: dict = {}
    if c.arrachage_vignes_types:
        extra = " AND btrim(a.c_div_re_1) = ANY(:arrachage_vignes_types)"
        params["arrachage_vignes_types"] = list(c.arrachage_vignes_types)
    return f"""
        SELECT a.geom_2154 AS g2154, a.c_div_re_1 AS type
        FROM ecocompensation.arrachage_vignes a
        WHERE a.geom_2154 IS NOT NULL {extra}
    """, params


def _bassin_sql(_c: FilterConfig) -> tuple[str, dict]:
    return """
        SELECT b.geom_2154 AS g2154, b.nom_bv_spe_md AS nom
        FROM ecocompensation.bv_spe_masse_d_eau b
        WHERE b.geom_2154 IS NOT NULL
          AND EXISTS (
            SELECT 1 FROM (
                SELECT COALESCE(
                    (SELECT f.geom_2154 FROM ecocompensation.projects pr
                     JOIN ecocompensation.foncier f ON f.id = pr.foncier_id
                     WHERE pr.id = CAST(:pid AS uuid) LIMIT 1),
                    (SELECT ST_Union(pp.geom_2154) FROM ecocompensation.project_parcelles pp
                     WHERE pp.project_id = CAST(:pid AS uuid))
                ) AS geom
            ) src
            WHERE src.geom IS NOT NULL AND b.geom_2154 && src.geom AND ST_Intersects(b.geom_2154, src.geom)
          )
    """, {}


def _rejets_sql(_c: FilterConfig) -> tuple[str, dict]:
    return """
        SELECT p.geom_2154 AS g2154, r.step_key, r.step_label
        FROM ecocompensation_results.parcelles_filtre_rejets r
        JOIN ecocompensation.parcelles p ON p.idu = r.idu
        WHERE r.project_id = CAST(:pid AS uuid)
          AND p.geom_2154 IS NOT NULL
          AND r.created_at >= COALESCE((
              SELECT MAX(created_at) FROM ecocompensation_results.parcelles_filtre_rejets
              WHERE project_id = CAST(:pid AS uuid)
          ), '-infinity'::timestamptz) - interval '1 second'
    """, {}


def _emprise_sql(_c: FilterConfig) -> tuple[str, dict]:
    return """
        SELECT COALESCE(
            (SELECT f.geom_2154 FROM ecocompensation.projects pr
             JOIN ecocompensation.foncier f ON f.id = pr.foncier_id
             WHERE pr.id = CAST(:pid AS uuid) LIMIT 1),
            (SELECT ST_Union(pp.geom_2154) FROM ecocompensation.project_parcelles pp
             WHERE pp.project_id = CAST(:pid AS uuid))
        ) AS g2154, 'emprise' AS type
    """, {}


FILTER_MAP_LAYERS: list[FilterMapLayer] = [
    FilterMapLayer("emprise", "Emprise du projet", "exclusion", "polygon", "#ff4fa3", None,
                   _popups(("type", "Type")), lambda c: c.search_mode != "within_foncier", _emprise_sql),
    FilterMapLayer("geomce", "Mesures compensatoires (GEOMCE)", "exclusion", "polygon", "#7c3aed", "type",
                   _popups(("type", "Type")), _in_excluded("geomce"), _geomce_sql),
    FilterMapLayer("preemption_ens", "Préemption ENS", "exclusion", "polygon", "#fbbf24", "nom_zpens",
                   _popups(("nom_zpens", "Zone"), ("commune", "Commune")), _in_excluded("preemption_ens"),
                   _national("ecocompensation.preemption_espaces_naturels_sensibles", "g.nom_zpens, g.commune, g.texte")),
    FilterMapLayer("ens", "Espaces naturels sensibles", "exclusion", "polygon", "#4ade80", "nom_site",
                   _popups(("nom_site", "Site"), ("commune", "Commune")), _in_excluded("ens"),
                   _national("ecocompensation.espaces_naturels_sensibles_ens", "g.nom_site, g.commune, g.texte")),
    FilterMapLayer("natura_2000", "Natura 2000", "exclusion", "polygon", "#a78bfa", None,
                   _popups(("nom", "Nom")), _in_excluded("natura_2000"),
                   _national("ecocompensation.natura_2000", "'Natura 2000' AS nom")),
    FilterMapLayer("znieff_1", "ZNIEFF type 1", "exclusion", "polygon", "#f472b6", None,
                   _popups(("nom", "Nom"), ("type_znieff", "Type")), _in_excluded("znieff_1"), _znieff("1")),
    FilterMapLayer("znieff_2", "ZNIEFF type 2", "exclusion", "polygon", "#fb7185", None,
                   _popups(("nom", "Nom"), ("type_znieff", "Type")), _in_excluded("znieff_2"), _znieff("2")),
    FilterMapLayer("ebc", "Espaces boisés classés", "exclusion", "polygon", "#166534", "classe",
                   _popups(("classe", "Classe")), _in_excluded("ebc"), _ebc_sql),
    FilterMapLayer("cesbio", "Végétation CESBIO", "critere", "polygon", "#86efac", "libelle_prio",
                   _popups(("libelle_prio", "Libellé"), ("source", "Source")),
                   lambda c: bool(c.cesbio_libelles), _cesbio_sql),
    FilterMapLayer("fauna", "Observations faune", "critere", "point", "#f97316", "nom_vernaculaire",
                   _popups(("nom_vernaculaire", "Espèce")), lambda c: bool(c.fauna_criteria), lambda c: ("SELECT NULL::geometry AS g2154 WHERE FALSE", {})),
    FilterMapLayer("fauna_buffer", "Buffers faune", "critere", "polygon", "#f472b6", "nom_vernaculaire",
                   _popups(("nom_vernaculaire", "Espèce")), lambda c: bool(c.fauna_criteria), lambda c: ("SELECT NULL::geometry AS g2154 WHERE FALSE", {})),
    FilterMapLayer("flore", "Observations flore", "critere", "point", "#84cc16", "nom",
                   _popups(("nom", "Nom"), ("taxon_key", "Taxon")), lambda c: bool(c.flora_criteria), _flora_sql(False)),
    FilterMapLayer("flore_buffer", "Buffers flore", "critere", "polygon", "#a3e635", "nom",
                   _popups(("nom", "Nom"), ("taxon_key", "Taxon")), lambda c: bool(c.flora_criteria), _flora_sql(True)),
    FilterMapLayer("zone_humide", "Zones humides établies", "critere", "polygon", "#38bdf8", None,
                   _popups(("libelle", "Libellé")), lambda c: c.zone_humide_mode in ("intersect", "exclude"),
                   lambda c: ("SELECT NULL::geometry AS g2154 WHERE FALSE", {})),
    FilterMapLayer("zones_humides_probables", "Zones humides probables", "critere", "polygon", "#67e8f9", "value",
                   _popups(("value", "Probabilité")), lambda c: c.zones_humides_probables_mode in ("intersect", "exclude"),
                   lambda c: ("SELECT NULL::geometry AS g2154 WHERE FALSE", {})),
    FilterMapLayer("troncons_hydros", "Cours d'eau", "critere", "line", "#2563eb", "nature",
                   _popups(("nom", "Nom"), ("nature", "Nature")),
                   lambda c: c.troncons_hydros_max_dist_m is not None, lambda c: ("SELECT NULL::geometry AS g2154 WHERE FALSE", {})),
    FilterMapLayer("surfaces_hydros", "Surfaces hydrographiques", "critere", "polygon", "#7dd3fc", "nature",
                   _popups(("nom", "Nom"), ("nature", "Nature")),
                   lambda c: c.surfaces_hydros_max_dist_m is not None, lambda c: ("SELECT NULL::geometry AS g2154 WHERE FALSE", {})),
    FilterMapLayer("arrachage_vignes", "Arrachage de vignes", "critere", "polygon", "#b45309", "type",
                   _popups(("type", "Type")), lambda c: bool(c.arrachage_vignes), _arrachage_sql),
    FilterMapLayer("bassin_versant", "Bassins versants du projet", "critere", "polygon", "#0ea5e9", "nom",
                   _popups(("nom", "Masse d'eau")), lambda c: bool(c.same_bassin_versant), _bassin_sql),
    FilterMapLayer("parcelles_ecartees", "Parcelles écartées", "contexte", "polygon", "#dc2626", "step_label",
                   _popups(("step_key", "Étape"), ("step_label", "Libellé")), lambda c: True, _rejets_sql),
]

FILTER_MAP_BY_KEY = {layer.key: layer for layer in FILTER_MAP_LAYERS}

# Ces clés restent servies par le chemin historique (map_layers ou tables projet).
LEGACY_GEOJSON_KEYS = frozenset({
    "cesbio", "fauna", "fauna_buffer",
    "zone_humide", "zones_humides_probables",
    "troncons_hydros", "surfaces_hydros",
})


def active_layer_defs(config: FilterConfig) -> list[dict[str, Any]]:
    out = []
    for layer in FILTER_MAP_LAYERS:
        if not layer.active(config):
            continue
        groupe = layer.groupe
        if layer.key == "zone_humide":
            groupe = _zh_group(config.zone_humide_mode)
        elif layer.key == "zones_humides_probables":
            groupe = _zh_group(config.zones_humides_probables_mode)
        out.append({
            "key": layer.key,
            "label": layer.label,
            "groupe": groupe,
            "color": layer.color,
            "discriminant": layer.discriminant,
            "popup_fields": layer.popup_fields,
            "geom_type": layer.geom_type,
        })
    return out


def fetch_filter_layer(conn, project_id: str, key: str, config: FilterConfig) -> dict:
    layer = FILTER_MAP_BY_KEY[key]
    inner, params = layer.sql(config)
    params = {**params, "pid": project_id, "project_id": project_id}
    clip = "ST_ClipByBox2D(s.g2154, aoi.box)" if layer.geom_type == "polygon" else "s.g2154"
    rows = conn.execute(text(f"""
        WITH aoi AS (
            SELECT a.geom_2154 AS geom, ST_Envelope(a.geom_2154) AS box
            FROM ecocompensation.projects p
            JOIN ecocompensation.aoi a ON a.id = p.aoi_id
            WHERE p.id = CAST(:pid AS uuid)
        )
        SELECT s.*,
               ST_AsGeoJSON(
                   ST_Transform(ST_SimplifyPreserveTopology({clip}, 1), 4326),
                   6
               ) AS geometry
        FROM ({inner}) s
        CROSS JOIN aoi
        WHERE s.g2154 IS NOT NULL
          AND s.g2154 && aoi.box
          AND ST_Intersects(s.g2154, aoi.geom)
        LIMIT {CAP + 1}
    """), params).mappings().all()
    truncated = len(rows) > CAP
    features = []
    for row in rows[:CAP]:
        geom = row.get("geometry")
        if not geom:
            continue
        if isinstance(geom, str):
            geom = json.loads(geom)
        props = {}
        for k, v in dict(row).items():
            if k in ("geometry", "g2154"):
                continue
            props[k] = v.isoformat() if hasattr(v, "isoformat") else v
        features.append({"type": "Feature", "geometry": geom, "properties": props})
    return {"type": "FeatureCollection", "features": features, "truncated": truncated}
