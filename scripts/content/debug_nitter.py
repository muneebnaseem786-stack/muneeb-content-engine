"""Temporary diagnostic: fetch one Nitter tweet page and dump HTML structure
around engagement counts. Run via workflow_dispatch on GitHub Actions
(residential IPs hit 503 on Nitter).
"""

import re
import requests

INSTANCES = ["nitter.poast.org", "nitter.net", "nitter.privacydev.net", "nitter.it"]
HANDLE = "MuneebNaseem"
TWEET_ID = "2052226441212039465"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ReactionRadar/1.0)"}


def main():
    for inst in INSTANCES:
        url = f"https://{inst}/{HANDLE}/status/{TWEET_ID}"
        try:
            resp = requests.get(url, timeout=12, headers=HEADERS)
            print(f"\n=== {inst} → HTTP {resp.status_code} | {len(resp.text)} bytes ===")
            if resp.status_code != 200:
                print(f"(skipping, non-200)")
                continue
            html = resp.text

            # Find any chunks containing engagement stats
            for keyword in ["tweet-stats", "tweet-stat", "icon-comment", "icon-retweet",
                            "icon-heart", "icon-quote", "favorite", "reply-count",
                            "retweet-count"]:
                if keyword in html:
                    idx = html.find(keyword)
                    snippet = html[max(0, idx-100):idx+400]
                    print(f"\n  [{keyword}] @ char {idx}")
                    print(f"  ... {snippet} ...")
                    break  # just need one example per instance

            # Also dump raw bytes around any standalone digit clusters that look like counts
            count_matches = re.findall(r'>\s*([0-9]{1,4}(?:[KMkm]|,[0-9]{3})*)\s*<', html)
            print(f"\n  Standalone count patterns found: {count_matches[:10]}")

        except Exception as e:
            print(f"\n=== {inst} → ERROR: {type(e).__name__}: {e} ===")


if __name__ == "__main__":
    main()
