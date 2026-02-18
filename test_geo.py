import geopandas as gpd
from shapely.geometry import Point
import pyproj

print("---- GEOS ----")
p = Point(2.35, 48.85)
print(p.buffer(0.01).area)

print("---- PROJ ----")
crs = pyproj.CRS.from_epsg(2154)
print(crs)

print("---- GEOPANDAS ----")
gdf = gpd.GeoDataFrame(geometry=[p], crs="EPSG:4326")
gdf2 = gdf.to_crs(2154)
print(gdf2.geometry.iloc[0])
