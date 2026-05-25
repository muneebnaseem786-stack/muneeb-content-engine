"""Shared helpers for the eToro cloud tasks (sunday-prep, friday-recap,
monthly-review, tuesday-thesis, portfolio-alert).

Mirrors the markets_radar pattern: Gemini primary -> Groq fallback for LLM
calls, direct Telegram HTTP for delivery, brain context loaded from
content-engine/data/etoro/ inside the checked-out repo.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


REPO_ROOT = Path(__file__).resolve().parents[2]
ETORO_DATA = REPO_ROOT / "data" / "etoro"
ARCHIVE_DIR = ETORO_DATA / "archive"


# ── Brain context ─────────────────────────────────────────────────────────────

def load_brain() -> dict:
    """Read the three eToro brain files and return them keyed for prompt use."""
    return {
        "portfolio": (ETORO_DATA / "portfolio.md").read_text(encoding="utf-8"),
        "voice": (ETORO_DATA / "voice.md").read_text(encoding="utf-8"),
        "platform": (ETORO_DATA / "platform.md").read_text(encoding="utf-8"),
    }


def load_recent_archive(prefix: str, limit: int = 10) -> list[tuple[str, str]]:
    """Return (filename, content) for the most recent archive files matching
    `etoro-{prefix}-*.md`, newest first."""
    pattern = f"etoro-{prefix}-*.md"
    files = sorted(ARCHIVE_DIR.glob(pattern), reverse=True)[:limit]
    return [(f.name, f.read_text(encoding="utf-8")) for f in files]


# ── Telegram ──────────────────────────────────────────────────────────────────

_TELEGRAM_MAX = 4000


def _tg_token() -> str:
    return os.environ["TELEGRAM_BOT_TOKEN"]


def _tg_chat_id() -> str:
    return os.environ.get("TELEGRAM_CHAT_ID") or "7056858166"


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tg_send(text: str) -> bool:
    """Send one Telegram message in HTML mode, splitting on paragraph boundaries
    if it exceeds the 4096-char limit."""
    if len(text) <= _TELEGRAM_MAX:
        return _tg_post(text)
    chunks = []
    remaining = text
    while len(remaining) > _TELEGRAM_MAX:
        cut = remaining.rfind("\n\n", 0, _TELEGRAM_MAX)
        if cut == -1:
            cut = _TELEGRAM_MAX
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    ok = True
    for i, chunk in enumerate(chunks):
        suffix = f"\n\n<i>({i + 1}/{len(chunks)})</i>" if len(chunks) > 1 else ""
        ok = _tg_post(chunk + suffix) and ok
        time.sleep(0.4)
    return ok


def _tg_post(text: str) -> bool:
    payload = {
        "chat_id": _tg_chat_id(),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{_tg_token()}/sendMessage",
            json=payload,
            timeout=15,
        )
        if resp.status_code == 200:
            return True
        print(f"[etoro] Telegram send failed [{resp.status_code}]: {resp.text[:300]}")
    except Exception as e:
        print(f"[etoro] Telegram send error: {e}")
    return False


# ── LLM ───────────────────────────────────────────────────────────────────────

def _call_gemini(prompt: str, grounded: bool = False) -> str:
    """Gemini 2.5 Flash. If grounded=True, enable Google Search grounding so the
    model has access to fresh web content."""
    import google.generativeai as genai

    genai.configure(api_key=os.environ["GEMINI_API_KEY"])

    tools = None
    if grounded:
        # google-generativeai >= 0.8 supports the simple string form.
        tools = "google_search_retrieval"

    model = genai.GenerativeModel("gemini-2.5-flash", tools=tools)

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = model.generate_content(prompt)
            return response.text
        except Exception as e:
            last_error = e
            if "429" in str(e) and attempt < 2:
                wait = 60 * (attempt + 1)
                print(f"[etoro] Gemini 429, retry in {wait}s ({attempt + 1}/3)")
                time.sleep(wait)
                continue
            if attempt < 2:
                time.sleep(5)
                continue
    raise last_error or RuntimeError("Gemini call failed")


def _call_groq(prompt: str, max_tokens: int = 4096) -> str:
    """Groq Kimi K2 fallback. No grounding -- relies on context in prompt."""
    api_key = os.environ["GROQ_API_KEY"]
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": "moonshotai/kimi-k2-instruct",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                    "temperature": 0.6,
                },
                timeout=120,
            )
            if resp.status_code == 429 and attempt < 2:
                wait = 30 * (attempt + 1)
                print(f"[etoro] Groq 429, retry in {wait}s ({attempt + 1}/3)")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            last_error = e
            if attempt < 2:
                time.sleep(5)
                continue
    raise last_error or RuntimeError("Groq call failed")


def call_llm(prompt: str, grounded: bool = False) -> str:
    """Gemini primary -> Groq fallback. Grounded flag only applies to Gemini;
    Groq falls back to plain prompt context."""
    if os.environ.get("GEMINI_API_KEY"):
        try:
            return _call_gemini(prompt, grounded=grounded)
        except Exception as e:
            print(f"[etoro] Gemini failed: {e}. Falling back to Groq.")
    return _call_groq(prompt)


# ── Archive + git commit ──────────────────────────────────────────────────────

def write_archive(filename: str, content: str) -> Path:
    """Write a dated archive file under data/etoro/archive/. Returns absolute path."""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = ARCHIVE_DIR / filename
    path.write_text(content, encoding="utf-8")
    print(f"[etoro] Wrote archive: {path}")
    return path


def commit_archive(path: Path, message: str) -> None:
    """Stage and commit the archive file. Workflow YAML still runs `git push`
    after the script exits, so we only commit here. No-op when running locally
    outside a git context (CI sets GITHUB_ACTIONS=true)."""
    if not os.environ.get("GITHUB_ACTIONS"):
        print(f"[etoro] Skipping commit (not in GitHub Actions): {path}")
        return
    import subprocess

    subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
    subprocess.run(
        ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
        check=True,
    )
    rel = path.relative_to(REPO_ROOT).as_posix()
    subprocess.run(["git", "add", rel], check=True)
    result = subprocess.run(
        ["git", "diff", "--staged", "--quiet"], check=False
    )
    if result.returncode == 0:
        print("[etoro] No changes to commit.")
        return
    subprocess.run(["git", "commit", "-m", f"{message} [skip ci]"], check=True)
    subprocess.run(["git", "pull", "--rebase", "--autostash"], check=False)
    subprocess.run(["git", "push"], check=True)
    print(f"[etoro] Committed + pushed: {rel}")


# ── Date helpers ──────────────────────────────────────────────────────────────

def uae_today() -> datetime:
    """Current date/time in UAE (UTC+4). Used so cron day-of-week assumptions
    line up with the user's perspective even though GH Actions runs UTC."""
    from datetime import timedelta
    return datetime.now(timezone.utc) + timedelta(hours=4)


