"""Entry point for the nightly performance tracking job."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from scripts.content.performance_tracker import fetch_metrics


def main() -> None:
    required = ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"ERROR: Missing secrets: {', '.join(missing)}")
        sys.exit(1)

    print("=== Performance Tracker Job ===")
    fetch_metrics("data/performance_log.json")
    print("Done.")


if __name__ == "__main__":
    main()
