"""
carhab_bulk.py
--------------
Intersection CARHAB sur tout le pool CSV en une seule requête bulk.
"""

import os
import time
import logging
from pathlib import Path
from urllib.parse import quote_plus

import pandas as pd
from sqlalchemy import create_engine, text as _text
from dotenv import load_dotenv

CSV_PATH = Path("/Volumes/T7/Travaux_Freelance/KERELIA/CUAs/COMPENSATION_PARCELLE/COMPENSATION_ECO/backend/pool_de_parcelles_de_test.csv")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def get_engine():
    BASE_DIR = Path(__file__).resolve().parent
    load_dotenv(BASE_DIR / ".env")
    host     = os.getenv("SUPABASE_HOST")
    port     = os.getenv("SUPABASE_PORT", "6543")
    db       = os.getenv("SUPABASE_DB", "postgres")
    user     = os.getenv("SUPABASE_USER")
    password = os.getenv("SUPABASE_PASSWORD")
    if not all([host, db, user, password]):
        raise RuntimeError("Variables de connexion manquantes dans le .env")
    return create_engine(
        f"postgresql+psycopg://{user}:{quote_plus(password)}@{host}:{port}/{db}",
        pool_pre_ping=True,
    )


def main():
    df = pd.read_csv(CSV_PATH, sep=";")
    idus = df["idu"].tolist()
    log.info("CSV chargé : %d parcelles", len(idus))

    engine = get_engine()
    log.info("Connexion DB OK — lancement requête bulk...")

    # Injection directe dans le SQL — valeurs contrôlées depuis CSV, pas de risque injection
    idus_sql = ", ".join(f"'{idu}'" for idu in idus)
    query = _text(f"""
        SELECT DISTINCT
            p.idu,
            c.nom_eunis,
            c.code_eunis
        FROM ecocompensation.parcelles p
        JOIN ecocompensation.carhab_clean c
            ON ST_Intersects(c.geom, ST_Transform(p.geom_2154, 4326))
        WHERE p.idu IN ({idus_sql})
          AND c.nom_eunis IS NOT NULL
        ORDER BY p.idu, c.nom_eunis
    """)

    t0 = time.time()
    with engine.connect() as conn:
        rows = conn.execute(query).mappings().all()
    elapsed = time.time() - t0
    log.info("Requête terminée en %.1fs — %d lignes retournées", elapsed, len(rows))

    # Grouper par IDU pour affichage
    by_idu = {}
    for r in rows:
        by_idu.setdefault(r["idu"], []).append({
            "code_eunis": r["code_eunis"],
            "nom_eunis":  r["nom_eunis"],
        })

    # Affichage + construction résultat
    resultats = []
    for _, row in df.iterrows():
        idu  = row["idu"]
        rang = row["rang"]
        eunis_list = by_idu.get(idu, [])

        if eunis_list:
            log.info(
                "Rang %2d | %-20s -> %d classe(s) : %s",
                rang, idu, len(eunis_list),
                ", ".join(f"{e['code_eunis']} ({e['nom_eunis']})" for e in eunis_list),
            )
            for e in eunis_list:
                resultats.append({"rang": rang, "idu": idu, **e})
        else:
            log.info("Rang %2d | %-20s -> aucune intersection CARHAB", rang, idu)
            resultats.append({"rang": rang, "idu": idu, "code_eunis": None, "nom_eunis": None})

    out_path = CSV_PATH.parent / "carhab_intersections_bulk.csv"
    pd.DataFrame(resultats).to_csv(out_path, index=False, sep=";")
    log.info("Resultats exportes -> %s", out_path)
    log.info("=== Termine. %d parcelles, %.1fs total ===", len(df), time.time() - t0)


if __name__ == "__main__":
    main()