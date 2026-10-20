"""
nomenclature.py — Libellés officiels CNIG (standard PLU v2024-01) pour enrichir
les entités GPU, et contrôles qualité simples sur les codes saisis.

Le code saisi par la collectivité n'est PAS considéré comme une vérité : on
l'affiche à côté du libellé saisi, et on lève des `flags` quand il est absent,
hors nomenclature ou générique ("99"). L'interprétation fine viendra plus tard
(normalisation / LLM).
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_CODES_PATH = Path(__file__).with_name("cnig_codes.json")


@lru_cache(maxsize=1)
def _codes() -> Dict[str, Dict[str, str]]:
    with open(_CODES_PATH, encoding="utf-8") as f:
        return json.load(f)


def _norm_code(v) -> Optional[str]:
    """'5' -> '05', ' 01 ' -> '01', None/'' -> None."""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in ("null", "none", "nan"):
        return None
    if s.isdigit() and len(s) == 1:
        s = "0" + s
    return s


def lookup(nomenclature: str, code_type, code_stype=None) -> Tuple[Optional[str], Optional[str], List[str]]:
    """
    Retourne (code_normalisé, libellé_cnig, flags).

    - psc / inf : code "TT-SS". Si le sous-type n'existe pas, on retombe sur "TT-00".
    - typezone / typesect : code simple.
    """
    flags: List[str] = []
    table = _codes().get(nomenclature) or {}
    t = _norm_code(code_type)
    if t is None:
        return None, None, ["code_absent"]

    if nomenclature in ("psc", "inf"):
        st = _norm_code(code_stype) or "00"
        code = f"{t}-{st}"
        label = table.get(code)
        if label is None:
            generic = table.get(f"{t}-00")
            if generic is not None:
                flags.append("sous_type_hors_nomenclature")
                label = generic
            else:
                flags.append("code_hors_nomenclature")
        if t == "99":
            flags.append("code_generique_99")
        return code, label, flags

    label = table.get(t)
    if label is None:
        flags.append("code_hors_nomenclature")
    return t, label, flags


_IDURBA_RE = re.compile(r"^(?P<terr>[0-9AB]{5}|\d{9})_(?P<type>[A-Z]+)_(?P<date>\d{8})")


def parse_idurba(idurba: Optional[str]) -> Dict[str, Optional[str]]:
    """
    '33234_PLU_20190702' -> {territoire: '33234', typedoc: 'PLU', date: '2019-07-02'}
    PLUi : territoire = SIREN de l'EPCI (9 chiffres). Retourne des None si le
    format n'est pas reconnu (cas fréquents de saisies non conformes).
    """
    out = {"territoire": None, "typedoc": None, "date": None}
    if not idurba:
        return out
    m = _IDURBA_RE.match(str(idurba).strip())
    if not m:
        return out
    d = m.group("date")
    out.update(territoire=m.group("terr"), typedoc=m.group("type"), date=f"{d[:4]}-{d[4:6]}-{d[6:]}")
    return out
