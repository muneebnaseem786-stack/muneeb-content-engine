"""eToro daily portfolio alert — cloud version.

Replaces the local mcp__scheduled-tasks etoro-portfolio-alert SKILL and the
prior (broken) Anthropic cloud routine. Runs Mon-Sat 8am UAE on GitHub
Actions. Reads yesterday's alert from archive to enforce anti-repeat, scans
top-22 holdings for 24h material news via Gemini grounding, drafts 0/1/2
ready-to-post eToro posts in voice.

Usage:
  python -m scripts.content.etoro_portfolio_alert              # full run
  python -m scripts.content.etoro_portfolio_alert --dry-run    # build prompt only
"""

import argparse
import re
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.content import etoro_common as ec  # noqa: E402


PROMPT_PATH = ec.REPO_ROOT / "prompts" / "etoro_portfolio_alert_prompt.txt"


def _yesterday_excluded_tickers() -> list[str]:
    """Read yesterday's alert file from archive and extract tickers under
    `## Option 1: $TICKER` and `## Option 2: $TICKER`. Empty list if no file."""
    yesterday = ec.uae_today() - timedelta(days=1)
    yesterday_iso = yesterday.strftime("%Y-%m-%d")
    path = ec.ARCHIVE_DIR / f"etoro-portfolio-alert-{yesterday_iso}.md"
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    matches = re.findall(r"^##\s+Option\s+\d+:\s+(\$[A-Z\.]+)\s*$", text, re.MULTILINE)
    return matches


def build_prompt() -> tuple[str, str]:
    brain = ec.load_brain()
    today = ec.uae_today()
    today_iso = today.strftime("%Y-%m-%d")

    excluded = _yesterday_excluded_tickers()
    if excluded:
        excluded_tickers = ", ".join(excluded)
        excluded_for_display = ", ".join(excluded)
    else:
        excluded_tickers = "(none — first run or yesterday had no alert file)"
        excluded_for_display = "none — first run / legacy file"

    template = PROMPT_PATH.read_text(encoding="utf-8")
    prompt = template.format(
        today_iso=today_iso,
        excluded_tickers=excluded_tickers,
        excluded_tickers_for_display=excluded_for_display,
        portfolio=brain["portfolio"],
        voice=brain["voice"],
        platform=brain["platform"],
    )
    return prompt, today_iso


def run(dry_run: bool = False) -> int:
    prompt, today_iso = build_prompt()
    print(f"[etoro-portfolio-alert] Today UAE: {today_iso}, prompt length: {len(prompt)} chars")

    if dry_run:
        print("[etoro-portfolio-alert] --dry-run -> skipping LLM + Telegram + commit")
        print(prompt[:2000])
        return 0

    print("[etoro-portfolio-alert] Calling Gemini (grounded) -> Groq fallback...")
    try:
        raw = ec.call_llm(prompt, grounded=True)
    except Exception as e:
        print(f"[etoro-portfolio-alert] LLM call failed: {e}")
        ec.tg_send(f"🔴 eToro Portfolio Alert — LLM error\n{str(e)[:400]}")
        return 2

    cleaned = ec.strip_em_dashes(raw).strip()
    # Outer wrapper fence (different from the inner ```post fences we want to keep)
    if cleaned.startswith("```") and not cleaned.startswith("```post"):
        first_nl = cleaned.find("\n")
        if first_nl != -1:
            first_line = cleaned[:first_nl]
            # Only strip if first line is ```<lang> (single word after fence)
            if re.match(r"^```[a-zA-Z]*\s*$", first_line):
                cleaned = cleaned[first_nl + 1:]
                if cleaned.rstrip().endswith("```"):
                    cleaned = cleaned.rstrip()[:-3].rstrip()

    filename = f"etoro-portfolio-alert-{today_iso}.md"
    path = ec.write_archive(filename, cleaned)

    ec.relay_portfolio_alert(cleaned)
    print("[etoro-portfolio-alert] Telegram relay pushed.")

    ec.commit_archive(path, f"chore: etoro portfolio alert {today_iso}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
