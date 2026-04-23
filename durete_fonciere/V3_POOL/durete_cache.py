from __future__ import annotations

import logging
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

logger = logging.getLogger(__name__)

CACHE_VERSION = "v1"
CACHE_TTL_DAYS = 7


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def get_cached_siren(conn, siren: str) -> dict | None:
    """Retourne le résultat caché pour ce SIREN, ou None si absent/expiré."""
    row = conn.execute(
        text(
            """
            SELECT
                score_llm_base, niveau_durete, explication,
                detail_axes, statut, avertissements,
                denomination, forme_juridique
            FROM ecocompensation.durete_fonciere_cache
            WHERE siren = :siren
              AND cache_version = :version
              AND expires_at > :now
            """
        ),
        {"siren": siren, "version": CACHE_VERSION, "now": _now_utc()},
    ).mappings().one_or_none()

    if row is None:
        return None

    logger.debug("DURETE CACHE HIT | siren=%s version=%s", siren, CACHE_VERSION)
    return {
        "score_final": row["score_llm_base"],   # renommé pour compatibilité avec run_durete_for_siren
        "score_llm_base": row["score_llm_base"],
        "niveau_durete": row["niveau_durete"],
        "explication": row["explication"],
        "detail_axes": row["detail_axes"],
        "statut": row["statut"],
        "avertissements": list(row["avertissements"] or []),
        "denomination": row["denomination"],
        "siren": siren,
        "_from_cache": True,
    }


def set_cached_siren(
    conn,
    siren: str,
    res: dict,
    denomination: str = "",
    forme_juridique: str = "",
) -> None:
    """Persiste le résultat LLM pour ce SIREN. Écrase si déjà présent (upsert)."""
    expires_at = _now_utc() + timedelta(days=CACHE_TTL_DAYS)
    detail_axes = res.get("detail_axes")
    avertissements = res.get("avertissements", [])

    conn.execute(
        text(
            """
            INSERT INTO ecocompensation.durete_fonciere_cache (
                siren, cache_version,
                score_llm_base, niveau_durete, explication,
                detail_axes, statut, avertissements,
                denomination, forme_juridique,
                computed_at, expires_at
            ) VALUES (
                :siren, :version,
                :score_llm_base, :niveau_durete, :explication,
                CAST(:detail_axes AS jsonb), :statut, CAST(:avertissements AS jsonb),
                :denomination, :forme_juridique,
                :now, :expires_at
            )
            ON CONFLICT (siren, cache_version) DO UPDATE SET
                score_llm_base  = EXCLUDED.score_llm_base,
                niveau_durete   = EXCLUDED.niveau_durete,
                explication     = EXCLUDED.explication,
                detail_axes     = EXCLUDED.detail_axes,
                statut          = EXCLUDED.statut,
                avertissements  = EXCLUDED.avertissements,
                denomination    = EXCLUDED.denomination,
                forme_juridique = EXCLUDED.forme_juridique,
                computed_at     = EXCLUDED.computed_at,
                expires_at      = EXCLUDED.expires_at
            """
        ),
        {
            "siren": siren,
            "version": CACHE_VERSION,
            "score_llm_base": res.get("score_final"),
            "niveau_durete": res.get("niveau_durete"),
            "explication": res.get("explication"),
            "detail_axes": json.dumps(detail_axes) if detail_axes is not None else None,
            "statut": res.get("statut", "ok"),
            "avertissements": json.dumps(avertissements or []),
            "denomination": denomination,
            "forme_juridique": forme_juridique,
            "now": _now_utc(),
            "expires_at": expires_at,
        },
    )
    logger.debug(
        "DURETE CACHE SET | siren=%s version=%s expires_at=%s",
        siren, CACHE_VERSION, expires_at.isoformat()
    )