"""
Unified LLM caller for all content pipelines.

Provider chain (free tier):
    1. Kimi K2          via Groq          (moonshotai/kimi-k2-instruct, 30 RPM / 1000 RPD)
    2. Llama 3.3 70B    via Groq          (llama-3.3-70b-versatile,    30 RPM / 1000 RPD)
    3. Gemini 2.5 Flash via Google        (~250 RPD free)
    4. Kimi K2.6        via OpenRouter    (moonshotai/kimi-k2.6:free,  20 RPM / 50 RPD)
    5. Nemotron 550B    via OpenRouter    (nvidia/nemotron-3-ultra-550b-a55b:free)

Public API:
    call_llm(prompt, max_tokens=4096, temperature=0.7, system=None) -> str
    parse_json_response(text) -> dict | list

Drop-in aliases (preserve existing call sites during migration):
    call_claude(...)              -> call_llm(...)
    call_claude_for_reaction(...) -> call_llm(...)
    call_claude_for_reply(...)    -> call_llm(...)
    call_claude_json(...)         -> parse_json_response(call_llm(...))
"""

import os
import json
import time
import requests
from typing import Optional

# ── Models ─────────────────────────────────────────────────────────────────────

GROQ_PRIMARY = "moonshotai/kimi-k2-instruct"
GROQ_FALLBACK = "llama-3.3-70b-versatile"
GEMINI_MODEL = "gemini-2.5-flash"
OPENROUTER_PRIMARY = "moonshotai/kimi-k2.6:free"
OPENROUTER_FALLBACK = "nvidia/nemotron-3-ultra-550b-a55b:free"

# Set CONTENT_LLM_VERBOSE=1 to log which provider answered each call.
VERBOSE = os.environ.get("CONTENT_LLM_VERBOSE", "1") == "1"


def _log(msg: str) -> None:
    if VERBOSE:
        print(f"[llm] {msg}", flush=True)


# ── Provider implementations ───────────────────────────────────────────────────

def _call_groq(model: str, prompt: str, max_tokens: int, temperature: float,
               system: Optional[str]) -> str:
    """Call any Groq model. Raises on failure."""
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise EnvironmentError("GROQ_API_KEY not set")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
                timeout=90,
            )
            if resp.status_code == 429 and attempt < 2:
                wait = 30 * (attempt + 1)
                _log(f"Groq {model} 429, retry in {wait}s ({attempt+1}/3)")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            last_error = e
            if attempt < 2:
                time.sleep(5)
                continue
    raise last_error or RuntimeError(f"Groq {model} call failed")


def _call_gemini(prompt: str, max_tokens: int, system: Optional[str]) -> str:
    """Call Gemini 2.5 Flash. Raises on failure."""
    import google.generativeai as genai
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise EnvironmentError("GEMINI_API_KEY not set")
    genai.configure(api_key=api_key)
    kwargs = {"model_name": GEMINI_MODEL}
    if system:
        kwargs["system_instruction"] = system
    model = genai.GenerativeModel(**kwargs)

    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            resp = model.generate_content(
                prompt,
                generation_config=genai.GenerationConfig(max_output_tokens=max_tokens),
            )
            return resp.text
        except Exception as e:
            last_error = e
            if "429" in str(e) and attempt < 2:
                wait = 60 * (attempt + 1)
                _log(f"Gemini 429, retry in {wait}s ({attempt+1}/3)")
                time.sleep(wait)
                continue
            if attempt < 2:
                time.sleep(5)
                continue
            raise
    raise last_error or RuntimeError("Gemini call failed")


def _call_openrouter(model: str, prompt: str, max_tokens: int, temperature: float,
                     system: Optional[str]) -> str:
    """Call any OpenRouter model. Raises on failure."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise EnvironmentError("OPENROUTER_API_KEY not set")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "HTTP-Referer": "https://github.com/muneebnaseem786-stack/muneeb-content-engine",
                    "X-Title": "muneeb-content-engine",
                },
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
                timeout=120,
            )
            if resp.status_code == 429 and attempt < 2:
                wait = 60 * (attempt + 1)
                _log(f"OpenRouter {model} 429, retry in {wait}s ({attempt+1}/3)")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            if "choices" not in data or not data["choices"]:
                raise RuntimeError(f"OpenRouter empty response: {data}")
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            last_error = e
            if attempt < 2:
                time.sleep(5)
                continue
    raise last_error or RuntimeError(f"OpenRouter {model} call failed")


# ── Orchestrator ───────────────────────────────────────────────────────────────

def call_llm(prompt: str, max_tokens: int = 4096, temperature: float = 0.7,
             system: Optional[str] = None) -> str:
    """
    Try providers in order: Groq Kimi K2 → Groq Llama 3.3 → Gemini 2.5 Flash
    → OpenRouter Kimi K2.6 → OpenRouter Nemotron 550B. First success wins.
    """
    chain = [
        ("groq:kimi-k2", lambda: _call_groq(GROQ_PRIMARY, prompt, max_tokens, temperature, system)),
        ("groq:llama-3.3", lambda: _call_groq(GROQ_FALLBACK, prompt, max_tokens, temperature, system)),
        ("gemini:2.5-flash", lambda: _call_gemini(prompt, max_tokens, system)),
        ("openrouter:kimi-k2.6", lambda: _call_openrouter(OPENROUTER_PRIMARY, prompt, max_tokens, temperature, system)),
        ("openrouter:nemotron-550b", lambda: _call_openrouter(OPENROUTER_FALLBACK, prompt, max_tokens, temperature, system)),
    ]

    last_error: Optional[Exception] = None
    for label, fn in chain:
        try:
            result = fn()
            _log(f"OK via {label}")
            return result
        except Exception as e:
            _log(f"{label} failed: {type(e).__name__}: {str(e)[:200]}")
            last_error = e
            continue

    raise RuntimeError(f"All LLM providers failed. Last error: {last_error}")


# ── JSON helper ────────────────────────────────────────────────────────────────

def parse_json_response(text: str):
    """Strip ```json fences and parse. Tolerant of leading prose."""
    t = (text or "").strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    # Fallback: try to locate first { or [ if the model added a preamble.
    if not t.startswith("{") and not t.startswith("["):
        for opener in ("{", "["):
            idx = t.find(opener)
            if idx >= 0:
                t = t[idx:]
                break
    return json.loads(t)


# ── Drop-in aliases for existing call sites ────────────────────────────────────

def call_claude(prompt: str, max_tokens: int = 4096, model: str = None,
                temperature: float = 0.7, system: Optional[str] = None) -> str:
    """Legacy alias. `model` argument is accepted but ignored (chain decides)."""
    return call_llm(prompt, max_tokens=max_tokens, temperature=temperature, system=system)


def call_claude_for_reaction(prompt: str) -> str:
    return call_llm(prompt, max_tokens=4096, temperature=0.7)


def call_claude_for_reply(prompt: str) -> str:
    return call_llm(prompt, max_tokens=1024, temperature=0.8)


def call_claude_json(prompt: str, max_tokens: int = 4096):
    return parse_json_response(call_llm(prompt, max_tokens=max_tokens, temperature=0.7))
