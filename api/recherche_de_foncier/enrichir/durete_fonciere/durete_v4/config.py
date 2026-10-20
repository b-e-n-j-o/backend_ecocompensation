# -*- coding: utf-8 -*-
"""
Configuration centrale du pipeline dureté foncière V4.

Tout ce qui est « réglage métier » (barèmes, seuils) est ici, pas dans le code
des règles : un urbaniste / l'équipe compensation peut relire et ajuster un seul
fichier, et chaque changement de barème doit s'accompagner d'un bump de
SCORING_VERSION (invalide le cache).
"""
from __future__ import annotations

import os

SCORING_VERSION = "4.0.0"

# ---------------------------------------------------------------------------
# Réseau
# ---------------------------------------------------------------------------
HTTP_TIMEOUT = float(os.getenv("DURETE_HTTP_TIMEOUT", "20"))
USER_AGENT = "Kerelia-DureteFonciere/4.0 (+https://kerelia.fr)"
# Cache disque optionnel (DVF communes, RPG, GPU…) — vide = cache mémoire seul
CACHE_DIR = os.getenv("DURETE_CACHE_DIR", "").strip() or None

ANNUAIRE_URL = "https://recherche-entreprises.api.gouv.fr/search"
ANNUAIRE_MIN_INTERVAL_S = 0.16          # API limitée à ~7 req/s
BODACC_URL = (
    "https://bodacc-datadila.opendatasoft.com/api/explore/v2.1/"
    "catalog/datasets/annonces-commerciales/records"
)
DVF_BASE_URL = "https://files.data.gouv.fr/geo-dvf/latest/csv"
DVF_NB_ANNEES = 7                        # on sonde N années glissantes, les 404 sont ignorés
WFS_URL = "https://data.geopf.fr/wfs/ows"
APICARTO_GPU_URL = "https://apicarto.ign.fr/api/gpu"
SIRENE_URL = "https://api.insee.fr/api-sirene/3.11"

# ---------------------------------------------------------------------------
# RPG
# ---------------------------------------------------------------------------
# Une intersection RPG n'est retenue comme « déclaration PAC sur la parcelle »
# que si elle couvre au moins RPG_SEUIL_RATIO de la parcelle OU RPG_SEUIL_M2.
# (avant : toute intersection, même un liseré d'1 m² en bord de parcelle)
RPG_SEUIL_RATIO = 0.10
RPG_SEUIL_M2 = 2500.0
RPG_MAX_WORKERS = 4

# ---------------------------------------------------------------------------
# Barèmes (guide méthodologique Kerelia / SNCRR + extensions V4 documentées)
# ---------------------------------------------------------------------------
AXE_MAX = {"axe1": 40, "axe2": 25, "axe3": 20, "axe4": 15}

# Axe 4 — ancienneté de détention (années) → points
AXE4_BAREME = [  # (borne sup exclusive, points, libellé)
    (3, 15, "acquisition très récente (< 3 ans)"),
    (5, 12, "détention courte (3-5 ans)"),
    (15, 8, "détention moyenne (5-15 ans)"),
    (30, 8, "détention longue (15-30 ans)"),
    (10_000, 12, "très longue détention (> 30 ans)"),
]

# Surcharges
SURCHARGE_BAIL_CERTAIN = 15
SURCHARGE_BAIL_PROBABLE = 8
SURCHARGE_BAIL_PONCTUEL = 3
SURCHARGE_FAIRE_VALOIR_DIRECT = 5       # extension V4 : la PM exploite elle-même
SURCHARGE_DROIT_REEL_TIERS = 15         # emphytéose / bail à construction (guide)
# Extension V4 — pression foncière via zonage PLU (désactivable)
SURCHARGE_ZONAGE_ACTIVE = os.getenv("DURETE_SURCHARGE_ZONAGE", "1") not in {"0", "false", "no"}
SURCHARGE_ZONAGE = {"U": 10, "AU": 8, "STECAL": 3, "A": 0, "N": 0}

NIVEAUX = [  # (borne sup inclusive, libellé)
    (20, "Opportunité exceptionnelle"),
    (40, "Dureté faible"),
    (60, "Dureté modérée"),
    (80, "Dureté forte"),
    (100, "Dureté rédhibitoire"),
]

# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
LLM_PROVIDER = os.getenv("DURETE_LLM_PROVIDER", "gemini").strip().lower()   # gemini | mistral | none
GEMINI_MODEL = os.getenv("DURETE_GEMINI_MODEL", "gemini-3.1-flash-lite").strip()
MISTRAL_MODEL = os.getenv("DURETE_MISTRAL_MODEL", "mistral-medium-latest").strip()
LLM_TEMPERATURE = 0.2
