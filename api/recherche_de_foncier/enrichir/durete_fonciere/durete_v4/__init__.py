# -*- coding: utf-8 -*-
"""
Kerelia — Dureté foncière V4 : analyse de la situation foncière d'une personne
morale à partir de son SIREN (score déterministe traçable + recherche web
sourcée + synthèse). Voir DESIGN.md.
"""
from .config import SCORING_VERSION
from .pipeline import Sources, analyser_pm
from .service import run_durete_batch_in_memory, run_durete_for_siren

__all__ = ["analyser_pm", "Sources", "run_durete_for_siren", "run_durete_batch_in_memory", "SCORING_VERSION"]
__version__ = SCORING_VERSION
