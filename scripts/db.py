"""
db.py
=====

Fournit deux engines SQLAlchemy :
  - get_engine()     → base principale (ecocompensation, résultats, projets)
  - get_engine_ppm() → base PPM (public.parcelles_personnes_morales)

Si SUPABASE_PPM_HOST n'est pas défini, get_engine_ppm() retourne le même
engine que get_engine() (utile si les deux bases sont sur la même instance).
"""

from __future__ import annotations

import os
from functools import lru_cache
from urllib.parse import quote_plus

from sqlalchemy import create_engine, Engine
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / ".env")


def _build_engine(
    host: str,
    port: str,
    db: str,
    user: str,
    password: str,
) -> Engine:
    url = (
        f"postgresql+psycopg://{user}:{quote_plus(password)}"
        f"@{host}:{port}/{db}"
    )
    return create_engine(
        url,
        connect_args={
            "keepalives":          1,
            "keepalives_idle":     30,
            "keepalives_interval": 10,
            "keepalives_count":    5,
            # Supabase/pgBouncer: désactiver les prepared statements côté psycopg
            # pour éviter les collisions "DuplicatePreparedStatement".
            "prepare_threshold":   None,
        },
        pool_pre_ping=True,
    )


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Engine vers la base principale (ecocompensation + résultats)."""
    return _build_engine(
        host     = os.environ["SUPABASE_HOST"],
        port     = os.getenv("SUPABASE_PORT", "6543"),
        db       = os.getenv("SUPABASE_DB", "postgres"),
        user     = os.environ["SUPABASE_USER"],
        password = os.environ["SUPABASE_PASSWORD"],
    )


@lru_cache(maxsize=1)
def get_engine_ppm() -> Engine:
    """
    Engine vers la base PPM (public.parcelles_personnes_morales).
    Utilise SUPABASE_PPM_* si défini, sinon fallback sur les variables
    principales (même base).
    """
    host     = os.getenv("SUPABASE_PPM_HOST")     or os.environ["SUPABASE_HOST"]
    port     = os.getenv("SUPABASE_PPM_PORT")     or os.getenv("SUPABASE_PORT", "6543")
    db       = os.getenv("SUPABASE_PPM_DB")       or os.getenv("SUPABASE_DB", "postgres")
    user     = os.getenv("SUPABASE_PPM_USER")     or os.environ["SUPABASE_USER"]
    password = os.getenv("SUPABASE_PPM_PASSWORD") or os.environ["SUPABASE_PASSWORD"]

    return _build_engine(host=host, port=port, db=db, user=user, password=password)