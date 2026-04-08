#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Module de visualisation RPG
Génère un histogramme empilé des proportions de cultures par année
"""

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
from collections import defaultdict

# Cache global pour assurer la cohérence des couleurs dans une session
_color_cache = {}


def get_color_palette(n):
    """Génère une palette de n couleurs distinctes et visuellement agréables"""
    if n <= 10:
        cmap = plt.colormaps['tab10']
    elif n <= 20:
        cmap = plt.colormaps['tab20']
    else:
        cmap = plt.colormaps['hsv'].resampled(n)
    
    return [cmap(i / max(n, 1)) for i in range(n)]


def assign_colors(cultures):
    """
    Assigne des couleurs distinctes à une liste de codes culture.
    Utilise un cache pour garantir la cohérence des couleurs.
    """
    global _color_cache
    
    # Nouvelles cultures pas encore dans le cache
    new_cultures = [c for c in cultures if c not in _color_cache]
    
    if new_cultures:
        # Générer assez de couleurs pour toutes les cultures (existantes + nouvelles)
        all_cultures = list(_color_cache.keys()) + new_cultures
        palette = get_color_palette(len(all_cultures))
        
        # Assigner les nouvelles couleurs
        for i, culture in enumerate(all_cultures):
            if culture not in _color_cache:
                _color_cache[culture] = palette[i]
    
    return {c: _color_cache[c] for c in cultures}


def reset_color_cache():
    """Réinitialise le cache de couleurs (utile entre différentes parcelles)"""
    global _color_cache
    _color_cache = {}


def build_chart_data(analysis_results):
    """
    Transforme les résultats d'analyse en données pour le graphique
    
    Args:
        analysis_results: dict {année: {"total": float, "cultures": {code: surface}}}
    
    Returns:
        years, percentages_by_culture, labels
    """
    # Collecter toutes les cultures uniques
    all_cultures = set()
    for year_data in analysis_results.values():
        if year_data and "cultures" in year_data:
            all_cultures.update(year_data["cultures"].keys())
    
    all_cultures = sorted(all_cultures)
    
    # Années triées
    years = sorted([y for y in analysis_results.keys() 
                   if analysis_results[y] and analysis_results[y].get("status") == "agricole"])
    
    if not years:
        print("Aucune année avec données agricoles à afficher")
        return None, None, None
    
    # Construire les pourcentages par culture et par année
    percentages = {culture: [] for culture in all_cultures}
    
    for year in years:
        year_data = analysis_results[year]
        total = year_data.get("total", 0)
        cultures = year_data.get("cultures", {})
        
        for culture in all_cultures:
            surface = cultures.get(culture, 0)
            pct = (surface / total * 100) if total > 0 else 0
            percentages[culture].append(pct)
    
    return years, percentages, all_cultures


def plot_rpg_history(analysis_results, nomenclature=None, output_path=None, 
                     title="Proportions des cultures (RPG)", parcelle_id=None):
    """
    Génère un histogramme empilé des cultures par année
    
    Args:
        analysis_results: dict {année: {"total": float, "cultures": {code: surface}, "labels": {code: label}}}
        nomenclature: dict optionnel pour les libellés
        output_path: chemin de sortie (si None, affiche à l'écran)
        title: titre du graphique
        parcelle_id: identifiant parcelle pour le sous-titre
    """
    years, percentages, cultures = build_chart_data(analysis_results)
    
    if years is None:
        return None
    
    # Configuration du graphique
    fig, ax = plt.subplots(figsize=(12, 7))
    
    x = np.arange(len(years))
    width = 0.7
    
    # Assigner les couleurs dynamiquement
    color_map = assign_colors(list(cultures))
    
    # Empiler les barres
    bottom = np.zeros(len(years))
    bars_info = []
    
    for culture in cultures:
        pcts = percentages[culture]
        color = color_map[culture]
        
        bars = ax.bar(x, pcts, width, bottom=bottom, label=culture, color=color, edgecolor='white', linewidth=0.5)
        
        # Ajouter les pourcentages sur les barres (si > 5%)
        for i, (bar, pct) in enumerate(zip(bars, pcts)):
            if pct > 5:
                ax.text(bar.get_x() + bar.get_width()/2, bottom[i] + pct/2,
                       f'{pct:.1f}%', ha='center', va='center', 
                       fontsize=9, fontweight='bold', color='white')
        
        bottom += pcts
        bars_info.append((culture, color))
    
    # Configuration des axes
    ax.set_xlabel('Année', fontsize=12)
    ax.set_ylabel('Pourcentage', fontsize=12)
    
    if parcelle_id:
        ax.set_title(f"{title}\nParcelle: {parcelle_id}", fontsize=14, fontweight='bold')
    else:
        ax.set_title(title, fontsize=14, fontweight='bold')
    
    ax.set_xticks(x)
    ax.set_xticklabels(years, rotation=0)
    ax.set_ylim(0, 105)
    ax.set_yticks([0, 20, 40, 60, 80, 100])
    
    # Grille légère
    ax.yaxis.grid(True, linestyle='--', alpha=0.3)
    ax.set_axisbelow(True)
    
    # Légende avec libellés complets
    legend_handles = []
    for culture, color in bars_info:
        # Chercher le libellé dans les résultats ou la nomenclature
        label = culture
        for year_data in analysis_results.values():
            if year_data and "labels" in year_data and culture in year_data["labels"]:
                label = f"{culture} — {year_data['labels'][culture]}"
                break
        
        patch = mpatches.Patch(color=color, label=label)
        legend_handles.append(patch)
    
    ax.legend(handles=legend_handles, title='Catégories', 
              loc='center left', bbox_to_anchor=(1, 0.5), fontsize=9)
    
    plt.tight_layout()
    
    if output_path:
        plt.savefig(output_path, dpi=150, bbox_inches='tight', facecolor='white')
        print(f"Graphique sauvegardé: {output_path}")
    else:
        plt.show()
    
    plt.close()
    return output_path


# =====================================================
# DEMO / TEST
# =====================================================

if __name__ == "__main__":
    # Données de test basées sur ta sortie
    test_data = {
        2010: {"status": "non_agricole"},
        2013: {"status": "non_agricole"},
        2014: {"status": "non_agricole"},
        2015: {"status": "non_agricole"},
        2016: {"status": "non_agricole"},
        2017: {
            "status": "agricole",
            "total": 28.0,
            "cultures": {"VRC": 28.0},
            "labels": {"VRC": "Vigne : raisins de cuve"}
        },
        2018: {
            "status": "agricole",
            "total": 28.0,
            "cultures": {"VRC": 28.0},
            "labels": {"VRC": "Vigne : raisins de cuve"}
        },
        2019: {
            "status": "agricole",
            "total": 28.0,
            "cultures": {"VRC": 28.0},
            "labels": {"VRC": "Vigne : raisins de cuve"}
        },
        2020: {
            "status": "agricole",
            "total": 15513.0,
            "cultures": {"VRC": 15513.0},
            "labels": {"VRC": "Vigne : raisins de cuve"}
        },
        2021: {
            "status": "agricole",
            "total": 20675.0,
            "cultures": {"VRC": 20675.0},
            "labels": {"VRC": "Vigne : raisins de cuve"}
        },
        2022: {
            "status": "agricole",
            "total": 122253.0,
            "cultures": {"PTR": 101579.0, "VRC": 20675.0},
            "labels": {"PTR": "Autre prairie temporaire de 5 ans ou moins", "VRC": "Vigne : raisins de cuve"}
        },
        2023: {
            "status": "agricole",
            "total": 122253.0,
            "cultures": {"PTR": 101579.0, "VRC": 20675.0},
            "labels": {"PTR": "Autre prairie temporaire de 5 ans ou moins", "VRC": "Vigne : raisins de cuve"}
        },
        2024: {
            "status": "agricole",
            "total": 122253.0,
            "cultures": {"VRC": 20675.0, "PTR": 101579.0},
            "labels": {"VRC": "Vigne : raisins de cuve", "PTR": "Autre prairie temporaire de 5 ans ou moins"}
        },
    }
    
    plot_rpg_history(
        test_data, 
        output_path="rpg_history_chart.png",
        title="Proportions des cultures (RPG)",
        parcelle_id="33274-0D-0962"
    )