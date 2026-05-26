"""eToro Monthly Review — cloud version.

Replaces the local mcp__scheduled-tasks etoro-monthly-review SKILL. Runs on
the 1st of each month at 10am UAE on GitHub Actions. Reviews the prior month,
drafts a 400-600 word post in voice.

Usage:
  python -m scripts.content.etoro_monthly_review              # full run
  python -m scripts.content.etoro_monthly_review --dry-run    # build prompt only
"""

import argparse
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.content import etoro_common as ec  # noqa: E402


PROMPT_PATH = ec.REPO_ROOT / "prompts" / "etoro_monthly_review_prompt.txt"


_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def build_prompt() -> tuple[str, str, str]:
    """Returns (prompt, today_iso, review_year_month)."""
    brain = ec.load_brain()
    today = ec.uae_today()
    today_iso = today.strftime("%Y-%m-%d")

    # The 1st of this month -> review the prior month. Walk back to last day of
    # prior month.
    first_of_this_month = today.replace(day=1)
    last_of_prior_month = first_of_this_month - timedelta(days=1)
    review_year = last_of_prior_month.year
    review_month = last_of_prior_month.month
    review_month_name = _MONTHS[review_month - 1]
    review_month_year = f"{review_month_name} {review_year}"
    today_month_year = f"{_MONTHS[today.month - 1]} {today.year}"
    review_yyyymm = f"{review_year}-{review_month:02d}"

    # Collect prior-month archive context: weekly scans, EOW recaps, portfolio
    # alerts. Filter filenames by yyyy-mm prefix in the date suffix.
    archive_files = []
    prefixes = ("etoro-weekly-scan-", "etoro-eow-recap-", "etoro-portfolio-alert-")
    if ec.ARCHIVE_DIR.exists():
        for f in sorted(ec.ARCHIVE_DIR.glob("*.md")):
            if not any(f.name.startswith(p) for p in prefixes):
                continue
            if review_yyyymm not in f.name:
                continue
            archive_files.append(f)

    if archive_files:
        sections = []
        for f in archive_files:
            content = f.read_text(encoding="utf-8")
            sections.append(f"### {f.name}\n\n{content}")
        archive_context = "\n\n---\n\n".join(sections)
    else:
        archive_context = "(no prior-month archive files available — first monthly review or archive empty)"

    template = PROMPT_PATH.read_text(encoding="utf-8")
    prompt = template.format(
        today_month_year=today_month_year,
        review_month_year=review_month_year,
        review_month_name=review_month_name,
        review_year=review_year,
        portfolio=brain["portfolio"],
        voice=brain["voice"],
        platform=brain["platform"],
        archive_context=archive_context,
        published_log=brain["published_log"],
    )
    return prompt, today_iso, review_yyyymm


def run(dry_run: bool = False) -> int:
    prompt, today_iso, review_yyyymm = build_prompt()
    print(f"[etoro-monthly-review] Today UAE: {today_iso}, reviewing {review_yyyymm}, prompt length: {len(prompt)} chars")

    if dry_run:
        print("[etoro-monthly-review] --dry-run -> skipping LLM + Telegram + commit")
        print(prompt[:2000])
        return 0

    print("[etoro-monthly-review] Calling Gemini (grounded) -> Groq fallback...")
    try:
        raw = ec.call_llm(prompt, grounded=True)
    except Exception as e:
        print(f"[etoro-monthly-review] LLM call failed: {e}")
        ec.tg_send(f"🔴 eToro Monthly Review — LLM error\n{str(e)[:400]}")
        return 2

    cleaned = ec.strip_em_dashes(raw).strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)

    filename = f"etoro-monthly-review-{review_yyyymm}.md"
    path = ec.write_archive(filename, cleaned)

    ec.relay_monthly_review(cleaned)
    print("[etoro-monthly-review] Telegram relay pushed.")

    ec.commit_archive(path, f"chore: etoro monthly review {review_yyyymm}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
