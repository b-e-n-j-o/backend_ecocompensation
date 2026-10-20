# -*- coding: utf-8 -*-
"""
Sources de données. Chaque module expose :
  - un `fetch_*` (réseau, tolérant aux pannes : ne lève pas, renvoie vide + log)
  - un `analyser_*` / `normaliser_*` PUR (pas de réseau) → testable hors-ligne.
Le scoring ne consomme que les sorties normalisées.
"""
