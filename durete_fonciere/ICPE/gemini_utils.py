# -*- coding: utf-8 -*-
"""
gemini_utils.py — Utilitaire générique pour appels LLM Gemini
Kerelia — Pipeline Dureté Foncière

Expose une fonction principale importable :
    from gemini_utils import appeler_gemini

Usage :
    from gemini_utils import appeler_gemini

    rapport = appeler_gemini(
        prompt   = mon_prompt_dynamique,
        model    = "gemini-2.0-flash",
        max_tokens = 8192,
        temperature = 0.3,
    )
"""

import logging
import os
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("gemini_utils")

# Modèles disponibles Gemini (ordre de préférence par usage)
MODELE_DEFAUT       = "gemini-3.1-flash-lite-preview"
MODELE_PRO          = "gemini-2.5-pro-preview-03-25"
MODELE_FLASH_LITE   = "gemini-2.0-flash-lite"


def appeler_gemini(
    prompt:      str,
    model:       str   = MODELE_DEFAUT,
    temperature: float = 0.3,
    max_tokens:  int   = 8192,
    api_key:     str   = None,
) -> str:
    """
    Appelle l'API Gemini avec un prompt et retourne le texte généré.

    Paramètres :
        prompt      : Prompt complet (système + contexte + instructions)
        model       : Identifiant du modèle Gemini
        temperature : Créativité (0.0 = déterministe, 1.0 = créatif). 0.3 pour rapports factuels.
        max_tokens  : Nombre max de tokens en sortie
        api_key     : Clé API (optionnel — sinon lue depuis GEMINI_API_KEY env)

    Retourne :
        str : Texte généré par Gemini

    Lève :
        ValueError       : si GEMINI_API_KEY manquante
        ModuleNotFoundError : si google-generativeai non installé
        RuntimeError     : si l'appel API échoue
    """
    try:
        import google.generativeai as genai
    except ModuleNotFoundError as e:
        raise ModuleNotFoundError(
            "Package 'google-generativeai' non installé. "
            "Lance : pip install google-generativeai"
        ) from e

    key = api_key or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise ValueError(
            "Clé API Gemini manquante. "
            "Définis la variable d'environnement GEMINI_API_KEY."
        )

    genai.configure(api_key=key)
    client = genai.GenerativeModel(model)

    log.info(f"Appel Gemini — modèle={model}, prompt={len(prompt):,} chars, max_tokens={max_tokens}")

    try:
        response = client.generate_content(
            prompt,
            generation_config=genai.GenerationConfig(
                temperature=temperature,
                max_output_tokens=max_tokens,
            )
        )
    except Exception as e:
        raise RuntimeError(f"Erreur appel Gemini ({model}) : {e}") from e

    texte = response.text
    log.info(f"Gemini OK — {len(texte):,} chars générés")
    return texte