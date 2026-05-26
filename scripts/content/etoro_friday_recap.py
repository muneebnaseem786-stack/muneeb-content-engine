"""eToro Saturday End-of-Week recap — cloud version.

Replaces the local mcp__scheduled-tasks etoro-friday-recap SKILL. Runs Saturday
morning UAE on GitHub Actions. Drafts the EOW recap post in voice based on
the past week's market action.

Usage:
  python -m scripts.content.etoro_friday_recap              # full run
  python -m scripts.content.etoro_friday_recap --dry-run    # build prompt only
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.content import etoro_common as ec  # noqa: E402


PROMPT_PATH = ec.REPO_ROOT / "prompts" / "etoro_friday_recap_prompt.txt"


def build_prompt() -> tuple[str, str]:
    brain = ec.load_brain()
    today = ec.uae_today()
    today_iso = today.strftime("%Y-%m-%d")

    template = PROMPT_PATH.read_text(encoding="utf-8")
    prompt = template.format(
        today_iso=today_iso,
        portfolio=brain["portfolio"],
        voice=brain["voice"],
        platform=brain["platform"],
        published_log=brain["published_log"],
    )
    return prompt, today_iso


def run(dry_run: bool = False) -> int:
    prompt, today_iso = build_prompt()
    print(f"[etoro-friday-recap] Today UAE: {today_iso}, prompt length: {len(prompt)} chars")

    if dry_run:
        print("[etoro-friday-recap] --dry-run -> skipping LLM + Telegram + commit")
        print(prompt[:2000])
        return 0

    print("[etoro-friday-recap] Calling Gemini (grounded) -> Groq fallback...")
    try:
        raw = ec.call_llm(prompt, grounded=True)
    except Exception as e:
        print(f"[etoro-friday-recap] LLM call failed: {e}")
        ec.tg_send(f"🔴 eToro EOW Recap — LLM error\n{str(e)[:400]}")
        return 2

    cleaned = ec.strip_em_dashes(raw).strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)

    filename = f"etoro-eow-recap-{today_iso}.md"
    path = ec.write_archive(filename, cleaned)

    ec.relay_eow_recap(cleaned)
    print("[etoro-friday-recap] Telegram relay pushed.")

    ec.commit_archive(path, f"chore: etoro eow recap {today_iso}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
