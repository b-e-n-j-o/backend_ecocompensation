"""
Registre des couches internes exposées au Web SIG.

Ajouter une couche = une entrée dans LAYERS (table déjà en base).
Consignes d'affichage (GeoJSON / MVT / MBTiles) : README.md dans ce dossier.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path


MBTILES_DIR = Path(__file__).resolve().parent / "mbtiles"

FAM_FONCIER = "reserves_foncieres"
FAM_DONNEES = "couches_donnees"
FAM_GEOMCE = "geomce"
FAMILY_LABELS: dict[str, str] = {
    FAM_FONCIER: "Réserves foncières",
    FAM_DONNEES: "Couches de données",
    FAM_GEOMCE: "GEOMCE aout 2026",
}

# Emprise métier KERELIA (Sud-Ouest élargi), EPSG:4326 west,south,east,north.
BBOX_SUD_OUEST: tuple[float, float, float, float] = (
    -2.768555,
    42.277309,
    3.515625,
    47.487513,
)

# Gironde — emprise réelle vegetation_sur_cesbio (EPSG:4326).
BBOX_VEGETATION_CESBIO: tuple[float, float, float, float] = (
    -1.363183,
    44.048426,
    0.426341,
    45.659873,
)

# Adour-Garonne — emprise réelle zone_humide / RPDZH (EPSG:4326).
BBOX_ZONE_HUMIDE: tuple[float, float, float, float] = (
    -2.065798,
    42.486592,
    3.913792,
    46.606526,
)

# France métropolitaine (hexagone + Corse). Pas de DOM-TOM (emprise / recadrage).
BBOX_FRANCE_METRO: tuple[float, float, float, float] = (
    -5.2,
    41.33,
    9.66,
    51.13,
)

# GEOMCE — mesures actives (pas de deleted). Légende par famille ERC (`classe`).
GEOMCE_WHERE = "t.deleted_at IS NULL"
GEOMCE_PROPS: tuple[str, ...] = (
    "identifiant",
    "classe",
    "type",
    "categorie",
    "sous_categorie",
    "projet",
    "maitre_ouvrage",
    "dossier_no",
    "l_dep",
    "liste_communes",
)
GEOMCE_CLASS_COLORS: dict[str, str] = {
    "E - Évitement": "#16a34a",
    "R - Réduction": "#ea580c",
    "C - Compensation": "#6d28d9",
    "A - Accompagnement": "#0284c7",
    "Z - Classe de mesure à préciser": "#78716c",
}
GEOMCE_CLASS_LABELS: dict[str, str] = {
    "E - Évitement": "E · Évitement",
    "R - Réduction": "R · Réduction",
    "C - Compensation": "C · Compensation",
    "A - Accompagnement": "A · Accompagnement",
    "Z - Classe de mesure à préciser": "Z · À préciser",
}
GEOMCE_ORDER_SQL = (
    "CASE t.classe "
    "WHEN 'C - Compensation' THEN 4 "
    "WHEN 'E - Évitement' THEN 3 "
    "WHEN 'R - Réduction' THEN 2 "
    "WHEN 'A - Accompagnement' THEN 1 "
    "ELSE 0 END"
)

# RPDZH : zhe = zones humides effectives (dessus), zh_total = inventaire compilé.
ZH_CLASS_COLORS: dict[str, str] = {
    "zhe": "#0369a1",
    "zh_total": "#22d3ee",
}
ZH_CLASS_LABELS: dict[str, str] = {
    "zhe": "Zones humides effectives",
    "zh_total": "Inventaire total",
}

# Couleurs occupation du sol (CESBIO + végétation BD TOPO, une seule légende).
VEG_CLASS_COLORS: dict[str, str] = {
    "Forêts de conifères": "#1b5e20",
    "Forêt fermée de conifères": "#1b5e20",
    "Forêts de feuillus": "#2e7d32",
    "Forêt fermée de feuillus": "#2e7d32",
    "Forêt fermée mixte": "#33691e",
    "Forêt ouverte": "#558b2f",
    "Bois": "#388e3c",
    "Peupleraie": "#7cb342",
    "Haie": "#6d4c41",
    "Landes ligneuses": "#827717",
    "Lande ligneuse": "#827717",
    "Lande herbacée": "#9e9d24",
    "Prairies": "#9ccc65",
    "Pelouses": "#c5e1a5",
    "Vigne": "#8e24aa",
    "Vignes": "#8e24aa",
    "Verger": "#ef6c00",
    "Vergers": "#ef6c00",
    "Maïs": "#f9a825",
    "Tournesol": "#fdd835",
    "Céréales à pailles": "#ffcc80",
    "Colza": "#ffee58",
    "Soja": "#dce775",
    "Protéagineux": "#d4e157",
    "Tubercules/racines": "#ce93d8",
    "Bâtis diffus": "#bcaaa4",
    "Bâtis denses": "#6d4c41",
    "Zones industrielles et commerciales": "#546e7a",
    "Surfaces routes": "#90a4ae",
    "Surfaces minérales": "#b0bec5",
    "Eau": "#1565c0",
    "Plages et dunes": "#ffe082",
    "Autres": "#9e9e9e",
}


@dataclass(frozen=True)
class LayerStyle:
    """Paramètres MapLibre. cluster = halos aux zooms larges (centroïdes, GeoJSON)."""

    cluster: bool = False
    cluster_max_zoom: int = 12
    cluster_radius: int = 56
    geom_min_zoom: float | None = None


GEOMCE_CLUSTER = LayerStyle(
    cluster=True,
    cluster_max_zoom=11,
    cluster_radius=72,
    geom_min_zoom=12,
)


@dataclass(frozen=True)
class InternalLayer:
    key: str
    label: str
    schema: str
    table: str
    geometry_type: str  # polygon | line | point
    color: str
    geom_2154: str = "geom_2154"
    geom_3857: str = "geom_3857"
    id_column: str = "id"
    properties: tuple[str, ...] = field(default_factory=tuple)
    default_visible: bool = True
    style: LayerStyle = field(default_factory=LayerStyle)
    delivery: str = "geojson"
    min_zoom: int | None = None
    max_zoom: int | None = None
    compute_bounds: bool = True
    where_sql: str | None = None
    feature_id_sql: str | None = None
    # ORDER BY dans la tuile : végétation BD TOPO au-dessus du CESBIO s'il reste un chevauchement.
    mvt_order_sql: str | None = None
    color_property: str | None = None
    class_colors: dict[str, str] = field(default_factory=dict)
    class_labels: dict[str, str] = field(default_factory=dict)
    # Filtre dump / MBTiles (4326). None = table entière.
    clip_bbox: tuple[float, float, float, float] | None = None
    bounds_4326: tuple[float, float, float, float] | None = None
    mbtiles_file: str | None = None
    storage_bucket: str | None = None
    storage_object: str | None = None
    family: str = "couches_donnees"

    def public_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "geometry_type": self.geometry_type,
            "color": self.color,
            "default_visible": self.default_visible,
            "style": asdict(self.style),
            "delivery": self.delivery,
            "min_zoom": self.min_zoom,
            "max_zoom": self.max_zoom,
            "color_property": self.color_property,
            "class_colors": self.class_colors,
            "class_labels": self.class_labels,
            "family": self.family,
            "family_label": FAMILY_LABELS.get(self.family, self.family),
        }

    def mbtiles_path(self) -> Path | None:
        if not self.mbtiles_file:
            return None
        return MBTILES_DIR / self.mbtiles_file


LAYERS: tuple[InternalLayer, ...] = (
    InternalLayer(
        key="parcelles_syndicates",
        label="Parcelles syndicats",
        schema="ecocompensation",
        table="parcelles_syndicates",
        geometry_type="polygon",
        color="#2563eb",
        family=FAM_FONCIER,
        default_visible=True,
        style=LayerStyle(
            cluster=True,
            cluster_max_zoom=13,
            cluster_radius=70,
            geom_min_zoom=14,
        ),
        properties=(
            "idu",
            "geo_parcel",
            "tex",
            "nomcommune",
            "codecommun",
            "adresse",
            "proprietai",
            "propriet_1",
            "surface",
            "surface_ge",
            "contenance",
            "urbain",
            "lot",
            "comptecomm",
            "voie",
            "geo_sectio",
            "code",
        ),
    ),
    InternalLayer(
        key="parcelles_prospects",
        label="Parcelles prospects",
        schema="ecocompensation",
        table="parcelles_prospects_filtered",
        geometry_type="polygon",
        color="#c2410c",
        family=FAM_FONCIER,
        default_visible=False,
        delivery="mbtiles",
        min_zoom=12,
        max_zoom=14,  # tuiles du fichier ; MapLibre sur-zoome au-delà (z15–16+)
        compute_bounds=False,
        clip_bbox=BBOX_SUD_OUEST,
        mbtiles_file="parcelles_prospects_so.mbtiles",
        storage_bucket="ecocompensation",
        storage_object="couches/parcelles-prospects/parcelles_prospects_so.mbtiles",
        feature_id_sql=(
            "('x' || substr(md5(t.siren || '|' || t.idu), 1, 15))::bit(60)::bigint"
        ),
        properties=(
            "siren",
            "denomination",
            "idu",
            "nom_commune",
            "code_insee",
            "section",
            "numero",
            "contenance",
            "nature_culture",
            "est_acteur_public",
            "est_grand_industriel",
            "parcelle_deja_en_mc",
        ),
    ),
    InternalLayer(
        key="vegetation_cesbio",
        label="Végétation / occupation du sol",
        schema="ecocompensation",
        table="vegetation_sur_cesbio",
        geometry_type="polygon",
        color="#2e7d32",
        family=FAM_DONNEES,
        geom_2154="geom",
        default_visible=False,
        delivery="mvt",
        min_zoom=14,
        compute_bounds=False,
        bounds_4326=BBOX_VEGETATION_CESBIO,
        color_property="libelle_prio",
        class_colors=VEG_CLASS_COLORS,
        mvt_order_sql="CASE t.source WHEN 'bdtopo' THEN 1 ELSE 0 END, t.id",
        properties=("libelle_prio", "source", "nature", "libelle"),
    ),
    InternalLayer(
        key="zone_humide",
        label="Zones humides (RPDZH)",
        schema="ecocompensation",
        table="zone_humide",
        geometry_type="polygon",
        color="#0284c7",
        family=FAM_DONNEES,
        default_visible=False,
        delivery="mvt",
        min_zoom=14,
        compute_bounds=False,
        bounds_4326=BBOX_ZONE_HUMIDE,
        color_property="source",
        class_colors=ZH_CLASS_COLORS,
        class_labels=ZH_CLASS_LABELS,
        mvt_order_sql="CASE t.source WHEN 'zhe' THEN 1 ELSE 0 END, t.inventaire_id",
        feature_id_sql=(
            "('x' || substr(replace(t.id::text, '-', ''), 1, 15))::bit(60)::bigint"
        ),
        properties=("source", "inventaire_id", "libelle", "inv_nom"),
    ),
    InternalLayer(
        key="geomce_surf",
        label="GEOMCE surfaces",
        schema="ecocompensation",
        table="geomce_surf",
        geometry_type="polygon",
        color="#6d28d9",
        family=FAM_GEOMCE,
        default_visible=False,
        delivery="mbtiles",
        min_zoom=12,
        max_zoom=16,
        compute_bounds=False,
        where_sql=GEOMCE_WHERE,
        clip_bbox=BBOX_FRANCE_METRO,
        bounds_4326=BBOX_FRANCE_METRO,
        color_property="classe",
        class_colors=GEOMCE_CLASS_COLORS,
        class_labels=GEOMCE_CLASS_LABELS,
        mvt_order_sql=GEOMCE_ORDER_SQL,
        style=GEOMCE_CLUSTER,
        feature_id_sql="t.identifiant",
        mbtiles_file="geomce_surf_fr.mbtiles",
        storage_bucket="ecocompensation",
        storage_object="couches/geomce/geomce_surf_fr.mbtiles",
        properties=GEOMCE_PROPS,
    ),
    InternalLayer(
        key="geomce_lin",
        label="GEOMCE linéaires",
        schema="ecocompensation",
        table="geomce_lin",
        geometry_type="line",
        color="#0f766e",
        family=FAM_GEOMCE,
        default_visible=False,
        delivery="mbtiles",
        min_zoom=12,
        max_zoom=16,
        compute_bounds=False,
        where_sql=GEOMCE_WHERE,
        clip_bbox=BBOX_FRANCE_METRO,
        bounds_4326=BBOX_FRANCE_METRO,
        color_property="classe",
        class_colors=GEOMCE_CLASS_COLORS,
        class_labels=GEOMCE_CLASS_LABELS,
        mvt_order_sql=GEOMCE_ORDER_SQL,
        style=GEOMCE_CLUSTER,
        feature_id_sql="t.identifiant",
        mbtiles_file="geomce_lin_fr.mbtiles",
        storage_bucket="ecocompensation",
        storage_object="couches/geomce/geomce_lin_fr.mbtiles",
        properties=GEOMCE_PROPS,
    ),
    InternalLayer(
        key="geomce_pct",
        label="GEOMCE ponctuelles",
        schema="ecocompensation",
        table="geomce_pct",
        geometry_type="point",
        color="#b45309",
        family=FAM_GEOMCE,
        default_visible=False,
        delivery="mbtiles",
        min_zoom=12,
        max_zoom=16,
        compute_bounds=False,
        where_sql=GEOMCE_WHERE,
        clip_bbox=BBOX_FRANCE_METRO,
        bounds_4326=BBOX_FRANCE_METRO,
        color_property="classe",
        class_colors=GEOMCE_CLASS_COLORS,
        class_labels=GEOMCE_CLASS_LABELS,
        mvt_order_sql=GEOMCE_ORDER_SQL,
        style=GEOMCE_CLUSTER,
        feature_id_sql="t.identifiant",
        mbtiles_file="geomce_pct_fr.mbtiles",
        storage_bucket="ecocompensation",
        storage_object="couches/geomce/geomce_pct_fr.mbtiles",
        properties=GEOMCE_PROPS,
    ),
)

LAYERS_BY_KEY: dict[str, InternalLayer] = {layer.key: layer for layer in LAYERS}


def get_layer(key: str) -> InternalLayer | None:
    return LAYERS_BY_KEY.get(key)
