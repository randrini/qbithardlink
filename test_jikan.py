#!/usr/bin/env python3
"""Test the Jikan (MyAnimeList) provider directly.

Usage:
    docker exec -it qbit-classifier python /app/test_jikan.py "Berserk"
    docker exec -it qbit-classifier python /app/test_jikan.py "Solo Leveling"
"""
import json
import sys
import time

sys.path.insert(0, "/app")

from metadata import JikanProvider, _lookup_with_timeout


def main():
    if len(sys.argv) < 2:
        print("Usage: test_jikan.py '<title>'")
        sys.exit(1)

    title = sys.argv[1]
    provider = JikanProvider()
    print(f"Jikan lookup for: {title!r}")
    print(f"Endpoint: https://api.jikan.moe/v4/manga?q={title}")
    start = time.time()
    try:
        cand = _lookup_with_timeout(provider, title, 15)
        elapsed = time.time() - start
        if cand:
            print(f"Result in {elapsed:.2f}s:")
            print(json.dumps(cand, indent=2, ensure_ascii=False))
        else:
            print(f"No result after {elapsed:.2f}s (Jikan may be down or no match)")
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    main()
