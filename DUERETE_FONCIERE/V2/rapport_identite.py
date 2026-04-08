# -*- coding: utf-8 -*-
"""
rapport_identite.py — Carte d'identité foncière d'une PM / parcelle(s)
Génère une page de synthèse PDF (ReportLab) à partir du JSON brut du pipeline.

Usage standalone :
    python rapport_identite.py --raw output_raw.json --output carte_identite.pdf
    python rapport_identite.py --raw output_raw.json --output carte.pdf --score 70 --axe1 14 --axe2 15 --axe3 14 --axe4 12
"""

import argparse
import io
import json
import os
import sys
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

# ============================================================
# PALETTE — alignée sur pdf_rapport.py (slate + vert Kerelia)
# ============================================================
C_DARK    = colors.HexColor("#1e293b")
C_MUTED   = colors.HexColor("#64748b")
C_SUB     = colors.HexColor("#94a3b8")
C_BG      = colors.HexColor("#f8fafc")
C_GREEN   = colors.HexColor("#16a34a")
C_AMBER   = colors.HexColor("#ea580c")
C_RED     = colors.HexColor("#dc2626")
C_RULE    = colors.HexColor("#e2e8f0")
C_TEXT    = colors.HexColor("#1e293b")
C_WHITE   = colors.white

DURETE_COLORS = [
    (0,  20,  "#16a34a"),
    (21, 40,  "#22c55e"),
    (41, 60,  "#ca8a04"),
    (61, 80,  "#ea580c"),
    (81, 100, "#dc2626"),
]

def get_durete_color_hex(score):
    if score is None: return "#94a3b8"
    for lo, hi, col in DURETE_COLORS:
        if lo <= score <= hi:
            return col
    return "#94a3b8"

def get_durete_color(score):
    return colors.HexColor(get_durete_color_hex(score))

def get_durete_label(score):
    if score is None:  return "Non calculé"
    if score <= 20:    return "Opportunité exceptionnelle"
    if score <= 40:    return "Dureté faible"
    if score <= 60:    return "Dureté modérée"
    if score <= 80:    return "Dureté forte"
    return "Dureté rédhibitoire"


