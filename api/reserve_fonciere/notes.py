"""Notes informatives persistantes par couche (Web SIG données internes)."""

from __future__ import annotations

import re
import threading
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

LAYER_KEY_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,119}$")
NOTE_MAX_LEN = 8000

ENSURE_SQL = """
CREATE TABLE IF NOT EXISTS ecocompensation.data_interne_layer_notes (
    layer_key text PRIMARY KEY,
    note text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NULL
)
"""

_ready = False
_lock = threading.Lock()


def is_valid_layer_key(key: str) -> bool:
    return bool(LAYER_KEY_RE.match(key))


def ensure_table(engine: Engine) -> None:
    global _ready
    if _ready:
        return
    with _lock:
        if _ready:
            return
        try:
            with engine.begin() as conn:
                conn.execute(text(ENSURE_SQL))
        except IntegrityError:
            # CREATE TABLE IF NOT EXISTS concurrent → collision pg_type
            pass
        _ready = True


def _row_to_dict(layer_key: str, row: Any | None) -> dict[str, Any]:
    if row is None:
        return {"layer_key": layer_key, "note": "", "updated_at": None, "updated_by": None}
    updated = row["updated_at"]
    return {
        "layer_key": layer_key,
        "note": row["note"] or "",
        "updated_at": updated.isoformat() if hasattr(updated, "isoformat") else updated,
        "updated_by": row["updated_by"],
    }


def list_notes(engine: Engine) -> dict[str, dict[str, Any]]:
    ensure_table(engine)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT layer_key, note, updated_at, updated_by
                FROM ecocompensation.data_interne_layer_notes
                """
            )
        ).mappings().all()
    return {str(r["layer_key"]): _row_to_dict(str(r["layer_key"]), r) for r in rows}


def get_note(engine: Engine, layer_key: str) -> dict[str, Any]:
    ensure_table(engine)
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT layer_key, note, updated_at, updated_by
                FROM ecocompensation.data_interne_layer_notes
                WHERE layer_key = :k
                """
            ),
            {"k": layer_key},
        ).mappings().first()
    return _row_to_dict(layer_key, row)


def upsert_note(
    engine: Engine,
    layer_key: str,
    note: str,
    updated_by: str | None = None,
) -> dict[str, Any]:
    ensure_table(engine)
    cleaned = note.strip()
    by = (updated_by or "").strip() or None
    with engine.begin() as conn:
        if not cleaned:
            conn.execute(
                text("DELETE FROM ecocompensation.data_interne_layer_notes WHERE layer_key = :k"),
                {"k": layer_key},
            )
            return _row_to_dict(layer_key, None)
        conn.execute(
            text(
                """
                INSERT INTO ecocompensation.data_interne_layer_notes
                    (layer_key, note, updated_at, updated_by)
                VALUES (:k, :note, now(), :by)
                ON CONFLICT (layer_key) DO UPDATE
                SET note = EXCLUDED.note,
                    updated_at = now(),
                    updated_by = EXCLUDED.updated_by
                """
            ),
            {"k": layer_key, "note": cleaned, "by": by},
        )
    return get_note(engine, layer_key)
