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


# ── Live portfolio from the eToro public API ──────────────────────────────────

ETORO_API_BASE = "https://public-api.etoro.com"
ETORO_USERNAME = "muneebnaseem"
INSTRUMENTS_CACHE = ETORO_DATA / "instruments_cache.json"


def _etoro_api_get(path: str) -> dict:
    import uuid

    resp = requests.get(
        ETORO_API_BASE + path,
        headers={
            "x-user-key": os.environ["ETORO_USER_KEY"],
            "x-api-key": os.environ["ETORO_API_KEY"],
            "x-request-id": str(uuid.uuid4()),
            # default python UA is WAF-blocked (403)
            "User-Agent": "curl/8.9.1",
            "Accept": "application/json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _instrument_names(ids) -> dict:
    cache = {}
    if INSTRUMENTS_CACHE.exists():
        cache = json.loads(INSTRUMENTS_CACHE.read_text(encoding="utf-8"))
    missing = [str(i) for i in ids if str(i) not in cache]
    if missing:
        data = _etoro_api_get(
            "/api/v1/market-data/instruments?instrumentIds=" + ",".join(missing)
        )
        for d in data.get("instrumentDisplayDatas", []):
            cache[str(d["instrumentID"])] = {
                "symbol": d.get("symbolFull", ""),
                "name": d.get("instrumentDisplayName", ""),
            }
        try:
            INSTRUMENTS_CACHE.write_text(json.dumps(cache, indent=1), encoding="utf-8")
        except OSError:
            pass
    return cache


def fetch_live_portfolio() -> str:
    """Live per-instrument portfolio snapshot from the eToro public API,
    rendered as a markdown block for prompt injection. Returns "" when the
    API keys are absent or any call fails, so callers can fall back to the
    curated portfolio.md alone."""
    if not (os.environ.get("ETORO_USER_KEY") and os.environ.get("ETORO_API_KEY")):
        return ""
    try:
        pf = _etoro_api_get(f"/api/v1/user-info/people/{ETORO_USERNAME}/portfolio/live")
        positions = pf.get("positions", [])
        if not positions:
            return ""

        agg: dict = {}
        for p in positions:
            a = agg.setdefault(p["instrumentId"], {"inv": 0.0, "wOpen": 0.0, "wPnl": 0.0, "lots": 0})
            w = p["investmentPct"]
            a["inv"] += w
            a["wOpen"] += p["openRate"] * w
            a["wPnl"] += p["netProfit"] * w  # netProfit is percent per position
            a["lots"] += 1

        names = _instrument_names(agg.keys())
        rates = {}
        ids = list(agg.keys())
        for i in range(0, len(ids), 100):
            chunk = ",".join(str(x) for x in ids[i : i + 100])
            data = _etoro_api_get("/api/v1/market-data/instruments/rates?instrumentIds=" + chunk)
            for r in data.get("rates", []):
                rates[r["instrumentID"]] = r.get("lastExecution") or r.get("bid")

        rows = []
        for iid, a in agg.items():
            inv = a["inv"]
            pnl = a["wPnl"] / inv if inv else 0.0
            meta = names.get(str(iid), {})
            rows.append({
                "symbol": meta.get("symbol", str(iid)),
                "inv": inv,
                "pnl": pnl,
                "open": a["wOpen"] / inv if inv else 0.0,
                "last": rates.get(iid),
                "lots": a["lots"],
            })
        rows.sort(key=lambda r: -r["inv"])
        grown = [r["inv"] * (1 + r["pnl"] / 100) for r in rows]
        total_grown = sum(grown) or 1.0

        fetched = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            f"## LIVE PORTFOLIO SNAPSHOT (eToro API, fetched {fetched})",
            "",
            "This snapshot is AUTHORITATIVE for current position figures: invested %,",
            "value %, P/L %, and prices. The curated sections above remain authoritative",
            "for theses, stances, and internal levels. If a figure here conflicts with a",
            "figure above, use this snapshot.",
            "",
            f"Open positions: {len(positions)} across {len(rows)} instruments.",
            "",
            "| Symbol | Invested % | Value % (est) | P/L % | Avg open | Last price | Lots |",
            "|---|---|---|---|---|---|---|",
        ]
        for r, g in zip(rows, grown):
            lines.append(
                f"| {r['symbol']} | {r['inv']:.2f} | {100 * g / total_grown:.2f} | "
                f"{r['pnl']:.1f} | {r['open']:.2f} | {r['last']} | {r['lots']} |"
            )
        return "\n".join(lines)
    except Exception as e:
        print(f"[etoro] live portfolio fetch failed ({e}); falling back to portfolio.md only")
        return ""


# ── Brain context ─────────────────────────────────────────────────────────────

def load_brain() -> dict:
    """Read the eToro brain files and return them keyed for prompt use.

    `published_log` is the rolling log of the last 4-6 actually-posted eToro
    posts (full text). Every routine should consume it to avoid rehashing
    what was already led. Falls back to a short stub if the file is missing.
    """
    published_log_path = ETORO_DATA / "published_log.md"
    published_log = (
        published_log_path.read_text(encoding="utf-8")
        if published_log_path.exists()
        else "(no published log yet — first runs cannot rehash anything)"
    )

    voice = (ETORO_DATA / "voice.md").read_text(encoding="utf-8")

    # Mirror of Claude Code's persistent memory. The cloud runner cannot read
    # that memory directly (it lives outside this repo), so the writing and
    # strategy feedback is mirrored into data/etoro/learnings.md and folded into
    # the voice context here. Every eToro routine injects {voice}, so this makes
    # all of them read the learned rules before drafting. Keep learnings.md in
    # sync whenever a new writing/voice/eToro lesson is added to memory.
    learnings_path = ETORO_DATA / "learnings.md"
    learnings = learnings_path.read_text(encoding="utf-8") if learnings_path.exists() else ""
    if learnings:
        voice = f"{voice}\n\n{learnings}"

    portfolio = (ETORO_DATA / "portfolio.md").read_text(encoding="utf-8")
    live = fetch_live_portfolio()
    if live:
        portfolio = f"{portfolio}\n\n{live}"

    return {
        "portfolio": portfolio,
        "voice": voice,
        "platform": (ETORO_DATA / "platform.md").read_text(encoding="utf-8"),
        "published_log": published_log,
        "learnings": learnings,
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
    """Gemini 2.5 Flash via the new google-genai SDK. If grounded=True, enables
    Google Search grounding so the model has access to fresh web content."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    config = None
    if grounded:
        # Gemini 2.x grounding: pass a Tool object with GoogleSearch, not a
        # bare string. The legacy google-generativeai SDK only accepts
        # "code_execution" as a string tool; everything else must be a Tool.
        config = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())]
        )

    last_error: Exception | None = None
    transient_markers = ("429", "500", "502", "503", "504", "UNAVAILABLE", "RESOURCE_EXHAUSTED")
    backoffs = [5, 15, 30, 60, 120]  # ~4 min total budget for high-demand spikes
    for attempt in range(len(backoffs) + 1):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=config,
            )
            return response.text
        except Exception as e:
            last_error = e
            err = str(e)
            transient = any(m in err for m in transient_markers)
            if attempt < len(backoffs) and transient:
                wait = backoffs[attempt]
                print(f"[etoro] Gemini transient ({err[:120]}), retry {attempt + 1}/{len(backoffs)} in {wait}s")
                time.sleep(wait)
                continue
            raise
    raise last_error or RuntimeError("Gemini call failed")


def _call_groq(prompt: str, max_tokens: int = 4096) -> str:
    """Groq Llama 3.3 70B fallback. No grounding -- relies on context in prompt."""
    api_key = os.environ["GROQ_API_KEY"]
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": "llama-3.3-70b-versatile",
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
    if not text.strip():
        return

    summary = extract_section(text, "Summary").strip()
    options = re.findall(
        r"^##\s+Option\s+\d+:\s+(\$[A-Z\.]+)\s*\n(.*?)(?=^##\s|\Z)",
        text,
        flags=re.DOTALL | re.MULTILINE,
    )

    # Quiet-day suppression applies ONLY when there are no draft options. The
    # per-ticker Triage lines ("$X: No material news found.") would otherwise
    # match here and silently kill an alert that has real drafts, so test the
    # Summary/body, never the full document.
    empty_signals = ["no material news", "nothing to report", "no items today"]
    if not options:
        body = (summary or text).strip()
        if not body or any(s in body.lower() for s in empty_signals):
            return
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