# ============================================================
# EXTRACTION DONNÉES
# ============================================================
def extraire_donnees(raw: dict, score_final=None, axe1=None, axe2=None, axe3=None, axe4=None) -> dict:
    ann  = raw.get("annuaire_raw", {})
    bod_raw  = raw.get("bodacc_raw", {})
    bod      = raw.get("bodacc", {})  # fallback (structure simplifiée)
    sir  = raw.get("sirene", {})
    dvf  = raw.get("dvf_raw", {})
    parcelles_ctx = raw.get("parcelles", [])  # fallback si on n'a pas dvf_raw
    rpg  = raw.get("rpg_raw", {})
    urba = raw.get("urba", {})
    sup  = raw.get("sup", {})

    siege = ann.get("siege", {})

    # Capital BODACC
    capital = None
    try:
        rec = bod_raw.get("json", {}).get("records", [{}])[0]
        lp  = rec.get("fields", {}).get("listepersonnes", "{}")
        lp_dict = json.loads(lp) if isinstance(lp, str) else lp
        cap_str = lp_dict.get("personne", {}).get("capital", {}).get("montantCapital")
        if cap_str:
            capital = float(cap_str)
    except Exception:
        pass
    if capital is None:
        # Fallback : bodacc simplifié (déjà calculé dans le contexte)
        try:
            capital = bod.get("capital_social_eur")
        except Exception:
            capital = None

    # Dirigeants enrichis
    annee_courante = datetime.now().year
    dir_enrichis = []
    for d in ann.get("dirigeants", []):
        an  = d.get("annee_de_naissance")
        age = (annee_courante - int(an)) if an else None
        dir_enrichis.append({
            "nom":    f"{(d.get('prenoms','').split() or [''])[0]} {d.get('nom','')}".strip(),
            "qualite": d.get("qualite", ""),
            "age":    age,
            "signal": "⚠ Succession" if age and age >= 70 else ("→ Retraite ~10 ans" if age and age >= 60 else ""),
        })

    # DVF (priorité : dvf_raw, sinon données contexte parcelles)
    parcelles_data = []
    if isinstance(dvf, dict) and dvf:
        for idu, dvf_p in dvf.items():
            m = (dvf_p.get("matches") or [{}])[0]
            parcelles_data.append({
                "idu":            idu,
                "commune":        m.get("nom_commune", "NC"),
                "date_acq":       m.get("date_mutation", "NC"),
                "valeur":         float(m.get("valeur_fonciere", 0)) if m.get("valeur_fonciere") else None,
                "nature_culture": m.get("nature_culture", "NC"),
                "surface_terrain":int(m.get("surface_terrain", 0)) if m.get("surface_terrain") else None,
            })
    elif isinstance(parcelles_ctx, list) and parcelles_ctx:
        for p in parcelles_ctx:
            dvf_p = p.get("dvf", {}) or {}
            m0 = (dvf_p.get("mutations_detail") or [{}])[0]
            parcelles_data.append({
                "idu":            p.get("idu", "NC"),
                "commune":        m0.get("commune", "NC"),
                "date_acq":       dvf_p.get("date_acquisition") or m0.get("date", "NC"),
                "valeur":         m0.get("valeur") if m0.get("valeur") is not None else dvf_p.get("valeur_acquisition_eur"),
                "nature_culture": dvf_p.get("nature_culture") or m0.get("culture", "NC"),
                "surface_terrain":m0.get("surface"),
            })

    # RPG cultures
    rpg_cultures = {}
    if isinstance(rpg, dict):
        for idu_key, rpg_p in rpg.items():
            if isinstance(rpg_p, dict):
                for annee_key, data_a in rpg_p.items():
                    if isinstance(data_a, dict):
                        for culture in data_a.get("cultures", []):
                            code = culture.get("code_culture", "?")
                            rpg_cultures[code] = rpg_cultures.get(code, 0) + 1

    tranche     = sir.get("trancheEffectifsUniteLegale") or siege.get("tranche_effectif_salarie", "NC")
    nb_periodes = sir.get("nombrePeriodesUniteLegale", 1)

    return {
        "siren":             raw.get("siren", ""),
        "idus":              raw.get("idus", list(dvf.keys())),
        "date_analyse":      raw.get("date_analyse", datetime.now().strftime("%d/%m/%Y")),
        "denomination":      ann.get("nom_complet", ""),
        "forme_juridique":   ann.get("nature_juridique", ""),
        "statut":            ann.get("etat_administratif", ""),
        "date_creation":     ann.get("date_creation", ""),
        "siege_adresse":     siege.get("adresse", ""),
        "naf":               ann.get("activite_principale", ""),
        "capital":           capital,
        "dirigeants":        dir_enrichis,
        "nb_dirigeants":     len(dir_enrichis),
        "tranche_effectifs": tranche,
        "nb_periodes_sirene":nb_periodes,
        "parcelles":         parcelles_data,
        "rpg_cultures":      rpg_cultures,
        "score_final":       score_final,
        "axe1": axe1, "axe2": axe2, "axe3": axe3, "axe4": axe4,
        "urba_score":        urba.get("score_urba") if urba else None,
        "urba_zone":         urba.get("zonage_dominant") if urba else None,
        "urba_detail":       urba.get("zonage_detail", []) if urba else [],
        "sup_list":          sup.get("servitudes", []) if sup else [],
        # Optionnel : PNG du chart RPG empilé (injecté par l'orchestrateur)
        "rpg_chart_png":     raw.get("rpg_chart_png"),
    }


