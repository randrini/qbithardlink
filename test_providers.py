#!/usr/bin/env python3
"""Test every metadata provider individually for a given release title.

Usage:
    docker exec -it qbit-classifier python /app/test_providers.py "Berserk" cbz
    docker exec -it qbit-classifier python /app/test_providers.py "Journal d'un prof à la gomme" cbz
"""
import json
import sys
import time

sys.path.insert(0, "/app")

from metadata import _build_providers, _lookup_with_timeout


def main():
    if len(sys.argv) < 2:
        print("Usage: test_providers.py '<title>' [<extension>]")
        sys.exit(1)

    title = sys.argv[1]
    ext = sys.argv[2] if len(sys.argv) > 2 else None
    files = []
    if ext:
        safe = title.replace("/", "_").replace("\\", "_")
        files = [{"name": f"{safe}/{safe}.{ext}"}]

    providers = _build_providers()
    print(f"Title: {title!r}")
    if files:
        print(f"Files: {files}")
    print(f"Providers: {len(providers)}")
    print("-" * 60)

    for p in providers:
        start = time.time()
        try:
            cand = _lookup_with_timeout(p, title, 20)
            elapsed = time.time() - start
            if cand:
                print(f"✓ {p.id:12s} ({elapsed:.2f}s): {cand.get('format'):10s} conf={cand.get('confidence', 0):.2f} title={cand.get('title')!r}")
            else:
                print(f"✗ {p.id:12s} ({elapsed:.2f}s): no match / error")
        except Exception as e:
            elapsed = time.time() - start
            print(f"✗ {p.id:12s} ({elapsed:.2f}s): error={e}")

    print("-" * 60)
    print("Tip: 'no match / error' is normal for slow or down providers.")


if __name__ == "__main__":
    main()
