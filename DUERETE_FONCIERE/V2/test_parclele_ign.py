"""
Debug WFS IGN — trouve le bon format de section pour une parcelle donnée.
Lance depuis ton environnement local.
"""
import requests, json

WFS_URL = "https://data.geopf.fr/wfs/ows"

def test_wfs(insee, section, numero, label=""):
    cql = f"code_insee='{insee}' AND section='{section}' AND numero='{numero}'"
    params = {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeNames": "CADASTRALPARCELS.PARCELLAIRE_EXPRESS:parcelle",
        "srsName": "EPSG:2154", "outputFormat": "application/json",
        "CQL_FILTER": cql,
    }
    r = requests.get(WFS_URL, params=params, timeout=30)
    feats = r.json().get("features", [])
    print(f"  [{label or section}] CQL={cql!r} → {len(feats)} feature(s)")
    if feats:
        props = feats[0].get("properties", {})
        print(f"    section dans réponse = {props.get('section')!r}")
        print(f"    numero dans réponse  = {props.get('numero')!r}")
    return len(feats) > 0

print("=== Test WFS IGN — section formats ===\n")

# Notre cas : 86275000D0319
for section in ["D", "0D", "000D", "d", "0d"]:
    test_wfs("86275", section, "0319", label=f"section={section!r}")

print("\n=== Recherche large par commune (sans filtre section) ===")
params = {
    "service": "WFS", "version": "2.0.0", "request": "GetFeature",
    "typeNames": "CADASTRALPARCELS.PARCELLAIRE_EXPRESS:parcelle",
    "srsName": "EPSG:2154", "outputFormat": "application/json",
    "CQL_FILTER": "code_insee='86275' AND numero='0319'",
    "count": 5,
}
r = requests.get(WFS_URL, params=params, timeout=30)
feats = r.json().get("features", [])
print(f"  Sans filtre section : {len(feats)} feature(s)")
for f in feats:
    p = f.get("properties", {})
    print(f"  → section={p.get('section')!r} numero={p.get('numero')!r} commune={p.get('nom_com')!r}")