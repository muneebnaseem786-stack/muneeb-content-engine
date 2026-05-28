"""Daily 10pm UAE nudge — list today's eToro drafts and ask which got posted.

Goal: prevent stale published_log.md by reminding the user, at end of day,
which routine drafts went out. The user replies to Claude (in chat) the next
session with which ones were actually posted to eToro, and Claude updates
published_log.md manually as before. This script only sends the reminder,
there is no reply parsing.

Triggered nightly via .github/workflows/etoro_published_log_checkin.yml.
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.content import etoro_common as ec  # noqa: E402


# routine archive prefix -> human label
ROUTINE_LABELS: list[tuple[str, str]] = [
    ("portfolio-alert", "Portfolio alert"),
    ("weekly-scan",     "Sunday Week Ahead"),
    ("thesis",          "Tuesday thesis"),
    ("eow-recap",       "Friday EOW recap"),
    ("monthly-review",  "Monthly review"),
]


def _extract_tickers(text: str) -> list[str]:
    """For portfolio-alert files, return tickers under `## Option N: $TICKER`."""
    return re.findall(r"^##\s+Option\s+\d+:\s+(\$[A-Z\.]+)\s*$", text, re.MULTILINE)


def _today_archives() -> list[tuple[str, str]]:
    """Return (label, summary) for archive files dated today / this month."""
    today = ec.uae_today()
    today_iso = today.strftime("%Y-%m-%d")
    month_iso = today.strftime("%Y-%m")
    items: list[tuple[str, str]] = []
    for prefix, label in ROUTINE_LABELS:
        if prefix == "monthly-review":
            path = ec.ARCHIVE_DIR / f"etoro-monthly-review-{month_iso}.md"
        else:
            path = ec.ARCHIVE_DIR / f"etoro-{prefix}-{today_iso}.md"
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if prefix == "portfolio-alert":
            tickers = _extract_tickers(text)
            summary = ", ".join(tickers) if tickers else "(no tickers parsed)"
        else:
            summary = path.name
        items.append((label, summary))
    return items


def build_message() -> str:
    today_iso = ec.uae_today().strftime("%Y-%m-%d")
    items = _today_archives()
    if not items:
        return (
            f"\U0001F514 <b>EOD eToro check-in, {today_iso}</b>\n\n"
            f"No routine drafts were generated today. If you posted anything to "
            f"eToro directly (outside the routines), tell Claude tomorrow so "
            f"<code>published_log.md</code> stays accurate."
        )
    lines = [f"\U0001F514 <b>EOD eToro check-in, {today_iso}</b>", "", "<b>Today's drafts:</b>"]
    for label, summary in items:
        lines.append(f"• {label}: {summary}")
    lines.append("")
    lines.append(
        "Tell Claude tomorrow which ones got posted (or none). Keeps "
        "<code>published_log.md</code> current so the radar can avoid rehashing."
    )
    return "\n".join(lines)


def run(dry_run: bool = False) -> int:
    msg = build_message()
    print(f"[etoro-published-log-checkin] message length: {len(msg)} chars")
    if dry_run:
        print("[etoro-published-log-checkin] --dry-run -> skipping Telegram send")
        print(msg)
        return 0
    ok = ec.tg_send(msg)
    print(f"[etoro-published-log-checkin] sent={ok}")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
