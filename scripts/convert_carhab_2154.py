"""
recreate_carhab_clean_2154.py
------------------------------
Recrée ecocompensation.carhab_clean en EPSG:2154 par batch de 10K.
"""

import os
import logging
from pathlib import Path
from urllib.parse import quote_plus

from sqlalchemy import create_engine, text as _text
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

BATCH_SIZE = 10_000


def get_engine():
    BASE_DIR = Path(__file__).resolve().parent
    load_dotenv(BASE_DIR / ".env")
    host     = os.getenv("SUPABASE_HOST")
    port     = os.getenv("SUPABASE_PORT", "6543")
    db       = os.getenv("SUPABASE_DB", "postgres")
    user     = os.getenv("SUPABASE_USER")
    password = os.getenv("SUPABASE_PASSWORD")
    if not all([host, db, user, password]):
        raise RuntimeError("Variables de connexion manquantes")
    return create_engine(
        f"postgresql+psycopg://{user}:{quote_plus(password)}@{host}:{port}/{db}",
        pool_pre_ping=True,
    )


DDL_CREATE = """
DROP TABLE IF EXISTS ecocompensation.carhab_clean;
CREATE TABLE ecocompensation.carhab_clean (
    id_polygone_carhab double precision,
    code_eunis         text,
    nom_eunis          text,
    geom               geometry(Geometry, 2154)
);
"""

DDL_INDEX = """
CREATE INDEX ON ecocompensation.carhab_clean USING gist(geom);
"""

INSERT_BATCH = """
INSERT INTO ecocompensation.carhab_clean (id_polygone_carhab, code_eunis, nom_eunis, geom)
SELECT
    id_polygone_carhab,
    code_eunis,
    nom_eunis,
    ST_MakeValid(ST_Transform(geometry, 2154))
FROM ecocompensation.carhab
WHERE geometry IS NOT NULL
  AND NOT ST_IsEmpty(geometry)
ORDER BY id_polygone_carhab
LIMIT :batch_size OFFSET :offset;
"""

COUNT_QUERY = """
SELECT COUNT(*) FROM ecocompensation.carhab
WHERE geometry IS NOT NULL AND NOT ST_IsEmpty(geometry);
"""


def main():
    engine = get_engine()

    with engine.begin() as conn:
        log.info("Création de la table cible...")
        conn.execute(_text(DDL_CREATE))

    with engine.connect() as conn:
        total = conn.execute(_text(COUNT_QUERY)).scalar()
    log.info("Total entités à traiter : %d", total)

    offset = 0
    batch_num = 0
    while offset < total:
        batch_num += 1
        with engine.begin() as conn:
            conn.execute(_text(INSERT_BATCH), {"batch_size": BATCH_SIZE, "offset": offset})
        log.info("Batch %3d — offset %6d / %d", batch_num, offset + BATCH_SIZE, total)
        offset += BATCH_SIZE

    with engine.begin() as conn:
        log.info("Création de l'index GiST...")
        conn.execute(_text(DDL_INDEX))

    log.info("=== Terminé. %d batches traités. ===", batch_num)


if __name__ == "__main__":
    main()