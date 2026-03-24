#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
import textwrap

import geopandas as gpd
import requests

WFS_URL = "https://data.geopf.fr/wfs/ows"

def test_parcelle(code_insee: str, section: str, numero: str) -> None:
  cql = f"code_insee='{code_insee}' AND section='{section}' AND numero='{numero}'"
  params = {
    "service": "WFS",
    "version": "2.0.0",
    "request": "GetFeature",
    "typeNames": "CADASTRALPARCELS.PARCELLAIRE_EXPRESS:parcelle",
    "srsName": "EPSG:2154",
    "outputFormat": "application/json",
    "CQL_FILTER": cql,
  }

  print("URL :", WFS_URL)
  print("Params :", params)
  resp = requests.get(WFS_URL, params=params, timeout=30)
  print("Status code :", resp.status_code)
  if not resp.ok:
    print("Body (début):")
    print(textwrap.shorten(resp.text, width=800))
    return

  print("OK, réponse JSON reçue, tentative de lecture avec GeoPandas…")
  gdf = gpd.read_file(resp.text)
  print("CRS :", gdf.crs)
  print("Nb d’entités :", len(gdf))
  if not gdf.empty:
    print(gdf.head())

if __name__ == "__main__":
  if len(sys.argv) != 4:
    print("Usage: python test_parcellaire.py CODE_INSEE SECTION NUMERO")
    sys.exit(1)
  test_parcelle(sys.argv[1], sys.argv[2], sys.argv[3])