"""eToro Telegram Relay.

Reads an eToro scheduled-task output file and pushes the relevant sections to
the user's Telegram chat in the 2-message pattern used by Reaction/Reply/
Substack radars (context first, copy-paste content second).

Supported file patterns (matched by filename prefix):

  etoro-weekly-scan-YYYY-MM-DD.md       → Sunday Week Ahead prep
  etoro-eow-recap-YYYY-MM-DD.md         → Saturday End-of-Week recap
  etoro-monthly-review-YYYY-MM.md       → Monthly review (fires 1st of month)
  etoro-portfolio-alert-YYYY-MM-DD.md   → Daily portfolio news radar
  etoro-thesis-YYYY-MM-DD.md            → Tuesday thesis or framework post

Usage: python etoro_telegram_relay.py <absolute_path_to_md_file>

Environment:
  TELEGRAM_BOT_TOKEN   required
  TELEGRAM_CHAT_ID     optional, defaults to user's known chat id 7056858166
"""

import os
import re
import sys
import time
import requests
from pathlib import Path


def _load_env_from_file_if_missing() -> None:
    """Populate TELEGRAM_BOT_TOKEN (and friends) from the local content-engine
    .env file if not already set in the process environment.

    Loads from content-engine/.env (which is gitignored). This must be the
    Muneeb content bot token, NOT the F&R bot. F&R has its own local .env
    in automation/fortune_ruin/.env; do not fall back to it here.
    """
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        return
    # content-engine/scripts/content/etoro_telegram_relay.py → content-engine/.env
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_env_from_file_if_missing()

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = (
    os.environ.get("TELEGRAM_CHAT_ID")
    or os.environ.get("YOUR_TELEGRAM_CHAT_ID")
    or "7056858166"
)

if not BOT_TOKEN:
    sys.exit("TELEGRAM_BOT_TOKEN not set in environment or .env file.")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TELEGRAM_MAX = 4000  # leave headroom under the 4096 limit


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _send(text: str) -> bool:
    """Send a single Telegram message. Splits if over the limit."""
    if len(text) <= TELEGRAM_MAX:
        return _post(text)
    # Split on paragraph boundaries when possible.
    chunks = []
    remaining = text
    while len(remaining) > TELEGRAM_MAX:
        cut = remaining.rfind("\n\n", 0, TELEGRAM_MAX)
        if cut == -1:
            cut = TELEGRAM_MAX
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    ok = True
    for i, chunk in enumerate(chunks):
        suffix = f"\n\n<i>({i + 1}/{len(chunks)})</i>" if len(chunks) > 1 else ""
        ok = _post(chunk + suffix) and ok
        time.sleep(0.4)
    return ok


def _post(text: str) -> bool:
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    resp = requests.post(f"{API}/sendMessage", json=payload, timeout=15)
    if resp.status_code != 200:
        print(f"Telegram send failed [{resp.status_code}]: {resp.text}")
        return False
    return True


def _extract_section(text: str, header: str) -> str:
    """Extract content under a `## {header}` heading until the next `## ` heading or EOF."""
    pattern = rf"^##\s+{re.escape(header)}\s*\n(.*?)(?=^##\s|\Z)"
    m = re.search(pattern, text, re.DOTALL | re.MULTILINE)
    return m.group(1).strip() if m else ""


def _has_meaningful_content(text: str) -> bool:
    stripped = text.strip().lower()
    if not stripped:
        return False
    empty_signals = ["no material news", "nothing to report", "no items today"]
    return not any(signal in stripped for signal in empty_signals)


_FILE_POINTER = "\n\n<i>Full triage, events, and market data are in the source file.</i>"


def relay_weekly_scan(text: str) -> None:
    summary = _extract_section(text, "Quick Summary for User")
    draft = _extract_section(text, "Draft Sunday Post")

    if draft:
        _send("<b>Week Ahead draft (copy to eToro):</b>\n\n<pre>" + _esc(draft) + "</pre>")
    if summary:
        _send("<b>Notes</b>\n\n<pre>" + _esc(summary) + "</pre>" + _FILE_POINTER)


def relay_eow_recap(text: str) -> None:
    notes = _extract_section(text, "Notes for User")
    draft = _extract_section(text, "Draft Saturday Post")

    if draft:
        _send("<b>EOW recap draft (copy to eToro):</b>\n\n<pre>" + _esc(draft) + "</pre>")
    if notes:
        _send("<b>Notes</b>\n\n<pre>" + _esc(notes) + "</pre>" + _FILE_POINTER)


