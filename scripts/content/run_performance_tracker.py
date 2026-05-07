"""Entry point for the nightly performance tracking job."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from scripts.content.performance_tracker import fetch_metrics


def main() -> None:
    # X API secrets no longer required — performance tracker uses Nitter
    # scraping for both attribution and engagement (free tier blocks all
    # tweet reads as of 2026-05-07). Kept env passthrough harmless if set.
    print("=== Performance Tracker Job ===")
    fetch_metrics("data/performance_log.json")
    print("Done.")


if __name__ == "__main__":
    main()
