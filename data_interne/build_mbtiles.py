"""
Dump filtré + MBTiles (tippecanoe) pour une couche interne lourde.

  cd COMPENSATION_ECO/backend
  python -m data_interne.build_mbtiles parcelles_prospects
  python -m data_interne.build_mbtiles geomce_surf --upload

N'écrit que les entités dans clip_bbox (+ where_sql du catalogue).
Zooms : min_zoom–max_zoom de la couche (défaut 12–14).

  python -m data_interne.build_mbtiles parcelles_prospects
  python -m data_interne.build_mbtiles parcelles_prospects --upload
  python -m data_interne.storage_mbtiles upload parcelles_prospects

"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy import text

from db import get_engine

from .catalog import MBTILES_DIR, InternalLayer, get_layer

DEFAULT_MIN_Z = 12
DEFAULT_MAX_Z = 14
TIPPECANOE_LAYER = "default"


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _fqn(layer: InternalLayer) -> str:
    return f"{_ident(layer.schema)}.{_ident(layer.table)}"


def _zoom_range(layer: InternalLayer) -> tuple[int, int]:
    zmin = layer.min_zoom if layer.min_zoom is not None else DEFAULT_MIN_Z
    zmax = layer.max_zoom if layer.max_zoom is not None else DEFAULT_MAX_Z
    return zmin, zmax


def _dump_sql(layer: InternalLayer) -> str:
    fqn = _fqn(layer)
    geom = _ident(layer.geom_2154)
    fid_sql = layer.feature_id_sql or f"t.{_ident(layer.id_column)}"
    props = ", ".join(f"'{c}', t.{_ident(c)}" for c in layer.properties)
    extra = f" AND ({layer.where_sql})" if layer.where_sql else ""
    clip = ""
    if layer.clip_bbox:
        clip = f"""
          AND t.{geom} && ST_Transform(ST_MakeEnvelope(:w, :s, :e, :n, 4326), 2154)
          AND ST_Intersects(
                t.{geom},
                ST_Transform(ST_MakeEnvelope(:w, :s, :e, :n, 4326), 2154)
              )
        """
    order = f"ORDER BY {layer.mvt_order_sql}" if layer.mvt_order_sql else ""
    return f"""
        SELECT json_build_object(
            'type', 'Feature',
            'properties', json_build_object(
                'fid', {fid_sql},
                {props}
            ),
            'geometry', ST_AsGeoJSON(ST_Transform(ST_Force2D(t.{geom}), 4326), 6)::json
        )::text
        FROM {fqn} AS t
        WHERE t.{geom} IS NOT NULL
        {extra}
        {clip}
        {order}
    """


def dump_geojsonseq(layer: InternalLayer, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    params: dict = {}
    if layer.clip_bbox:
        params = {
            "w": layer.clip_bbox[0],
            "s": layer.clip_bbox[1],
            "e": layer.clip_bbox[2],
            "n": layer.clip_bbox[3],
        }
    engine = get_engine()
    n = 0
    t0 = time.perf_counter()
    sql = text(_dump_sql(layer))
    with engine.connect().execution_options(stream_results=True) as conn:
        result = conn.execute(sql, params)
        with dest.open("w", encoding="utf-8") as f:
            for (line,) in result:
                if not line:
                    continue
                f.write(line)
                f.write("\n")
                n += 1
                if n % 20_000 == 0:
                    print(f"  dump {n} entités…", flush=True)
    print(f"Dump {n} entités → {dest} en {time.perf_counter() - t0:.1f}s", flush=True)
    return n


def run_tippecanoe(layer: InternalLayer, geojsons: Path, mbtiles: Path) -> None:
    mbtiles.parent.mkdir(parents=True, exist_ok=True)
    zmin, zmax = _zoom_range(layer)
    cmd = [
        "tippecanoe",
        "--force",
        "-o",
        str(mbtiles),
        "-l",
        TIPPECANOE_LAYER,
        f"-Z{zmin}",
        f"-z{zmax}",
        "--no-feature-limit",
        "--no-tile-size-limit",
        "--simplify-only-low-zooms",
        "--generate-ids",
        "--read-parallel",
        "--buffer=64",
    ]
    if layer.geometry_type == "polygon":
        cmd.extend(["--detect-shared-borders", "--no-tiny-polygon-reduction"])
    cmd.append(str(geojsons))
    print(" ".join(cmd), flush=True)
    t0 = time.perf_counter()
    subprocess.run(cmd, check=True)
    print(f"tippecanoe terminé en {time.perf_counter() - t0:.1f}s", flush=True)


def _stamp_metadata(mbtiles: Path, layer: InternalLayer, feature_count: int) -> None:
    zmin, zmax = _zoom_range(layer)
    conn = sqlite3.connect(mbtiles)
    try:
        rows = {
            "feature_count": str(feature_count),
            "kerelia_key": layer.key,
            "minzoom": str(zmin),
            "maxzoom": str(zmax),
        }
        if layer.clip_bbox:
            w, s, e, n = layer.clip_bbox
            rows["bounds"] = f"{w},{s},{e},{n}"
        for name, value in rows.items():
            conn.execute("DELETE FROM metadata WHERE name = ?", (name,))
            conn.execute("INSERT INTO metadata(name, value) VALUES(?, ?)", (name, value))
        conn.commit()
    finally:
        conn.close()


def build(layer_key: str) -> Path:
    layer = get_layer(layer_key)
    if layer is None:
        raise SystemExit(f"Couche inconnue : {layer_key}")
    if not layer.mbtiles_file:
        raise SystemExit(f"Pas de mbtiles_file pour {layer_key}")

    MBTILES_DIR.mkdir(parents=True, exist_ok=True)
    geojsons = MBTILES_DIR / f"{layer.key}.geojsons"
    mbtiles = layer.mbtiles_path()
    assert mbtiles is not None

    n = dump_geojsonseq(layer, geojsons)
    if n == 0:
        raise SystemExit("Aucune entité dans l'emprise — MBTiles non généré")
    run_tippecanoe(layer, geojsons, mbtiles)
    _stamp_metadata(mbtiles, layer, n)
    zmin, zmax = _zoom_range(layer)
    size_mo = mbtiles.stat().st_size / 1_000_000
    print(f"OK {mbtiles} ({size_mo:.1f} Mo, {n} entités, z{zmin}–{zmax})")
    return mbtiles


def main(argv: list[str]) -> None:
    args = [a for a in argv[1:] if not a.startswith("-")]
    flags = {a for a in argv[1:] if a.startswith("-")}
    key = args[0] if args else "parcelles_prospects"
    do_upload = "--upload" in flags
    upload_only = "--upload-only" in flags
    if upload_only:
        from .storage_mbtiles import upload_mbtiles

        layer = get_layer(key)
        if layer is None:
            raise SystemExit(f"Couche inconnue : {key}")
        dest = upload_mbtiles(layer)
        print(f"Upload OK → {layer.storage_bucket}/{dest}")
        return
    path = build(key)
    if do_upload:
        from .storage_mbtiles import upload_mbtiles

        layer = get_layer(key)
        assert layer is not None
        dest = upload_mbtiles(layer, path)
        print(f"Upload OK → {layer.storage_bucket}/{dest}")


if __name__ == "__main__":
    main(sys.argv)