def date_str(d: datetime | None = None) -> str:
    return (d or uae_today()).strftime("%Y-%m-%d")


# ── Markdown section extraction (for the relay handlers) ──────────────────────

def extract_section(text: str, header: str) -> str:
    """Extract content under a `## {header}` heading until the next `## ` or EOF."""
    pattern = rf"^##\s+{re.escape(header)}\s*\n(.*?)(?=^##\s|\Z)"
    m = re.search(pattern, text, re.DOTALL | re.MULTILINE)
    return m.group(1).strip() if m else ""


_FILE_POINTER = "\n\n<i>Full triage, events, and market data are in the archive file on GitHub.</i>"


def relay_weekly_scan(text: str) -> None:
    summary = extract_section(text, "Quick Summary for User")
    draft = extract_section(text, "Draft Sunday Post")
    if draft:
        tg_send("<b>Week Ahead draft (copy to eToro):</b>\n\n<pre>" + _esc(draft) + "</pre>")
    if summary:
        tg_send("<b>Notes</b>\n\n<pre>" + _esc(summary) + "</pre>" + _FILE_POINTER)


def relay_eow_recap(text: str) -> None:
    notes = extract_section(text, "Notes for User")
    draft = extract_section(text, "Draft Saturday Post")
    if draft:
        tg_send("<b>EOW recap draft (copy to eToro):</b>\n\n<pre>" + _esc(draft) + "</pre>")
    if notes:
        tg_send("<b>Notes</b>\n\n<pre>" + _esc(notes) + "</pre>" + _FILE_POINTER)


