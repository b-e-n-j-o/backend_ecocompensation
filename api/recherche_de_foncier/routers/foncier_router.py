from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from pathlib import Path
import tempfile
import shutil
import zipfile
import geopandas as gpd
from shapely import wkt as shapely_wkt
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union
from sqlalchemy import text
from sqlalchemy.engine import Connection
from db import get_engine

router = APIRouter(tags=["foncier"])

engine = get_engine()

BV_TABLE = "ecocompensation.bv_spe_masse_d_eau"


# -------------------------------------------------------
# Helpers
# -------------------------------------------------------

def _load_geodataframe_from_upload(file: UploadFile) -> gpd.GeoDataFrame:
    """
    Supporte :
      - .gpkg
      - .zip contenant un shapefile
    """

    suffix = Path(file.filename).suffix.lower()

    tmpdir = tempfile.mkdtemp()

    filepath = Path(tmpdir) / file.filename

    with open(filepath, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # --- GPKG ---
    if suffix == ".gpkg":
        gdf = gpd.read_file(filepath)

    # --- ZIP SHAPEFILE ---
    elif suffix == ".zip":
        with zipfile.ZipFile(filepath, "r") as z:
            z.extractall(tmpdir)

        shp_files = list(Path(tmpdir).glob("*.shp"))
        if not shp_files:
            raise HTTPException(400, "Le zip ne contient pas de shapefile (.shp)")

        gdf = gpd.read_file(shp_files[0])

    else:
        raise HTTPException(
            400,
            "Format non supporté. Fournir un .gpkg ou un shapefile compressé (.zip)."
        )

    if gdf.empty:
        raise HTTPException(400, "Le fichier ne contient aucune géométrie.")

    # reprojection
    if gdf.crs is None:
        raise HTTPException(400, "CRS inconnu dans le fichier fourni.")

    if gdf.crs.to_string() != "EPSG:2154":
        gdf = gdf.to_crs("EPSG:2154")

    return gdf


def _to_multipolygon(geom):
    if geom is None or geom.is_empty:
        return geom
    if isinstance(geom, Polygon):
        return MultiPolygon([geom])
    return geom


def _resolve_zh_aoi_from_upload(conn: Connection, base_geom_wkt: str) -> dict:
    """
    Sélectionne les bassins versants entiers qui intersectent la zone uploadée,
    puis fusionne leurs géométries côté Python (évite ST_Union lourd sur Supabase).
    """
    rows = conn.execute(
        text(f"""
            WITH upload AS (
                SELECT ST_GeomFromText(:geom, 2154) AS geom
            )
            SELECT b.nom_bv_spe_md, ST_AsText(b.geom_2154) AS geom_wkt
            FROM {BV_TABLE} b
            CROSS JOIN upload u
            WHERE b.geom_2154 IS NOT NULL
              AND b.geom_2154 && u.geom
              AND ST_Intersects(b.geom_2154, u.geom)
        """),
        {"geom": base_geom_wkt},
    ).mappings().all()

    if not rows:
        raise HTTPException(
            400,
            "Aucun bassin versant (masse d'eau) n'intersecte la zone uploadée. "
            "Vérifiez l'emprise ou la couche ecocompensation.bv_spe_masse_d_eau.",
        )

    geoms = [shapely_wkt.loads(str(r["geom_wkt"])) for r in rows if r.get("geom_wkt")]
    union_geom = _to_multipolygon(unary_union(geoms))
    bv_names = sorted({
        str(r["nom_bv_spe_md"]).strip()
        for r in rows
        if r.get("nom_bv_spe_md") and str(r["nom_bv_spe_md"]).strip()
    })

    return {
        "bv_count": len(rows),
        "geom_wkt": union_geom.wkt,
        "area_ha": float(union_geom.area) / 10000.0,
        "bv_names": bv_names,
    }


def _geom_wkt_to_geojson_4326(conn: Connection, geom_wkt: str) -> dict:
    row = conn.execute(
        text("""
            SELECT ST_AsGeoJSON(ST_Transform(ST_GeomFromText(:geom, 2154), 4326))::json AS geometry
        """),
        {"geom": geom_wkt},
    ).mappings().one()
    return dict(row["geometry"])


# -------------------------------------------------------
# Endpoint principal
# -------------------------------------------------------

@router.post("/preview")
async def preview_foncier(
    file: UploadFile = File(...),
    study_type: str = Form("faune_buffer"),
):
    """
    Calcule l'emprise d'un upload (ZIP SHP/GPKG) et retourne la géométrie
    en GeoJSON (EPSG:4326) pour prévisualisation.

    zones_humides_intra : union des BV entiers intersectant l'upload
    (pas la découpe ST_Intersection).
    """
    if study_type not in ("faune_buffer", "zones_humides_intra"):
        raise HTTPException(400, "study_type invalide (faune_buffer | zones_humides_intra)")

    gdf = _load_geodataframe_from_upload(file)
    base_geom = gdf.union_all()
    upload_area_ha = float(base_geom.area / 10000.0)

    if study_type == "zones_humides_intra":
        base_geom_4326 = gpd.GeoSeries([base_geom], crs="EPSG:2154").to_crs("EPSG:4326").iloc[0]
        with engine.connect() as conn:
            bv = _resolve_zh_aoi_from_upload(conn, base_geom.wkt)
            geometry = _geom_wkt_to_geojson_4326(conn, bv["geom_wkt"])
        return {
            "area_ha": round(float(bv["area_ha"]), 4),
            "upload_area_ha": round(upload_area_ha, 4),
            "bv_count": bv["bv_count"],
            "bv_names": bv["bv_names"],
            "study_type": study_type,
            "feature": {
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "bv_count": bv["bv_count"],
                    "bv_names": bv["bv_names"],
                },
            },
            "upload_feature": {
                "type": "Feature",
                "geometry": base_geom_4326.__geo_interface__,
                "properties": {},
            },
        }

    base_geom_4326 = gpd.GeoSeries([base_geom], crs="EPSG:2154").to_crs("EPSG:4326").iloc[0]
    return {
        "area_ha": round(upload_area_ha, 4),
        "study_type": study_type,
        "feature": {
            "type": "Feature",
            "geometry": base_geom_4326.__geo_interface__,
            "properties": {},
        },
    }

