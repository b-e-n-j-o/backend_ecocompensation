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
        # prepare_threshold=None désactive les prepared statements côté serveur (psycopg3).
        # Évite DuplicatePreparedStatement quand une connexion du pool est réutilisée.
        _engine = create_engine(
            DB_URL,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            connect_args={"prepare_threshold": None},
        )
    return _engine
