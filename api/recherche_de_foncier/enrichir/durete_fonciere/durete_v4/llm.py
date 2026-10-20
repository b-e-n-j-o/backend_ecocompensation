# -*- coding: utf-8 -*-
"""
Backends LLM interchangeables : Gemini (google-genai) ou Mistral (REST, stack souveraine).

Deux usages seulement, jamais le calcul du score :
  - `json(prompt)`   : génération structurée (narration, classification) ;
  - `search(prompt)` : recherche web sourcée (Gemini : Google Search grounding ;
                        Mistral : outil intégré `web_search` de l'API Conversations).

Note : V3 utilisait `google-generativeai`, SDK remplacé par `google-genai`.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from . import config, net

log = logging.getLogger("durete.llm")


@dataclass
class ResultatRecherche:
    texte: str
    sources: list = field(default_factory=list)       # [{"titre", "url"}]
    appuis: list = field(default_factory=list)        # [{"texte", "sources": [idx]}] — segments sourcés
    requetes: list = field(default_factory=list)


def parse_json_lenient(text: str) -> dict:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    try:
        return json.loads(t)
    except ValueError:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            return json.loads(m.group(0))
        raise


class GeminiClient:
    nom = "gemini"

    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None):
        from google import genai  # pip install google-genai
        self.model = model or config.GEMINI_MODEL
        self.client = genai.Client(api_key=api_key or os.getenv("GEMINI_API_KEY"))

    def json(self, prompt: str, max_tokens: int = 2048) -> dict:
        from google.genai import types
        r = self.client.models.generate_content(
            model=self.model, contents=prompt,
            config=types.GenerateContentConfig(temperature=config.LLM_TEMPERATURE,
                                               max_output_tokens=max_tokens,
                                               response_mime_type="application/json"))
        return parse_json_lenient(r.text)

    def search(self, prompt: str) -> ResultatRecherche:
        from google.genai import types
        r = self.client.models.generate_content(
            model=self.model, contents=prompt,
            config=types.GenerateContentConfig(temperature=config.LLM_TEMPERATURE,
                                               tools=[types.Tool(google_search=types.GoogleSearch())]))
        res = ResultatRecherche(texte=r.text or "")
        gm = getattr(r.candidates[0], "grounding_metadata", None) if r.candidates else None
        if gm:
            for ch in gm.grounding_chunks or []:
                web = getattr(ch, "web", None)
                res.sources.append({"titre": getattr(web, "title", None), "url": getattr(web, "uri", None)})
            for sup in gm.grounding_supports or []:
                seg = getattr(sup, "segment", None)
                res.appuis.append({"texte": getattr(seg, "text", "") or "",
                                   "sources": list(sup.grounding_chunk_indices or [])})
            res.requetes = list(gm.web_search_queries or [])
        return res


class MistralClient:
    nom = "mistral"
    BASE = "https://api.mistral.ai/v1"

    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None):
        self.model = model or config.MISTRAL_MODEL
        self.key = api_key or os.getenv("MISTRAL_API_KEY", "")
        if not self.key:
            raise ValueError("MISTRAL_API_KEY absente")

    def _post(self, path: str, payload: dict, timeout: int = 120) -> dict:
        r = net.session().post(f"{self.BASE}{path}", json=payload, timeout=timeout,
                               headers={"Authorization": f"Bearer {self.key}"})
        r.raise_for_status()
        return r.json()

    def json(self, prompt: str, max_tokens: int = 2048) -> dict:
        data = self._post("/chat/completions", {
            "model": self.model, "temperature": config.LLM_TEMPERATURE, "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": prompt}],
        })
        return parse_json_lenient(data["choices"][0]["message"]["content"])

    def search(self, prompt: str) -> ResultatRecherche:
        data = self._post("/conversations", {
            "model": self.model, "inputs": prompt, "tools": [{"type": "web_search"}],
            "completion_args": {"temperature": config.LLM_TEMPERATURE}, "store": False,
        }, timeout=180)
        res = ResultatRecherche(texte="")
        textes = []
        for out in data.get("outputs") or []:
            if out.get("type") != "message.output":
                continue
            content = out.get("content")
            if isinstance(content, str):
                textes.append(content)
                continue
            courant, idx_courants = [], []
            for chunk in content or []:
                if chunk.get("type") == "text":
                    if courant and idx_courants:      # segment précédent clos et sourcé
                        res.appuis.append({"texte": "".join(courant), "sources": idx_courants})
                        courant, idx_courants = [], []
                    courant.append(chunk.get("text", ""))
                    textes.append(chunk.get("text", ""))
                elif chunk.get("type") == "tool_reference":
                    res.sources.append({"titre": chunk.get("title"), "url": chunk.get("url")})
                    idx_courants.append(len(res.sources) - 1)
            if courant and idx_courants:
                res.appuis.append({"texte": "".join(courant), "sources": idx_courants})
        res.texte = "".join(textes)
        return res


def get_llm(provider: Optional[str] = None, model: Optional[str] = None):
    """Client LLM ou None (clé absente / provider 'none') — le pipeline reste fonctionnel sans LLM."""
    p = (provider or config.LLM_PROVIDER or "none").lower()
    try:
        if p == "gemini" and os.getenv("GEMINI_API_KEY"):
            return GeminiClient(model)
        if p == "mistral" and os.getenv("MISTRAL_API_KEY"):
            return MistralClient(model)
    except Exception as e:  # noqa: BLE001
        log.warning("LLM %s indisponible : %s", p, e)
    return None
