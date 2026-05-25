"""eToro Tuesday thesis/framework — cloud version.

Replaces the local mcp__scheduled-tasks etoro-tuesday-thesis SKILL. Runs
Tuesday morning UAE on GitHub Actions. LLM picks the topic from the cadence
table in portfolio.md, drafts the post in voice.

Usage:
  python -m scripts.content.etoro_tuesday_thesis              # full run
  python -m scripts.content.etoro_tuesday_thesis --dry-run    # build prompt only
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.content import etoro_common as ec  # noqa: E402


PROMPT_PATH = ec.REPO_ROOT / "prompts" / "etoro_tuesday_thesis_prompt.txt"


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
    )
    return prompt, today_iso


def run(dry_run: bool = False) -> int:
    prompt, today_iso = build_prompt()
    print(f"[etoro-tuesday-thesis] Today UAE: {today_iso}, prompt length: {len(prompt)} chars")

    if dry_run:
        print("[etoro-tuesday-thesis] --dry-run -> skipping LLM + Telegram + commit")
        print(prompt[:2000])
        return 0

    print("[etoro-tuesday-thesis] Calling Gemini (grounded) -> Groq fallback...")
    try:
        raw = ec.call_llm(prompt, grounded=True)
    except Exception as e:
        print(f"[etoro-tuesday-thesis] LLM call failed: {e}")
        ec.tg_send(f"🔴 eToro Tuesday Thesis — LLM error\n{str(e)[:400]}")
        return 2

    cleaned = ec.strip_em_dashes(raw).strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)

    filename = f"etoro-thesis-{today_iso}.md"
    path = ec.write_archive(filename, cleaned)

    ec.relay_tuesday_thesis(cleaned)
    print("[etoro-tuesday-thesis] Telegram relay pushed.")

    ec.commit_archive(path, f"chore: etoro tuesday thesis {today_iso}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