@router.post("/import", status_code=201)
async def import_foncier(
    name: str = Form(...),
    buffer_km: float = Form(12.0),
    study_type: str = Form("faune_buffer"),
    file: UploadFile = File(...)
):
    """
    1) importe le foncier
    2) crée AOI
    3) crée analyse (project)
    """
    if study_type not in ("faune_buffer", "zones_humides_intra"):
        raise HTTPException(400, "study_type invalide (faune_buffer | zones_humides_intra)")

    gdf = _load_geodataframe_from_upload(file)

    # ----------------------------
    # Géométrie de base (foncier)
    # ----------------------------
    base_geom = gdf.union_all()
    area_ha = base_geom.area / 10000.0

    bv_count = None
    bv_names = None
    aoi_area_ha = None
    aoi_geom_wkt = None

    if study_type == "zones_humides_intra":
        buffer_m = 0
        with engine.connect() as conn:
            bv = _resolve_zh_aoi_from_upload(conn, base_geom.wkt)
        bv_count = bv["bv_count"]
        bv_names = bv["bv_names"]
        aoi_area_ha = float(bv["area_ha"])
        aoi_geom_wkt = bv["geom_wkt"]
    else:
        buffer_m = int(buffer_km * 1000)
        aoi_geom_wkt = base_geom.buffer(buffer_m, resolution=32).wkt

    with engine.begin() as conn:
        # 1) FONCIER
        foncier_row = conn.execute(text("""
            INSERT INTO ecocompensation.foncier (name, geom_2154, area_ha)
            VALUES (
                :name,
                ST_Multi(ST_GeomFromText(:geom, 2154)),
                :area
            )
            RETURNING id
        """), {
            "name": name,
            "geom": base_geom.wkt,
            "area": area_ha
        }).mappings().one()

        foncier_id = str(foncier_row["id"])

        # 2) AOI
        aoi_row = conn.execute(text("""
            INSERT INTO ecocompensation.aoi (code_insee, buffer_m, geom_2154)
            VALUES (
                'USER',
                :buffer,
                ST_GeomFromText(:geom, 2154)
            )
            RETURNING id
        """), {
            "buffer": buffer_m,
            "geom": aoi_geom_wkt
        }).mappings().one()

        aoi_id = str(aoi_row["id"])

        # 3) ANALYSE (table projects existante)
        project_row = conn.execute(text("""
            INSERT INTO ecocompensation.projects (name, aoi_id, foncier_id, status, study_type)
            VALUES (
                :name,
                :aoi,
                :foncier,
                'created',
                :study_type
            )
            RETURNING id
        """), {
            "name": name,
            "aoi": aoi_id,
            "foncier": foncier_id,
            "study_type": study_type,
        }).mappings().one()

        project_id = str(project_row["id"])

    payload = {
        "foncier_id": foncier_id,
        "aoi_id": aoi_id,
        "project_id": project_id,
        "area_ha": round(area_ha, 2),
        "buffer_km": buffer_km if study_type == "faune_buffer" else 0.0,
        "study_type": study_type,
    }
    if study_type == "zones_humides_intra":
        payload["bv_count"] = bv_count
        payload["bv_names"] = bv_names
        payload["aoi_area_ha"] = round(aoi_area_ha, 2) if aoi_area_ha is not None else None
    return payload


@router.get("/{project_id}/geometry")
def get_foncier_geometry(project_id: str):
  """
  Retourne la géométrie du foncier en GeoJSON (EPSG:4326)
  pour affichage MapLibre.
  """

  with engine.begin() as conn:
      row = conn.execute(text("""
          SELECT
              ST_AsGeoJSON(ST_Transform(f.geom_2154, 4326))::json AS geometry
          FROM ecocompensation.projects p
          JOIN ecocompensation.foncier f ON f.id = p.foncier_id
          WHERE p.id = :pid
      """), {"pid": project_id}).mappings().one_or_none()

  if not row:
      raise HTTPException(404, "Foncier introuvable pour ce projet")

  return {
      "type": "FeatureCollection",
      "features": [
          {
              "type": "Feature",
              "geometry": row["geometry"],
              "properties": {}
          }
      ]
  }