# ============================================================
# DATAVIZ
# ============================================================
def make_gauge(score, w=280, h=150):
    fig, ax = plt.subplots(figsize=(w/100, h/100), facecolor="none")
    ax.set_aspect("equal")
    ax.axis("off")
    theta = np.linspace(np.pi, 0, 200)
    ax.plot(np.cos(theta), np.sin(theta), color="#e2e8f0", linewidth=12, solid_capstyle="round")
    if score is not None:
        fill_theta = np.linspace(np.pi, np.pi - (score/100)*np.pi, 200)
        ax.plot(np.cos(fill_theta), np.sin(fill_theta),
                color=get_durete_color_hex(score), linewidth=12, solid_capstyle="round")
    s_txt = str(score) if score is not None else "?"
    ax.text(0, -0.05, s_txt, ha="center", va="center", fontsize=26, fontweight="bold", color="#1e293b")
    ax.text(0, -0.38, "/100", ha="center", va="center", fontsize=9, color="#64748b")
    ax.text(0, -0.62, get_durete_label(score), ha="center", va="center", fontsize=6.5, color="#64748b", style="italic")
    ax.set_xlim(-1.3, 1.3)
    ax.set_ylim(-0.85, 1.1)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight", transparent=True, pad_inches=0.05)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def make_rpg_bar(cultures: dict, w=300, h=130):
    if not cultures:
        return None
    items  = sorted(cultures.items(), key=lambda x: x[1], reverse=True)[:8]
    labels = [k for k, _ in items]
    vals   = [v for _, v in items]
    palette = ["#16a34a","#22c55e","#4ade80","#86efac","#bbf7d0","#dcfce7","#ca8a04","#ea580c"]
    fig, ax = plt.subplots(figsize=(w/100, h/100), facecolor="none")
    bars = ax.barh(labels, vals, color=palette[:len(labels)], height=0.6, edgecolor="none")
    ax.set_xlabel("Nb années", fontsize=7, color="#64748b")
    ax.tick_params(axis="both", labelsize=7, colors="#1e293b")
    ax.spines[["top","right","left"]].set_visible(False)
    ax.spines["bottom"].set_color("#e2e8f0")
    ax.set_facecolor("none")
    fig.patch.set_alpha(0)
    for bar, val in zip(bars, vals):
        ax.text(bar.get_width()+0.05, bar.get_y()+bar.get_height()/2, str(val), va="center", fontsize=7, color="#64748b")
    ax.invert_yaxis()
    plt.tight_layout(pad=0.3)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight", transparent=True)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def make_urba_pie(urba_detail: list, w=170, h=170):
    if not urba_detail:
        return None
    labels = [z.get("typezone","?") for z in urba_detail]
    sizes  = [z.get("proportion_pct", 0) for z in urba_detail]
    pal = {"N":"#16a34a","Np":"#22c55e","Nf":"#4ade80","Nh":"#86efac",
           "A":"#ca8a04","Ap":"#ea580c","U":"#dc2626","Uc":"#b91c1c",
           "AU":"#7c3aed","1A":"#6d28d9","2A":"#5b21b6"}
    cols = [pal.get(l[:2], "#94a3b8") for l in labels]
    fig, ax = plt.subplots(figsize=(w/100, h/100), facecolor="none")
    _, texts, autotexts = ax.pie(sizes, labels=labels, colors=cols,
        autopct=lambda p: f"{p:.0f}%" if p > 5 else "",
        startangle=90, pctdistance=0.72,
        wedgeprops={"edgecolor":"white","linewidth":1.5})
    for t in texts + autotexts:
        t.set_fontsize(7)
    fig.patch.set_alpha(0)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight", transparent=True)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ============================================================
# HELPERS CANVAS
# ============================================================
def section_header(c, x, y, w, h, title, bg=None):
    c.setFillColor(bg or C_DARK)
    c.roundRect(x, y - h, w, h, 3, fill=1, stroke=0)
    c.setFillColor(C_WHITE)
    c.setFont("Helvetica-Bold", 7.5)
    c.drawString(x + 5, y - h + 3.5, title.upper())
    return y - h - 2

def card(c, x, y, w, h, r=4):
    c.setFillColor(C_BG)
    c.setStrokeColor(C_RULE)
    c.setLineWidth(0.5)
    c.roundRect(x, y - h, w, h, r, fill=1, stroke=1)

def kv(c, x, y, key, val, kw=40*mm, fs=8):
    c.setFont("Helvetica", fs - 1)
    c.setFillColor(C_MUTED)
    c.drawString(x, y, key)
    c.setFont("Helvetica-Bold", fs)
    c.setFillColor(C_TEXT)
    c.drawString(x + kw, y, str(val) if val else "—")
    return y - (fs + 2.5)


