"""Optional LLM reasoning. Every response must parse and validate against a Pydantic schema;
anything else is logged and the caller falls back to the deterministic result.

Providers (first configured wins, or force with LLM_PROVIDER=gemini|anthropic|openai_compat):
  gemini         GEMINI_API_KEY      (google-genai SDK; model GEMINI_MODEL, default gemini-2.5-flash)
  anthropic      ANTHROPIC_API_KEY   (anthropic SDK; model ANTHROPIC_MODEL, default claude-haiku-4-5)
  openai_compat  LLM_BASE_URL + LLM_MODEL [+ LLM_API_KEY]  any OpenAI-compatible /chat/completions
                 endpoint, e.g. https://integrate.api.nvidia.com/v1 or a local Ollama http://localhost:11434/v1
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Callable, TypeVar

from pydantic import BaseModel, ValidationError

from . import obs

T = TypeVar("T", bound=BaseModel)
SYSTEM = ("You are a verification engineer for an FPGA synthesis tool. Answer ONLY with one JSON object "
          "that matches the given JSON schema. No prose, no markdown fences.")


def provider_name() -> str | None:
    forced = os.environ.get("LLM_PROVIDER")
    if forced:
        return forced
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("LLM_BASE_URL") and os.environ.get("LLM_MODEL"):
        return "openai_compat"
    return None


def _gemini(prompt: str) -> str:
    from google import genai

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    r = client.models.generate_content(
        model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"), contents=prompt,
        config={"system_instruction": SYSTEM, "response_mime_type": "application/json", "temperature": 0})
    return r.text or ""


def _anthropic(prompt: str) -> str:
    import anthropic

    client = anthropic.Anthropic()
    r = client.messages.create(model=os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5"), max_tokens=2000,
                               system=SYSTEM, messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")


def _openai_compat(prompt: str) -> str:
    url = os.environ["LLM_BASE_URL"].rstrip("/") + "/chat/completions"
    body = {"model": os.environ["LLM_MODEL"], "temperature": 0,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]}
    if os.environ.get("LLM_JSON_MODE", "1") == "1":
        body["response_format"] = {"type": "json_object"}
    # some hosts (for example Groq behind Cloudflare) reject the default Python-urllib user agent
    headers = {"Content-Type": "application/json", "User-Agent": "vagent/0.1"}
    if os.environ.get("LLM_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['LLM_API_KEY']}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    retries = int(os.environ.get("LLM_MAX_RETRIES", "8"))
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())["choices"][0]["message"]["content"] or ""
        except urllib.error.HTTPError as e:
            # free tiers rate-limit by tokens per minute; wait as told instead of falling back to rules
            if e.code != 429 or attempt == retries:
                raise
            wait = min(float(e.headers.get("retry-after") or 15), 90)
            obs.event("llm_rate_limited", wait_s=wait, attempt=attempt + 1)
            obs.inc("llm_rate_limited")
            time.sleep(wait)
    raise RuntimeError("unreachable")


PROVIDERS: dict[str, Callable[[str], str]] = {"gemini": _gemini, "anthropic": _anthropic,
                                              "openai_compat": _openai_compat}


def parse_structured(text: str, schema: type[T]) -> T:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    if not t.startswith("{"):
        m = re.search(r"\{.*\}", t, re.S)
        t = m.group(0) if m else t
    return schema.model_validate_json(t)


def structured(task: str, payload: dict, schema: type[T], call: Callable[[str], str] | None = None) -> T | None:
    """Ask the LLM for a `schema` object. Returns None (caller uses deterministic result) on any failure."""
    name = provider_name() if call is None else "injected"
    if call is None:
        if name is None or name not in PROVIDERS:
            obs.event("llm_unavailable", task=task, provider=name)
            return None
        call = PROVIDERS[name]
    prompt = (f"Task: {task}\n\nJSON schema:\n{json.dumps(schema.model_json_schema())}\n\n"
              f"Input:\n{json.dumps(payload, default=str)[:12000]}")
    try:
        raw = call(prompt)
    except Exception as e:  # network, auth, quota
        obs.event("llm_fallback", task=task, provider=name, reason=f"call failed: {type(e).__name__}: {e}"[:300])
        obs.inc("llm_fallbacks")
        return None
    try:
        out = parse_structured(raw, schema)
    except (ValidationError, ValueError) as e:
        obs.event("llm_fallback", task=task, provider=name, reason=f"invalid output: {str(e)[:300]}",
                  raw=raw[:500])
        obs.inc("llm_fallbacks")
        return None
    obs.event("llm_ok", task=task, provider=name)
    obs.inc("llm_ok")
    return out
