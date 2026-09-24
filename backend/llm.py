"""OpenAI-compatible vision model access, configured entirely by environment.

    OFM_LLM_BASE_URL   default https://openrouter.ai/api/v1 — any OpenAI-compatible endpoint works:
                       OpenRouter, OpenAI, or a private server (vLLM, Ollama, LM Studio, ...)
    OFM_LLM_API_KEY    bearer token; optional for private servers that need none
    OFM_LLM_MODEL      vision-capable model id (default: google/gemini-2.5-flash)
    OFM_LLM_TIMEOUT    seconds (default 120)

The assistant is enabled when a key is set, or when OFM_LLM_BASE_URL points somewhere other than the
default (a private server). Only the standard /chat/completions call with an image_url content part
is used, so no provider-specific API is required. The key stays on the server.

The model is used for what language models are reliable at — reading and interpreting text on a
map. Geometry always comes from pixels (see trace.py), never from the model.
"""
from __future__ import annotations

import base64
import json
import os
import re
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "google/gemini-2.5-flash"
LABEL_KINDS = ["settlement", "street", "building", "water", "region", "landform", "other"]

_transport: httpx.BaseTransport | None = None  # tests inject an httpx.MockTransport here


class LLMError(RuntimeError):
    """The model could not be reached or did not answer usefully."""


def settings() -> dict[str, Any]:
    base_url = os.environ.get("OFM_LLM_BASE_URL", "").strip() or DEFAULT_BASE_URL
    key = os.environ.get("OFM_LLM_API_KEY", "").strip()
    return {
        "base_url": base_url.rstrip("/"),
        "api_key": key,
        "model": os.environ.get("OFM_LLM_MODEL", "").strip() or DEFAULT_MODEL,
        "timeout": float(os.environ.get("OFM_LLM_TIMEOUT", "120")),
        "enabled": bool(key) or base_url.rstrip("/") != DEFAULT_BASE_URL,
    }


def public_settings() -> dict[str, Any]:
    """What the UI may know: enabled, provider host and model — never the key."""
    s = settings()
    return {"enabled": s["enabled"], "provider": httpx.URL(s["base_url"]).host, "model": s["model"]}


def _client(s: dict[str, Any]) -> httpx.Client:
    headers = {"Content-Type": "application/json"}
    if s["api_key"]:
        headers["Authorization"] = f"Bearer {s['api_key']}"
    if "openrouter.ai" in s["base_url"]:
        headers["HTTP-Referer"] = "https://github.com/openhistorymap/raster-vectorizer"
        headers["X-Title"] = "OFM raster-vectorizer"
    return httpx.Client(base_url=s["base_url"], headers=headers, timeout=s["timeout"], transport=_transport)


def parse_json(text: str) -> dict[str, Any]:
    """Pull the JSON object out of a model reply (tolerates code fences and chatter)."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text[text.find("{"): text.rfind("}") + 1]
    try:
        value = json.loads(candidate)
    except ValueError:
        raise LLMError("the model did not return JSON")
    if not isinstance(value, dict):
        raise LLMError("the model did not return a JSON object")
    return value


def chat_json(prompt: str, image_png: bytes | None = None) -> tuple[dict[str, Any], str]:
    """One vision call; returns (parsed JSON object, model id that answered)."""
    s = settings()
    if not s["enabled"]:
        raise LLMError("no model configured: set OFM_LLM_API_KEY (and optionally OFM_LLM_BASE_URL, OFM_LLM_MODEL)")
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    if image_png is not None:
        data = base64.b64encode(image_png).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}})
    body = {"model": s["model"], "temperature": 0, "messages": [{"role": "user", "content": content}]}
    try:
        with _client(s) as client:
            r = client.post("/chat/completions", json=body)
    except httpx.HTTPError as e:
        raise LLMError(f"could not reach {httpx.URL(s['base_url']).host}: {e.__class__.__name__}")
    if r.status_code >= 400:
        try:
            detail = r.json().get("error", {}).get("message") or r.text
        except ValueError:
            detail = r.text
        raise LLMError(f"{httpx.URL(s['base_url']).host} answered {r.status_code}: {str(detail)[:300]}")
    try:
        reply = r.json()
        text = reply["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise LLMError("unexpected response shape from the model endpoint")
    if isinstance(text, list):  # some servers return content parts
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    return parse_json(text or ""), reply.get("model") or s["model"]


READ_PROMPT = """You are reading a region cut from a scanned historical or fantasy map.
Transcribe every text label you can see, exactly as written (keep the original spelling, language,
accents and abbreviations; do not translate or modernise). For each label say what kind of feature
it most likely names: one of {kinds}. If part of a label is illegible, write what you can read and
use [...] for the rest.

Answer with JSON only, no prose:
{{"labels": [{{"text": "...", "kind": "...", "confidence": 0.0}}], "notes": "anything a cartographer should know, or empty"}}
{hint}"""


def read_labels(image_png: bytes, hint: str | None = None) -> dict[str, Any]:
    prompt = READ_PROMPT.format(kinds=", ".join(LABEL_KINDS),
                                hint=f"\nContext from the user: {hint}" if hint else "")
    data, model = chat_json(prompt, image_png)
    labels = []
    for item in data.get("labels") or []:
        if not isinstance(item, dict) or not str(item.get("text", "")).strip():
            continue
        kind = item.get("kind") if item.get("kind") in LABEL_KINDS else "other"
        try:
            conf = min(1.0, max(0.0, float(item.get("confidence", 0.5))))
        except (TypeError, ValueError):
            conf = 0.5
        labels.append({"text": str(item["text"]).strip(), "kind": kind, "confidence": conf})
    return {"labels": labels, "notes": str(data.get("notes") or ""), "model": model}
