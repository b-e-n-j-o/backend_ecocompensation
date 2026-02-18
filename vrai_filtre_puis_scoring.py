#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vrai_filtre_puis_scoring.py
============================

Enchaîne le vrai filtre (mêmes critères que vrai_filtre.py) puis classe les
parcelles restantes pour les départager.

Toutes les parcelles en sortie du vrai filtre ont déjà :
  - pas de GEOMCE, pas de patrimoine naturel
  - ZDV (ex. Forêt ouverte), tronçon hydro en intersection, surface hydro à ≤ 500 m
  - Miller ≥ 0.39, superficie ≥ 7 ha, distance ≤ rayon dynamique (ex. 10 km)

Pour les départager on utilise uniquement les critères qui varient encore :
  1. Distance au centre de l'AOI (plus proche = mieux) → 3 pts si < 2 km, 2 pts si < 5 km, 1 pt si < 10 km
  2. Surface (plus grande = mieux) → 1 pt si ≥ 20 ha
  3. Coefficient de Miller (plus compact = mieux) → 1 pt si ≥ 0.5
  4. Proximité à une surface hydro (plus proche = mieux) → 1 pt si < 100 m

Tri final : score décroissant, puis distance au centre croissante.
Sortie : liste des parcelles dans l'ordre, avec score et valeurs ayant servi au classement.
"""

from __future__ import annotations

from pathlib import Path
from datetime import datetime

from db import get_engine
from sqlalchemy import text

from vrai_filtre import FiltreOptions, run, TARGET_COUNT, RADIUS_START_KM, RADIUS_MIN_KM

from export_classement_shp import export_classement_shp


def _score_parcelle(p: dict) -> tuple[int, str, list[dict]]:
    """
    Calcule le score de départage (0 à 6 pts), une courte explication et la liste
    des détails pour le rapport HTML.
    """
    dist_m = p.get("distance_centre_m")
    if dist_m is None:
        dist_m = 999999.0
    dist_km = dist_m / 1000.0
    surface_ha = float(p.get("surface_ha") or 0.0)
    miller = float(p.get("miller") or 0.0)
    dist_hydro_m = p.get("dist_surface_hydro_m")

    pts = 0
    details = []
    score_details: list[dict] = []

    # 1. Distance au centre (toutes sont ≤ 10 km)
    if dist_m < 2000:
        pts += 3
        details.append(f"<2 km ({dist_km:.1f} km)=3 pts")
        score_details.append({"critere": "Distance au centre AOI", "points": 3, "raison": f"< 2 km ({dist_km:.1f} km)"})
    elif dist_m < 5000:
        pts += 2
        details.append(f"2–5 km ({dist_km:.1f} km)=2 pts")
        score_details.append({"critere": "Distance au centre AOI", "points": 2, "raison": f"2–5 km ({dist_km:.1f} km)"})
    else:
        pts += 1
        details.append(f"5–10 km ({dist_km:.1f} km)=1 pt")
        score_details.append({"critere": "Distance au centre AOI", "points": 1, "raison": f"5–10 km ({dist_km:.1f} km)"})

    # 2. Surface ≥ 20 ha
    if surface_ha >= 20.0:
        pts += 1
        details.append(f"≥20 ha ({surface_ha:.1f})=1 pt")
        score_details.append({"critere": "Surface", "points": 1, "raison": f"≥ 20 ha ({surface_ha:.1f} ha)"})
    else:
        details.append(f"<20 ha ({surface_ha:.1f})=0 pt")
        score_details.append({"critere": "Surface", "points": 0, "raison": f"< 20 ha ({surface_ha:.1f} ha)"})

    # 3. Miller ≥ 0.5
    if miller >= 0.5:
        pts += 1
        details.append(f"Miller≥0.5 ({miller:.2f})=1 pt")
        score_details.append({"critere": "Coefficient de Miller", "points": 1, "raison": f"≥ 0,5 ({miller:.2f})"})
    else:
        details.append(f"Miller<0.5 ({miller:.2f})=0 pt")
        score_details.append({"critere": "Coefficient de Miller", "points": 0, "raison": f"< 0,5 ({miller:.2f})"})

    # 4. Surface hydro très proche (< 100 m)
    if dist_hydro_m is not None:
        if dist_hydro_m < 100.0:
            pts += 1
            details.append(f"hydro<100 m ({dist_hydro_m:.0f} m)=1 pt")
            score_details.append({"critere": "Proximité surface hydro", "points": 1, "raison": f"< 100 m ({dist_hydro_m:.0f} m)"})
        else:
            details.append(f"hydro≥100 m ({dist_hydro_m:.0f} m)=0 pt")
            score_details.append({"critere": "Proximité surface hydro", "points": 0, "raison": f"≥ 100 m ({dist_hydro_m:.0f} m)"})
    else:
        details.append("hydro=—")
        score_details.append({"critere": "Proximité surface hydro", "points": 0, "raison": "Non renseigné"})

    return pts, " | ".join(details), score_details


def main() -> None:
    engine = get_engine()

    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                SELECT
                    id,
                    ST_X(ST_Centroid(geom_2154)) AS cx,
                    ST_Y(ST_Centroid(geom_2154)) AS cy
                FROM ecocompensation.aoi
                ORDER BY created_at DESC
                LIMIT 1;
                """
            )
        ).mappings().one_or_none()

    if row is None:
        print("⚠️ Aucune AOI trouvée dans ecocompensation.aoi.")
        return

    aoi_id = str(row["id"])
    cx = row["cx"]
    cy = row["cy"]

    print(f"🔗 AOI id       : {aoi_id}")
    print(f"   centre 2154  : x={cx:.1f}, y={cy:.1f}")
    print()

    options = FiltreOptions.defaut()
    result = run(
        engine, aoi_id, cx, cy, options,
        return_parcelles=True,
    )

    if result is None:
        return

    parcelles, final_radius_km, funnel = result

    if not parcelles:
        print("Aucune parcelle en sortie du vrai filtre.")
        return

    # Score et tri : score décroissant, puis distance au centre croissante
    scored = []
    for p in parcelles:
        pts, detail, score_details = _score_parcelle(p)
        scored.append((pts, p.get("distance_centre_m") or 999999.0, detail, score_details, p))

    scored.sort(key=lambda x: (-x[0], x[1]))  # score desc, distance asc

    # Affichage
    print("=" * 100)
    print("CLASSEMENT DES PARCELLES (après vrai filtre)")
    print("=" * 100)
    print()
    print("Critères de départage (toutes ont déjà : pas GEOMCE/patrimoine, ZDV, hydro, Miller≥0.39, ≥7 ha, ≤ rayon) :")
    print("  • Distance au centre AOI : 3 pts si <2 km, 2 pts si <5 km, 1 pt si <10 km")
    print("  • Surface                : 1 pt si ≥20 ha")
    print("  • Miller                 : 1 pt si ≥0.5")
    print("  • Proximité surface hydro: 1 pt si <100 m")
    print()
    print(f"Rayon utilisé pour le filtre : {final_radius_km:.0f} km  |  Parcelles classées : {len(parcelles)}")
    print()
    print("-" * 100)
    print(f"{'Rang':<5} {'IDU':<18} {'Score':<6} {'Dist (km)':<10} {'Surf (ha)':<10} {'Miller':<8} {'Hydro (m)':<10}  Détail")
    print("-" * 100)

    for i, (pts, _dist, detail, _sd, p) in enumerate(scored, 1):
        idu = p.get("idu") or f"{p.get('code_insee','')}/{p.get('section','')}/{p.get('numero','')}"
        dist_km = (p.get("distance_centre_m") or 0) / 1000.0
        surface_ha = p.get("surface_ha") or 0.0
        miller = p.get("miller") or 0.0
        hydro_m = p.get("dist_surface_hydro_m")
        hydro_s = f"{hydro_m:.0f}" if hydro_m is not None else "—"
        print(f"{i:<5} {str(idu):<18} {pts:<6} {dist_km:<10.2f} {surface_ha:<10.2f} {miller:<8.3f} {hydro_s:<10}  {detail}")

    print("-" * 100)
    print()
    print("Fin du classement.")

    # Rapport HTML
    html_out = Path(__file__).with_name("rapport_vrai_filtre_classement.html")
    generer_rapport_html(options, aoi_id, cx, cy, scored, final_radius_km, html_out)
    print(f"📄 Rapport HTML écrit : {html_out}")

    # Export Shapefile (une couche, attributs = filtre + classement)
    shp_out = Path(__file__).with_name("classement_parcelles.shp")
    try:
        export_classement_shp(engine, aoi_id, scored, options, final_radius_km, shp_out)
        print(f"📁 Export SHP écrit : {shp_out}")
    except Exception as e:
        print(f"⚠️ Export SHP non réalisé : {e}")


