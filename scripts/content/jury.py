"""Editorial jury — evaluates generated content against a rubric before it ships.

Each radar generates candidates with the primary LLM (Gemini), then calls
judge(...) to score the result on relevance, voice match, and compliance.
The jury runs on Groq (Llama 3.3 70B) by default to avoid same-model bias —
the model that wrote the content does not judge its own work.

Verdict shape:
{
  "relevance":      1-5,
  "voice_match":    1-5,
  "compliance":     "PASS" | "FAIL",
  "violations":     [<short strings>],
  "verdict":        "PASS" | "REVISE" | "REJECT",
  "verdict_reason": "<one sentence>"
}

Verdict rules (enforced by the rubric prompt, but callers should also handle):
  - REJECT → drop the candidate, try the next one
  - REVISE → re-generate once with violations in context, then ship-or-skip
  - PASS   → ship to Telegram; verdict card attached so user sees the "why"

Fail-open: if the jury LLM call itself fails (network, quota), we let the
content through with `verdict_reason: "jury_error: ..."` rather than block
the whole pipeline on a flaky judge.
"""

import json
import os
import time
from pathlib import Path

import requests


def _call_groq_strict(prompt: str, max_tokens: int = 1024) -> str:
    """Call Groq Llama 3.3 70B with low temperature for judgment tasks.
    Uses JSON object response format so the output is parseable."""
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise EnvironmentError("GROQ_API_KEY not set — jury cannot run.")
    last_error = None
    for attempt in range(3):
        try:
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": "llama-3.3-70b-versatile",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                    "temperature": 0.2,
                    "response_format": {"type": "json_object"},
                },
                timeout=60,
            )
            if resp.status_code == 429 and attempt < 2:
                time.sleep(30 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            last_error = e
            if attempt < 2:
                time.sleep(5)
                continue
    raise last_error or RuntimeError("Groq jury call failed")


def _extract_json(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        lines = t.split("\n")
        t = "\n".join(lines[1:-1] if lines and lines[-1].strip() == "```" else lines[1:])
        t = t.strip()
        if t.startswith("json"):
            t = t[4:].strip()
    start = t.find("{")
    end = t.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found:\n{text[:300]}")
    return json.loads(t[start:end + 1])


def judge(rubric_path: Path, **context) -> dict:
    """Run the jury on a generated piece of content.

    Args:
      rubric_path: Path to a jury_*.txt rubric (format-string template).
      **context:   Fields to fill into the rubric template (source, generated, etc.).

    Returns:
      A verdict dict (see module docstring). Always returns — never raises —
      so the caller's pipeline does not crash if the jury LLM is unavailable.
    """
    try:
        rubric = rubric_path.read_text(encoding="utf-8")
        prompt = rubric.format(**context)
    except (FileNotFoundError, KeyError) as e:
        return _fail_open(f"rubric_error: {e}")

    try:
        raw = _call_groq_strict(prompt)
        verdict = _extract_json(raw)
    except Exception as e:
        return _fail_open(f"jury_error: {e}")

    # Defensive normalisation — make sure the caller always gets the same keys
    return {
        "relevance":      int(verdict.get("relevance", 0) or 0),
        "voice_match":    int(verdict.get("voice_match", 0) or 0),
        "compliance":     str(verdict.get("compliance", "UNKNOWN")).upper(),
        "violations":     list(verdict.get("violations", []) or []),
        "verdict":        str(verdict.get("verdict", "PASS")).upper(),
        "verdict_reason": str(verdict.get("verdict_reason", "")),
    }


def _fail_open(reason: str) -> dict:
    """Default verdict when the jury cannot run — lets content through
    with the error tagged in verdict_reason so it shows up in Telegram."""
    return {
        "relevance":      0,
        "voice_match":    0,
        "compliance":     "UNKNOWN",
        "violations":     [],
        "verdict":        "PASS",
        "verdict_reason": reason,
    }


def format_verdict_card(verdict: dict) -> str:
    """One-line summary suitable for appending to a Telegram message."""
    v = verdict.get("verdict", "PASS")
    icon = {"PASS": "✅", "REVISE": "🟡", "REJECT": "🔴"}.get(v, "⚪")
    rel = verdict.get("relevance", 0)
    voice = verdict.get("voice_match", 0)
    reason = verdict.get("verdict_reason", "") or ""
    violations = verdict.get("violations") or []
    line = f"{icon} Jury: {v} · relevance {rel}/5 · voice {voice}/5"
    if reason:
        line += f"\n   {reason}"
    if violations:
        line += f"\n   ⚠ {'; '.join(violations[:3])}"
    return line