# ============================================================
# GÉNÉRATION PDF
# ============================================================
def generer_carte_identite(data: dict, output_path: str):
    W, H = A4
    c = canvas.Canvas(output_path, pagesize=A4)
    M = 20 * mm
    IW = W - 2 * M   # inner width
    y  = H - M

    # ── HEADER ──────────────────────────────────────────────────────────
    HH = 26 * mm
    c.setFillColor(C_DARK)
    c.rect(0, H - HH, W, HH, fill=1, stroke=0)
    c.setStrokeColor(C_GREEN)
    c.setLineWidth(1.2)
    c.line(0, H - HH, W, H - HH)

    # Badge Kerelia
    c.setFillColor(C_GREEN)
    c.roundRect(M, H - HH + 5*mm, 20*mm, 14*mm, 3, fill=1, stroke=0)
    c.setFillColor(C_WHITE)
    c.setFont("Helvetica-Bold", 9)
    c.drawCentredString(M + 10*mm, H - HH + 11*mm, "KERELIA")

    c.setFillColor(C_WHITE)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(M + 24*mm, H - HH + 14*mm, "CARTE D'IDENTITÉ FONCIÈRE")
    c.setFont("Helvetica", 8)
    c.setFillColor(C_SUB)
    c.drawString(M + 24*mm, H - HH + 7*mm,
                 f"Scoring Dureté Foncière — Pipeline Kerelia — {data['date_analyse']}")

    # Badge score
    score = data.get("score_final")
    if score is not None:
        sc = get_durete_color(score)
        c.setFillColor(sc)
        c.roundRect(W - M - 26*mm, H - HH + 3*mm, 26*mm, 19*mm, 4, fill=1, stroke=0)
        c.setFillColor(C_WHITE)
        c.setFont("Helvetica-Bold", 18)
        c.drawCentredString(W - M - 13*mm, H - HH + 11*mm, str(score))
        c.setFont("Helvetica", 6.5)
        c.drawCentredString(W - M - 13*mm, H - HH + 6*mm, "/100")

    y = H - HH - 4*mm

    # ── BLOC 1 : IDENTITÉ PM | GOUVERNANCE ──────────────────────────────
    col_w  = (IW - 4*mm) / 2
    bloc1_h = 60*mm

    # — Carte gauche : Identité —
    card(c, M, y, col_w, bloc1_h)
    yy = section_header(c, M, y, col_w, 6.5*mm, "Identité de la personne morale")
    yy -= 2*mm
    kx  = M + 3*mm
    kw  = 36*mm
    fs  = 8
    yy = kv(c, kx, yy, "Dénomination",   data["denomination"], kw, fs)
    yy = kv(c, kx, yy, "SIREN",          data["siren"], kw, fs)
    yy = kv(c, kx, yy, "Forme jur.",     data["forme_juridique"], kw, fs)
    yy = kv(c, kx, yy, "Statut",         "Actif ✓" if data["statut"] == "A" else data["statut"], kw, fs)
    yy = kv(c, kx, yy, "Création",       data["date_creation"], kw, fs)
    yy = kv(c, kx, yy, "Activité NAF",   data["naf"], kw, fs)
    cap_str = f"{data['capital']:,.0f} EUR".replace(",", " ") if data.get("capital") else None
    yy = kv(c, kx, yy, "Capital social", cap_str, kw, fs)
    yy = kv(c, kx, yy, "Effectifs",      data.get("tranche_effectifs") or "—", kw, fs)
    siège = data.get("siege_adresse","")
    if len(siège) > 32:
        siège = siège[:32] + "..."
    yy = kv(c, kx, yy, "Siège", siège, kw, fs)

    # Badge stabilité Sirene
    nb_p   = data.get("nb_periodes_sirene", 1)
    s_col  = C_GREEN if nb_p == 1 else (C_AMBER if nb_p <= 2 else C_RED)
    s_txt  = "Aucune modification depuis création" if nb_p == 1 else f"{nb_p} modifications enregistrées"
    c.setFillColor(s_col)
    c.roundRect(kx, yy - 4*mm, col_w - 6*mm, 4.5*mm, 2, fill=1, stroke=0)
    c.setFillColor(C_WHITE)
    c.setFont("Helvetica-Bold", 6.5)
    c.drawCentredString(kx + (col_w-6*mm)/2, yy - 1.5*mm, f"Sirene : {s_txt}")

    # — Carte droite : Gouvernance —
    cx2  = M + col_w + 4*mm
    card(c, cx2, y, col_w, bloc1_h)
    yy2  = section_header(c, cx2, y, col_w, 6.5*mm,
                          f"Gouvernance — {data['nb_dirigeants']} personne(s)")
    yy2 -= 2*mm
    kx2  = cx2 + 3*mm

    for d in data["dirigeants"]:
        if yy2 < y - bloc1_h + 6*mm:
            break
        c.setFont("Helvetica-Bold", 8)
        c.setFillColor(C_TEXT)
        c.drawString(kx2, yy2, d["nom"][:26])
        if d.get("age"):
            c.setFont("Helvetica", 7)
            c.setFillColor(C_MUTED)
            c.drawRightString(cx2 + col_w - 3*mm, yy2, f"{d['age']} ans")
        yy2 -= 4*mm
        c.setFont("Helvetica", 7)
        c.setFillColor(C_MUTED)
        c.drawString(kx2 + 2*mm, yy2, d["qualite"][:40])
        if d.get("signal"):
            c.setFillColor(C_AMBER)
            c.setFont("Helvetica-Bold", 6.5)
            c.drawRightString(cx2 + col_w - 3*mm, yy2, d["signal"])
        yy2 -= 5.5*mm

    y -= bloc1_h + 4*mm

    # ── BLOC 2 : PARCELLES DVF ───────────────────────────────────────────
    nb_parc = len(data["parcelles"])
    bloc2_h = 10*mm + max(nb_parc, 1) * 5*mm + 4*mm
    card(c, M, y, IW, bloc2_h)
    yy = section_header(c, M, y, IW, 6.5*mm,
                        f"Analyse foncière DVF — {nb_parc} parcelle(s)")
    yy -= 2*mm

    # En-têtes tableau
    cols_x  = [M+3*mm, M+38*mm, M+73*mm, M+105*mm, M+135*mm, M+158*mm]
    headers = ["IDU","Commune","Date acquisition","Valeur foncière","Culture","Surface"]
    c.setFont("Helvetica-Bold", 7)
    c.setFillColor(C_MUTED)
    for hd, cx in zip(headers, cols_x):
        c.drawString(cx, yy, hd)
    yy -= 1.5*mm
    c.setStrokeColor(C_RULE)
    c.setLineWidth(0.3)
    c.line(M+3*mm, yy, M+IW-3*mm, yy)
    yy -= 4*mm

    c.setFont("Helvetica", 8)
    c.setFillColor(C_TEXT)
    for p in data["parcelles"]:
        val_str  = f"{p['valeur']:,.0f} EUR".replace(",", " ") if p.get("valeur") else "—"
        surf_str = f"{p['surface_terrain']} m2" if p.get("surface_terrain") else "—"
        row = [p["idu"], p["commune"], p["date_acq"], val_str, p["nature_culture"], surf_str]
        for val, cx in zip(row, cols_x):
            c.drawString(cx, yy, str(val)[:22])
        yy -= 5*mm

    y -= bloc2_h + 4*mm

    # ── BLOC 3 : RPG | PLU ───────────────────────────────────────────────
    half_w = (IW - 4*mm) / 2
    # Hauteur suffisante pour la figure PLU (titre + camembert + légende dans le PNG)
    bloc3_h = 92 * mm

    # Col A — RPG (image doit occuper l'espace)
    cx_a = M
    card(c, cx_a, y, half_w, bloc3_h)
    section_header(c, cx_a, y, half_w, 6.5*mm, "Historique RPG (cultures)")

    img_x = cx_a + 2*mm
    img_y = y - bloc3_h + 3.5*mm
    img_w = half_w - 4*mm
    img_h = bloc3_h - 11*mm

    if data.get("rpg_chart_png"):
        c.drawImage(
            ImageReader(io.BytesIO(data["rpg_chart_png"])),
            img_x, img_y, img_w, img_h,
            preserveAspectRatio=False, mask="auto"
        )
    else:
        rpg_bytes = make_rpg_bar(data.get("rpg_cultures", {}), w=520, h=260)
        if rpg_bytes:
            c.drawImage(
                ImageReader(io.BytesIO(rpg_bytes)),
                img_x, img_y, img_w, img_h,
                preserveAspectRatio=False, mask="auto"
            )
        else:
            c.setFont("Helvetica", 7.5)
            c.setFillColor(C_MUTED)
            c.drawCentredString(cx_a + half_w/2, y - bloc3_h/2, "Données RPG non disponibles")

    # Col B — PLU (camembert + score + top zones)
    cx_b = M + half_w + 4*mm
    card(c, cx_b, y, half_w, bloc3_h)
    section_header(c, cx_b, y, half_w, 6.5*mm, "Zonage PLU")

    urba_detail = data.get("urba_detail", [])
    if urba_detail:
        pie_bytes = None
        try:
            from urba import generer_camembert_zonage_png
            pie_bytes = generer_camembert_zonage_png(urba_detail, width_px=640, height_px=420)
        except Exception:
            pie_bytes = None
        if not pie_bytes:
            pie_bytes = make_urba_pie(urba_detail, w=400, h=280)

        if pie_bytes:
            _hdr = 6.5 * mm
            _gap = 2 * mm
            _score_h = 7 * mm
            img_w = half_w - 4 * mm
            img_h = bloc3_h - _hdr - _gap - _score_h - 1 * mm
            img_y_ll = y - _hdr - _gap - img_h
            c.drawImage(
                ImageReader(io.BytesIO(pie_bytes)),
                cx_b + 2 * mm, img_y_ll,
                img_w, img_h,
                preserveAspectRatio=True, mask="auto",
            )

        # Score global (détail des zones dans le PNG : légende)
        yyy = y - bloc3_h + 3.5 * mm
        us = data.get("urba_score")
        if us is not None:
            c.setFont("Helvetica-Bold", 8)
            c.setFillColor(C_TEXT)
            c.drawCentredString(cx_b + half_w / 2, yyy, f"Score PLU : {us}/10")
    else:
        c.setFont("Helvetica", 7.5)
        c.setFillColor(C_MUTED)
        c.drawCentredString(cx_b + half_w/2, y - bloc3_h/2, "Zonage PLU non analysé")

    y -= bloc3_h + 4*mm

    # ── BLOC 4 : SUP ────────────────────────────────────────────────────
    sup_list = data.get("sup_list", [])
    bloc4_h  = max(16*mm, 9*mm + len(sup_list[:6])*5*mm)
    card(c, M, y, IW, bloc4_h)
    yy = section_header(c, M, y, IW, 6.5*mm,
                        f"Servitudes d'utilité publique — {len(sup_list)} détectée(s)")
    yy -= 2*mm

    if sup_list:
        cx_ = [M+3*mm, M+26*mm, M+105*mm, M+138*mm, M+162*mm]
        hds = ["TYPE","FAMILLE","COUVERTURE","SURFACE m2","NOM"]
        c.setFont("Helvetica-Bold", 7)
        c.setFillColor(C_MUTED)
        for hd, cx in zip(hds, cx_):
            c.drawString(cx, yy, hd)
        yy -= 1.5*mm
        c.setStrokeColor(C_RULE)
        c.setLineWidth(0.3)
        c.line(M+3*mm, yy, M+IW-3*mm, yy)
        yy -= 4*mm
        for s in sup_list[:6]:
            cov = s.get("couverture_pct", 0)
            c.setFont("Helvetica-Bold", 7.5)
            c.setFillColor(C_RED if cov >= 50 else C_TEXT)
            c.drawString(cx_[0], yy, s.get("suptype","?").upper())
            c.setFont("Helvetica", 7.5)
            c.setFillColor(C_TEXT)
            c.drawString(cx_[1], yy, s.get("famille","")[:38])
            c.drawString(cx_[2], yy, f"{cov:.1f}%")
            c.drawString(cx_[3], yy, str(s.get("surface_m2","—")))
            c.drawString(cx_[4], yy, s.get("nomsuplitt","")[:28])
            yy -= 5*mm
    else:
        c.setFont("Helvetica", 8)
        c.setFillColor(C_GREEN)
        c.drawString(M+3*mm, yy, "Aucune servitude detectee sur ces parcelles")

    y -= bloc4_h + 4*mm

    # ── FOOTER ───────────────────────────────────────────────────────────
    c.setFillColor(C_BG)
    c.rect(0, 0, W, 9*mm, fill=1, stroke=0)
    c.setStrokeColor(C_RULE)
    c.setLineWidth(0.3)
    c.line(0, 9*mm, W, 9*mm)
    c.setFont("Helvetica", 6.5)
    c.setFillColor(C_MUTED)
    c.drawString(M, 3.5*mm,
                 f"KERELIA — Usage interne confidentiel — {data['date_analyse']}")
    c.drawRightString(W-M, 3.5*mm,
                      f"SIREN {data['siren']} — {data['denomination']}")

    c.save()
    print(f"Carte d'identite generee : {output_path}")


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw",    required=True)
    parser.add_argument("--output", default="carte_identite.pdf")
    parser.add_argument("--score",  type=int, default=None)
    parser.add_argument("--axe1",   type=int, default=None)
    parser.add_argument("--axe2",   type=int, default=None)
    parser.add_argument("--axe3",   type=int, default=None)
    parser.add_argument("--axe4",   type=int, default=None)
    args = parser.parse_args()

    with open(args.raw, encoding="utf-8") as f:
        raw = json.load(f)

    data = extraire_donnees(
        raw,
        score_final=args.score,
        axe1=args.axe1, axe2=args.axe2,
        axe3=args.axe3, axe4=args.axe4
    )
    generer_carte_identite(data, args.output)