def relay_monthly_review(text: str) -> None:
    notes = extract_section(text, "Notes for User")
    draft = extract_section(text, "Draft Monthly Review Post")
    if draft:
        tg_send("<b>Monthly review draft (copy to eToro):</b>\n\n<pre>" + _esc(draft) + "</pre>")
    if notes:
        tg_send("<b>Notes</b>\n\n<pre>" + _esc(notes) + "</pre>" + _FILE_POINTER)


def relay_tuesday_thesis(text: str) -> None:
    topic = extract_section(text, "Topic")
    notes = extract_section(text, "Notes for User")
    draft = extract_section(text, "Draft Tuesday Post")
    header_topic = topic.splitlines()[0].strip() if topic else "Tuesday thesis"
    if draft:
        tg_send(
            "<b>Tuesday thesis draft — " + _esc(header_topic) + " (copy to eToro):</b>"
            "\n\n<pre>" + _esc(draft) + "</pre>"
        )
    if notes:
        tg_send("<b>Notes</b>\n\n<pre>" + _esc(notes) + "</pre>" + _FILE_POINTER)


def relay_portfolio_alert(text: str) -> None:
    """Daily radar handler matching etoro_telegram_relay.relay_portfolio_alert."""
    stripped = text.strip().lower()
    if not stripped:
        return
    empty_signals = ["no material news", "nothing to report", "no items today"]
    if any(s in stripped for s in empty_signals):
        return

    summary = extract_section(text, "Summary").strip()
    options = re.findall(
        r"^##\s+Option\s+\d+:\s+(\$[A-Z\.]+)\s*\n(.*?)(?=^##\s|\Z)",
        text,
        flags=re.DOTALL | re.MULTILINE,
    )

    if not options:
        body = summary or text.strip()
        tg_send("<b>eToro Portfolio Radar</b>\n\n<pre>" + _esc(body) + "</pre>")
        return

    if summary:
        tg_send(
            "<b>eToro Portfolio Radar — " + str(len(options)) + " draft(s) below</b>"
            "\n\n<pre>" + _esc(summary) + "</pre>"
        )

    for ticker, section_body in options:
        draft_match = re.search(r"```post\s*\n(.*?)\n```", section_body, re.DOTALL)
        if not draft_match:
            draft_match = re.search(r"```[a-zA-Z]*\s*\n(.*?)\n```", section_body, re.DOTALL)
        if not draft_match:
            continue
        draft = draft_match.group(1).strip()
        catalyst_match = re.search(r"\*\*Catalyst[^*]*\*\*\s*(.+)", section_body)
        source_match = re.search(r"\*\*Source:\*\*\s*(\S+)", section_body)

        header_lines = [f"<b>Option: {_esc(ticker)} — tap-and-hold to copy</b>"]
        if catalyst_match:
            header_lines.append(_esc(catalyst_match.group(1).strip()))
        if source_match:
            header_lines.append("Source: " + _esc(source_match.group(1).strip()))

        tg_send("\n".join(header_lines) + "\n\n<pre>" + _esc(draft) + "</pre>")
        time.sleep(0.4)


# ── LLM output cleanup ────────────────────────────────────────────────────────

def strip_em_dashes(s: str) -> str:
    """The voice rule bans em dashes everywhere. Belt and suspenders -- the
    prompt says no but LLMs slip. Replace em/en dashes with commas."""
    return s.replace("—", ", ").replace("–", ", ")
