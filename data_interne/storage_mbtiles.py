"""
MBTiles dans Supabase Storage → cache local → tuiles servies par FastAPI.

Bucket : ecocompensation
Objet  : couches/parcelles-prospects/<fichier>.mbtiles

Le navigateur ne lit jamais Storage. Le backend télécharge une fois le fichier
puis répond /tiles/{z}/{x}/{y}.mvt depuis SQLite local.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import requests
from dotenv import load_dotenv

from .catalog import InternalLayer

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

logger = logging.getLogger(__name__)

FILE_SIZE_LIMIT = 200 * 1024 * 1024  # 200 Mo — le .mbtiles prospects ~54 Mo
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _layer_lock(key: str) -> threading.Lock:
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


def _auth_headers() -> dict[str, str]:
    url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    key = os.getenv("SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or ""
    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SERVICE_KEY manquants pour Storage")
    return {"Authorization": f"Bearer {key}", "apikey": key, "_url": url}


def _storage_base() -> tuple[str, dict[str, str]]:
    headers = _auth_headers()
    url = headers.pop("_url")
    return url, headers


def ensure_bucket(bucket: str) -> None:
    base, headers = _storage_base()
    payload = {
        "id": bucket,
        "name": bucket,
        "public": False,
        "file_size_limit": FILE_SIZE_LIMIT,
    }
    r = requests.post(f"{base}/storage/v1/bucket", json=payload, headers=headers, timeout=30)
    if r.status_code in (200, 201):
        logger.info("Bucket Storage créé : %s", bucket)
        return
    if r.status_code not in (409, 400):
        r.raise_for_status()
    upd = requests.put(
        f"{base}/storage/v1/bucket/{bucket}",
        json={"public": False, "file_size_limit": FILE_SIZE_LIMIT},
        headers=headers,
        timeout=30,
    )
    if upd.status_code >= 400:
        logger.warning("Update bucket %s : %s %s", bucket, upd.status_code, upd.text[:200])


def upload_mbtiles(layer: InternalLayer, local: Path | None = None) -> str:
    if not layer.storage_bucket or not layer.storage_object:
        raise RuntimeError(f"Couche {layer.key} : pas de chemin Storage")
    path = local or layer.mbtiles_path()
    if path is None or not path.is_file():
        raise FileNotFoundError(f"MBTiles local introuvable : {path}")

    ensure_bucket(layer.storage_bucket)
    base, headers = _storage_base()
    object_path = layer.storage_object.lstrip("/")
    url = f"{base}/storage/v1/object/{layer.storage_bucket}/{object_path}"
    hdrs = {
        **headers,
        "Content-Type": "application/octet-stream",
        "x-upsert": "true",
    }
    size_mo = path.stat().st_size / 1_000_000
    logger.info("Upload Storage %s (%.1f Mo) → %s/%s", path.name, size_mo, layer.storage_bucket, object_path)
    with path.open("rb") as f:
        r = requests.post(url, headers=hdrs, data=f, timeout=300)
    if r.status_code >= 400:
        raise RuntimeError(f"Upload Storage {r.status_code}: {r.text[:400]}")
    return object_path


def _download_to(layer: InternalLayer, dest: Path) -> None:
    assert layer.storage_bucket and layer.storage_object
    base, headers = _storage_base()
    object_path = layer.storage_object.lstrip("/")
    url = f"{base}/storage/v1/object/{layer.storage_bucket}/{object_path}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    logger.info("Download Storage %s/%s → %s", layer.storage_bucket, object_path, dest)
    with requests.get(url, headers=headers, stream=True, timeout=300) as r:
        if r.status_code == 404:
            raise FileNotFoundError(f"Objet Storage absent : {layer.storage_bucket}/{object_path}")
        r.raise_for_status()
        with part.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
    part.replace(dest)


def ensure_local_mbtiles(layer: InternalLayer) -> Path | None:
    """Fichier local s'il existe, sinon pull Storage. None si impossible."""
    dest = layer.mbtiles_path()
    if dest is None:
        return None
    if dest.is_file() and dest.stat().st_size > 1024:
        return dest
    if not layer.storage_bucket or not layer.storage_object:
        return dest if dest.is_file() else None

    with _layer_lock(layer.key):
        if dest.is_file() and dest.stat().st_size > 1024:
            return dest
        try:
            _download_to(layer, dest)
        except Exception:
            logger.exception("Échec pull MBTiles %s depuis Storage", layer.key)
            return dest if dest.is_file() else None
    return dest if dest.is_file() else None


def main(argv: list[str]) -> None:
    from .catalog import get_layer

    if len(argv) < 3 or argv[1] != "upload":
        print("usage: python -m data_interne.storage_mbtiles upload <layer_key>")
        raise SystemExit(2)
    layer = get_layer(argv[2])
    if layer is None:
        raise SystemExit(f"Couche inconnue : {argv[2]}")
    dest = upload_mbtiles(layer)
    print(f"OK {layer.storage_bucket}/{dest}")


if __name__ == "__main__":
    import sys

    main(sys.argv)
