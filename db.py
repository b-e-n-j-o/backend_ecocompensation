#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
db.py
=====

Configuration de la base de données et fonction get_engine().
Module séparé pour éviter les imports circulaires.
"""

import os
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

# Charger les variables d'environnement
load_dotenv(Path(__file__).parent / ".env")

SUPABASE_HOST     = os.environ["SUPABASE_HOST"]
SUPABASE_PORT     = os.getenv("SUPABASE_PORT", "6543")
SUPABASE_DB       = os.getenv("SUPABASE_DB", "postgres")
SUPABASE_USER     = os.environ["SUPABASE_USER"]
SUPABASE_PASSWORD = os.environ["SUPABASE_PASSWORD"]

DB_URL = (
    f"postgresql+psycopg://{SUPABASE_USER}:{quote_plus(SUPABASE_PASSWORD)}"
    f"@{SUPABASE_HOST}:{SUPABASE_PORT}/{SUPABASE_DB}"
)

# Engine singleton
_engine = None


def get_engine():
    """Retourne l'engine SQLAlchemy (singleton)."""
    global _engine
    if _engine is None:
        # pgBouncer (Supabase pooler) = transaction pooling : on ne pool pas côté SQLAlchemy.
        # NullPool = pas de pool, nouvelle connexion à chaque usage → pgBouncer gère tout.
        # prepare_threshold=None = pas de prepared statements (incompatibles avec le pooler).
        _engine = create_engine(
            DB_URL,
            pool_pre_ping=True,
            poolclass=NullPool,
            connect_args={
                "prepare_threshold": None,
                "sslmode": "require",
            },
        )
    return _engine
