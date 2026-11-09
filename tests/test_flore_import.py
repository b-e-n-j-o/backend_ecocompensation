import zipfile
from datetime import date
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, Polygon

from api.reserve_fonciere.ajout_de_foncier.flore.flore_import import (
    classify_new,
    fingerprint,
    map_columns,
    prepare_import,
)


def _mixed(tmp: Path) -> Path:
    """GeoPackage : un shapefile unique ne peut pas mélanger points, lignes et polygones."""
    gdf = gpd.GeoDataFrame(
        {
            "Nom_tax_ci": ["Robinia pseudoacacia", "Quercus robur", "Salix alba", "", "Fagus sylvatica"],
            "Nom_tax_v": ["Robinier faux-acacia", None, None, None, None],
            "Observateu": ["Fanny", "Fanny", "Fanny", "Fanny", "Fanny"],
            "Date_obser": ["18/06/2026", "18/06/2026", "18/06/2026", "pas-une-date", "18/06/2026"],
            "Note_libre": ["hors gabarit", None, None, None, None],
        },
        geometry=[
            Point(395619.25, 6388659.56),
            LineString([(395600, 6388600), (395700, 6388700)]),
            Polygon([(395000, 6388000), (395100, 6388000), (395100, 6388100), (395000, 6388000)]),
            Point(396000, 6389000),
            Point(396100, 6389100),
        ],
        crs=2154,
    )
    path = tmp / "obs.gpkg"
    gdf.to_file(path, driver="GPKG")
    return path


def _zip_point_shp(tmp: Path) -> Path:
    gdf = gpd.GeoDataFrame(
        {"Nom_tax_ci": ["Robinia pseudoacacia"], "Date_obser": ["18/06/2026"]},
        geometry=[Point(395619.25, 6388659.56)],
        crs=2154,
    )
    shp_dir = tmp / "shp"
    shp_dir.mkdir()
    gdf.to_file(shp_dir / "obs.shp")
    zpath = tmp / "obs.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        for p in shp_dir.iterdir():
            zf.write(p, p.name)
    return zpath


def test_mapping_tronque():
    mapped, unknown = map_columns(["Nom_tax_ci", "Observateu", "Note_libre"])
    assert mapped["Nom_tax_ci"] == "nom_taxon_cite"
    assert mapped["Observateu"] == "observateur"
    assert "Note_libre" in unknown


def test_prepare_rejets_et_types(tmp_path: Path):
    report = prepare_import(_mixed(tmp_path))
    types = {row["geom"].geom_type for row in report["_rows"]}
    assert {"Point", "LineString", "Polygon"} <= types
    reasons = " ".join(r["raison"] for r in report["rejets"])
    assert "vide" in reasons
    from api.reserve_fonciere.ajout_de_foncier.flore.flore_import import _parse_date
    with pytest.raises(ValueError, match="date"):
        _parse_date("pas-une-date")
    assert report["colonnes_inconnues"]
    robin = next(r for r in report["_rows"] if r["nom_taxon_cite"].startswith("Robinia"))
    assert robin["nom_taxon_v"] == "Robinier faux-acacia"
    assert robin["date_observation"] == date(2026, 6, 18)


def test_empreinte_stable_et_idempotence(tmp_path: Path):
    report = prepare_import(_mixed(tmp_path))
    row = report["_rows"][0]
    again = fingerprint(row["nom_taxon_cite"], row["date_observation"], row["observateur"], row["geom"])
    assert again == row["empreinte"]
    fresh, dups = classify_new(report["_rows"], set())
    assert dups == 0 and fresh
    fresh2, dups2 = classify_new(report["_rows"], {r["empreinte"] for r in fresh})
    assert fresh2 == []
    assert dups2 == len(report["_rows"])


def test_zip_shp_lu(tmp_path: Path):
    report = prepare_import(_zip_point_shp(tmp_path))
    assert report["nb_valides"] == 1
    assert report["crs_detecte"]


def test_fichier_sans_nom_rejete(tmp_path: Path):
    gdf = gpd.GeoDataFrame({"Observateu": ["A"]}, geometry=[Point(400000, 6400000)], crs=2154)
    gdf.to_file(tmp_path / "bad.geojson")
    with pytest.raises(ValueError, match="Nom_tax_ci"):
        prepare_import(tmp_path / "bad.geojson")