def relay_monthly_review(text: str) -> None:
    notes = _extract_section(text, "Notes for User")
    draft = _extract_section(text, "Draft Monthly Review Post")

    if draft:
        _send("<b>Monthly review draft (copy to eToro):</b>\n\n<pre>" + _esc(draft) + "</pre>")
    if notes:
        _send("<b>Notes</b>\n\n<pre>" + _esc(notes) + "</pre>" + _FILE_POINTER)


def relay_tuesday_thesis(text: str) -> None:
    """Tuesday thesis or framework post draft.

    File format:
      ## Topic
      ## Research Notes
      ## Draft Tuesday Post     ← pushed as copy-paste draft
      ## Notes for User         ← pushed as short notes message
      ## Sources                ← archive only, not pushed
    """
    topic = _extract_section(text, "Topic")
    notes = _extract_section(text, "Notes for User")
    draft = _extract_section(text, "Draft Tuesday Post")

    header_topic = topic.splitlines()[0].strip() if topic else "Tuesday thesis"

    if draft:
        _send(
            "<b>Tuesday thesis draft — " + _esc(header_topic) + " (copy to eToro):</b>"
            "\n\n<pre>" + _esc(draft) + "</pre>"
        )
    if notes:
        _send("<b>Notes</b>\n\n<pre>" + _esc(notes) + "</pre>" + _FILE_POINTER)


def relay_portfolio_alert(text: str) -> None:
    """Daily radar file format (new, since 2026-05-16):

      ## Summary
      ... brief summary lines ...

      ## Option 1: $TICKER
      **Catalyst (last 24h):** ...
      **Source:** https://...
      ```post
      <ready-to-paste draft>
      ```

      ## Option 2: $TICKER
      ...

      ## Triage
      ... internal notes, NOT pushed to Telegram ...

    Relay behaviour:
      - 1 short summary message (the `## Summary` section content)
      - 1 tap-to-copy draft message per `## Option N: $TICKER` section,
        containing only the fenced ```post block content as <pre>
      - Triage section is skipped (archive only)
      - If neither summary nor options exist, the file is treated as quiet
        and we send a single short heads-up.

    Legacy fallback: if the file has the old `### $TICKER` per-holding shape
    and no `## Option` sections, we send a single combined message rather
    than spamming one per ticker.
    """
    if not _has_meaningful_content(text):
        return

    summary = _extract_section(text, "Summary").strip()

    options = re.findall(
        r"^##\s+Option\s+\d+:\s+(\$[A-Z\.]+)\s*\n(.*?)(?=^##\s|\Z)",
        text,
        flags=re.DOTALL | re.MULTILINE,
    )

    if not options:
        # Legacy or malformed file. Send one combined message, not 20.
        body = summary or text.strip()
        _send("<b>eToro Portfolio Radar</b>\n\n<pre>" + _esc(body) + "</pre>")
        return

    if summary:
        _send("<b>eToro Portfolio Radar — " + str(len(options)) + " draft(s) below</b>\n\n<pre>" + _esc(summary) + "</pre>")

    for ticker, section_body in options:
        draft_match = re.search(r"```post\s*\n(.*?)\n```", section_body, re.DOTALL)
        if not draft_match:
            # Fallback: try any fenced block
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

        _send("\n".join(header_lines) + "\n\n<pre>" + _esc(draft) + "</pre>")
        time.sleep(0.4)


HANDLERS = [
    ("etoro-weekly-scan-", relay_weekly_scan),
    ("etoro-eow-recap-", relay_eow_recap),
    ("etoro-monthly-review-", relay_monthly_review),
    ("etoro-portfolio-alert-", relay_portfolio_alert),
    ("etoro-thesis-", relay_tuesday_thesis),
]


def main(filepath: str) -> int:
    path = Path(filepath)
    if not path.exists():
        print(f"File not found: {filepath}")
        return 1
    text = path.read_text(encoding="utf-8")
    for prefix, handler in HANDLERS:
        if path.name.startswith(prefix):
            handler(text)
            print(f"Relayed: {path.name}")
            return 0
    print(f"Unrecognized eToro file pattern: {path.name}")
    return 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python etoro_telegram_relay.py <filepath>")
    sys.exit(main(sys.argv[1]))