def generer_rapport_html(
    options: FiltreOptions,
    aoi_id: str,
    cx: float,
    cy: float,
    scored: list,
    final_radius_km: float,
    output_path: Path,
) -> None:
    """Génère un rapport HTML du classement (paramètres du filtre + classement des parcelles)."""
    date_str = datetime.now().strftime("%d/%m/%Y à %H:%M")

    zdv_str = ", ".join(options.zdv_natures) if options.zdv_natures else "Aucune (non filtré)"
    troncon_str = (
        "Intersection avec la parcelle"
        if options.troncon_hydro_mode == "intersect"
        else f"À moins de {options.troncon_hydro_radius_m:.0f} m"
        if options.troncon_hydro_mode == "within_radius"
        else "Ignoré"
    )
    surface_hydro_str = (
        "Intersection avec la parcelle"
        if options.surface_hydro_mode == "intersect"
        else f"À moins de {options.surface_hydro_radius_m:.0f} m"
        if options.surface_hydro_mode == "within_radius"
        else "Ignoré"
    )

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Compte rendu – Filtrage et classement des parcelles</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f5f5f5;
            padding: 20px;
            line-height: 1.6;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            padding: 40px;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}
        h1 {{
            color: #2c3e50;
            margin-bottom: 8px;
            font-size: 28px;
        }}
        .date {{
            color: #7f8c8d;
            margin-bottom: 28px;
            font-size: 14px;
        }}
        .section {{
            margin-bottom: 36px;
        }}
        .section-title {{
            font-size: 20px;
            color: #2c3e50;
            margin-bottom: 16px;
            padding-bottom: 8px;
            border-bottom: 3px solid #3498db;
        }}
        .params-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 16px;
        }}
        .param-card {{
            background: #f8f9fa;
            border-radius: 8px;
            padding: 16px;
            border-left: 4px solid #3498db;
        }}
        .param-label {{
            font-size: 12px;
            color: #7f8c8d;
            text-transform: uppercase;
            margin-bottom: 4px;
        }}
        .param-value {{
            font-size: 15px;
            color: #2c3e50;
            font-weight: 600;
        }}
        .summary {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }}
        .stat-card {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 20px;
            border-radius: 8px;
            text-align: center;
        }}
        .stat-card.success {{ background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%); }}
        .stat-card.info {{ background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%); }}
        .stat-value {{ font-size: 28px; font-weight: bold; }}
        .stat-label {{ font-size: 13px; opacity: 0.9; }}
        .criteria-list {{
            background: #f8f9fa;
            padding: 16px;
            border-radius: 8px;
            margin-top: 12px;
            font-size: 14px;
        }}
        .criteria-list li {{ margin-bottom: 6px; }}
        .parcelle-card {{
            background: white;
            border: 1px solid #e0e0e0;
            border-radius: 8px;
            padding: 24px;
            margin-bottom: 20px;
            transition: box-shadow 0.3s;
        }}
        .parcelle-card:hover {{ box-shadow: 0 4px 12px rgba(0,0,0,0.12); }}
        .parcelle-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            padding-bottom: 12px;
            border-bottom: 2px solid #f0f0f0;
        }}
        .parcelle-id {{ font-size: 18px; font-weight: 600; color: #2c3e50; }}
        .score-badge {{
            font-size: 24px;
            font-weight: bold;
            padding: 8px 18px;
            border-radius: 8px;
            color: white;
        }}
        .score-high {{ background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%); }}
        .score-mid {{ background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%); }}
        .score-low {{ background: linear-gradient(135deg, #a8a8a8 0%, #6c6c6c 100%); }}
        .parcelle-info {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
            gap: 12px;
            margin-bottom: 16px;
        }}
        .info-item {{
            background: #f8f9fa;
            padding: 10px;
            border-radius: 6px;
        }}
        .info-label {{ font-size: 11px; color: #7f8c8d; text-transform: uppercase; margin-bottom: 4px; }}
        .info-value {{ font-size: 15px; color: #2c3e50; font-weight: 600; }}
        .score-table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
        .score-table th {{ background: #ecf0f1; padding: 10px; text-align: left; font-weight: 600; color: #2c3e50; }}
        .score-table td {{ padding: 10px; border-bottom: 1px solid #ecf0f1; }}
        .points {{ font-weight: bold; padding: 4px 10px; border-radius: 4px; display: inline-block; min-width: 44px; text-align: center; }}
        .points-positive {{ background: #d4edda; color: #155724; }}
        .points-zero {{ background: #e2e3e5; color: #383d41; }}
        .rank {{
            display: inline-block;
            background: #ffd700;
            color: #333;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 14px;
            font-weight: bold;
            margin-right: 10px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Compte rendu – Filtrage et classement des parcelles</h1>

        <div class="section">
            <h2 class="section-title">Paramètres du filtre utilisé</h2>
            <div class="params-grid">
                <div class="param-card">
                    <div class="param-label">Zone de végétation (ZDV)</div>
                    <div class="param-value">{zdv_str}</div>
                </div>
                <div class="param-card">
                    <div class="param-label">Cours d'eau</div>
                    <div class="param-value">{troncon_str}</div>
                </div>
                <div class="param-card">
                    <div class="param-label">Surface d'eau</div>
                    <div class="param-value">{surface_hydro_str}</div>
                </div>
                <div class="param-card">
                    <div class="param-label">Miller minimum</div>
                    <div class="param-value">≥ 0,39</div>
                </div>
                <div class="param-card">
                    <div class="param-label">Superficie minimum</div>
                    <div class="param-value">≥ 7 ha</div>
                </div>
                <div class="param-card">
                    <div class="param-label">Fourchette de rayon</div>
                    <div class="param-value">{RADIUS_START_KM:.0f} km → {RADIUS_MIN_KM:.0f} km (cible ≤ {TARGET_COUNT} parcelles)</div>
                </div>
                <div class="param-card">
                    <div class="param-label">Rayon final</div>
                    <div class="param-value">{final_radius_km:.0f} km</div>
                </div>
            </div>
        </div>

        <div class="section">
            <h2 class="section-title">Critères de départage (score 0–6 pts)</h2>
            <p style="margin-bottom: 8px;">Les parcelles ci-dessous ont déjà satisfait le filtre (hors GEOMCE, hors patrimoine naturel (ZNIEFF, Natura 2000, pâturages,...), une zone de végétation ciblée : Forêt ouverte, des éléments hydro : tronçon ou surface d'eau dans ou proche de la parcelle, coefficient de Miller ≥ 0,39, superficie ≥ 7 ha, dans le rayon). Le score sert à les classer entre elles.</p>
            <ul class="criteria-list">
                <li><strong>Distance au centre de la zone d'étude :</strong> 3 pts si &lt; 2 km, 2 pts si 2–5 km, 1 pt si 5–10 km</li>
                <li><strong>Surface :</strong> 1 pt si ≥ 20 ha</li>
                <li><strong>Coefficient de Miller :</strong> 1 pt si ≥ 0,5</li>
                <li><strong>Proximité à une surface d'eau :</strong> 1 pt si &lt; 100 m</li>
            </ul>
        </div>

        <div class="summary">
            <div class="stat-card info">
                <div class="stat-value">{len(scored)}</div>
                <div class="stat-label">Parcelles classées</div>
            </div>
            <div class="stat-card success">
                <div class="stat-value">{max((s[0] for s in scored), default=0)}</div>
                <div class="stat-label">Score maximum</div>
            </div>
        </div>

        <div class="section">
            <h2 class="section-title">Classement des parcelles</h2>
"""

    for i, (pts, _dist, _detail, score_details, p) in enumerate(scored, 1):
        idu = p.get("idu") or f"{p.get('code_insee','')}/{p.get('section','')}/{p.get('numero','')}"
        dist_km = (p.get("distance_centre_m") or 0) / 1000.0
        surface_ha = p.get("surface_ha") or 0.0
        miller = p.get("miller") or 0.0
        hydro_m = p.get("dist_surface_hydro_m")
        hydro_s = f"{hydro_m:.0f} m" if hydro_m is not None else "—"
        score_class = "score-high" if pts >= 5 else "score-mid" if pts >= 3 else "score-low"

        html += f"""
            <div class="parcelle-card">
                <div class="parcelle-header">
                    <div>
                        <span class="rank">#{i}</span>
                        <span class="parcelle-id">{idu}</span>
                    </div>
                    <div class="score-badge {score_class}">{pts} pts</div>
                </div>
                <div class="parcelle-info">
                    <div class="info-item"><div class="info-label">Distance</div><div class="info-value">{dist_km:.2f} km</div></div>
                    <div class="info-item"><div class="info-label">Surface</div><div class="info-value">{surface_ha:.2f} ha</div></div>
                    <div class="info-item"><div class="info-label">Miller</div><div class="info-value">{miller:.3f}</div></div>
                    <div class="info-item"><div class="info-label">Hydro</div><div class="info-value">{hydro_s}</div></div>
                </div>
                <table class="score-table">
                    <thead><tr><th>Critère</th><th>Points</th><th>Détail</th></tr></thead>
                    <tbody>
"""
        for d in score_details:
            pts_val = d["points"]
            pts_class = "points-positive" if pts_val > 0 else "points-zero"
            pts_text = f"+{pts_val}" if pts_val > 0 else str(pts_val)
            html += f"""
                        <tr>
                            <td>{d["critere"]}</td>
                            <td><span class="points {pts_class}">{pts_text}</span></td>
                            <td>{d["raison"]}</td>
                        </tr>
"""
        html += """
                    </tbody>
                </table>
            </div>
"""

    html += """
        </div>
    </div>
</body>
</html>
"""

    output_path.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
