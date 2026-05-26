"""eToro Sunday weekly-prep — cloud version.

Replaces the local mcp__scheduled-tasks SKILL of the same name. Runs on GitHub
Actions every Sunday morning UAE time. Loads the eToro brain from
data/etoro/, calls Gemini (grounded with Google Search) to scan top-22
holdings for past-week news + upcoming-week events, drafts the Sunday Week
Ahead post in voice, writes the dated archive into data/etoro/archive/,
commits it back to the repo, and pushes the draft + notes to Telegram via
direct HTTP.

Usage:
  python -m scripts.content.etoro_sunday_prep              # full run
  python -m scripts.content.etoro_sunday_prep --dry-run    # build prompt + print, skip LLM + Telegram + commit
"""

import argparse
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.content import etoro_common as ec  # noqa: E402


PROMPT_PATH = ec.REPO_ROOT / "prompts" / "etoro_sunday_prep_prompt.txt"


def build_prompt() -> tuple[str, str]:
    brain = ec.load_brain()
    today = ec.uae_today()
    iso = lambda d: d.strftime("%Y-%m-%d")  # noqa: E731

    today_iso = iso(today)
    seven_days_ago_iso = iso(today - timedelta(days=7))

    # Mon = next day after Sunday. weekday(): Mon=0 ... Sun=6. We expect Sun (6).
    days_to_monday = (7 - today.weekday()) % 7 or 1
    next_monday = today + timedelta(days=days_to_monday)

    template = PROMPT_PATH.read_text(encoding="utf-8")
    prompt = template.format(
        today_iso=today_iso,
        seven_days_ago_iso=seven_days_ago_iso,
        next_monday_iso=iso(next_monday),
        next_tuesday_iso=iso(next_monday + timedelta(days=1)),
        next_wednesday_iso=iso(next_monday + timedelta(days=2)),
        next_thursday_iso=iso(next_monday + timedelta(days=3)),
        next_friday_iso=iso(next_monday + timedelta(days=4)),
        portfolio=brain["portfolio"],
        voice=brain["voice"],
        platform=brain["platform"],
        published_log=brain["published_log"],
    )
    return prompt, today_iso


def run(dry_run: bool = False) -> int:
    prompt, today_iso = build_prompt()
    print(f"[etoro-sunday-prep] Today UAE: {today_iso}, prompt length: {len(prompt)} chars")

    if dry_run:
        print("[etoro-sunday-prep] --dry-run -> skipping LLM + Telegram + commit")
        print(prompt[:2000])
        return 0

    print("[etoro-sunday-prep] Calling Gemini (grounded) -> Groq fallback...")
    try:
        raw = ec.call_llm(prompt, grounded=True)
    except Exception as e:
        print(f"[etoro-sunday-prep] LLM call failed: {e}")
        ec.tg_send(f"🔴 eToro Sunday Prep — LLM error\n{str(e)[:400]}")
        return 2

    cleaned = ec.strip_em_dashes(raw).strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)

    filename = f"etoro-weekly-scan-{today_iso}.md"
    path = ec.write_archive(filename, cleaned)

    ec.relay_weekly_scan(cleaned)
    print("[etoro-sunday-prep] Telegram relay pushed.")

    ec.commit_archive(path, f"chore: etoro sunday prep {today_iso}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
