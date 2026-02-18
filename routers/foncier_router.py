from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse
from pathlib import Path
import tempfile
import shutil
import zipfile
import geopandas as gpd
from sqlalchemy import text
from db import get_engine

router = APIRouter(prefix="/api/foncier", tags=["foncier"])

engine = get_engine()


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


# -------------------------------------------------------
# Endpoint principal
# -------------------------------------------------------

@router.post("/import", status_code=201)
async def import_foncier(
    name: str = Form(...),
    buffer_km: float = Form(12.0),
    file: UploadFile = File(...)
):
    """
    1) importe le foncier
    2) crée AOI
    3) crée analyse (project)
    """

    gdf = _load_geodataframe_from_upload(file)

    # ----------------------------
    # Géométrie de base (foncier)
    # ----------------------------
    base_geom = gdf.union_all()
    area_ha = base_geom.area / 10000.0

    buffer_m = int(buffer_km * 1000)
    aoi_geom = base_geom.buffer(buffer_m, resolution=32)

    with engine.begin() as conn:

        # 1) FONCIER
        foncier_row = conn.execute(text("""
            INSERT INTO ecocompensation.foncier (name, geom_2154, area_ha)
            VALUES (
                :name,
                ST_GeomFromText(:geom, 2154),
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
            "geom": aoi_geom.wkt
        }).mappings().one()

        aoi_id = str(aoi_row["id"])

        # 3) ANALYSE (table projects existante)
        project_row = conn.execute(text("""
            INSERT INTO ecocompensation.projects (name, aoi_id, foncier_id, status)
            VALUES (
                :name,
                :aoi,
                :foncier,
                'created'
            )
            RETURNING id
        """), {
            "name": name,
            "aoi": aoi_id,
            "foncier": foncier_id
        }).mappings().one()

        project_id = str(project_row["id"])

    return {
        "foncier_id": foncier_id,
        "aoi_id": aoi_id,
        "project_id": project_id,
        "area_ha": round(area_ha, 2),
        "buffer_km": buffer_km
    }


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
