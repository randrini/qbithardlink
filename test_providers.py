#!/usr/bin/env python3
"""Test every metadata provider + LLM individually for a given release title.

Usage:
    docker exec -it qbit-classifier python /app/test_providers.py "Journal.dun.prof.a.la.gomme.2024.RETAiL.COMiC.CBZ.eBOOK-NoTag" cbz
    docker exec -it -e LLM_ENABLED=true -e LLM_API_KEY=... qbit-classifier python /app/test_providers.py "Berserk" cbz
"""
import os
import sys
import time

sys.path.insert(0, "/app")

from metadata import _build_providers, _lookup_with_timeout, llm_classify
from classifier import clean_release_name, extract_signals


def main():
    if len(sys.argv) < 2:
        print("Usage: test_providers.py '<release name>' [<file-extension>]")
        sys.exit(1)

    raw_title = sys.argv[1]
    ext = sys.argv[2] if len(sys.argv) > 2 else None
    files = []
    if ext:
        safe = raw_title.replace("/", "_").replace("\\", "_")
        files = [{"name": f"{safe}/{safe}.{ext}"}]

    signals = extract_signals(raw_title, files=files)
    clean_title = clean_release_name(raw_title, signals)

    print(f"Raw title:   {raw_title!r}")
    print(f"Clean title: {clean_title!r}")
    if files:
        print(f"Files:       {files}")
    print(f"Signals:     {signals}")
    print("-" * 70)

    providers = _build_providers()
    print(f"Metadata providers: {len(providers)}")
    for p in providers:
        start = time.time()
        try:
            # Providers need the cleaned, searchable title.
            cand = _lookup_with_timeout(p, clean_title, 20)
            elapsed = time.time() - start
            if cand:
                print(f"✓ {p.id:12s} ({elapsed:.2f}s): {cand.get('format'):10s} conf={cand.get('confidence', 0):.2f} title={cand.get('title')!r}")
            else:
                print(f"✗ {p.id:12s} ({elapsed:.2f}s): no match / error")
        except Exception as e:
            elapsed = time.time() - start
            print(f"✗ {p.id:12s} ({elapsed:.2f}s): error={e}")

    # Optional LLM check
    if os.environ.get("LLM_ENABLED", "").strip().lower() in ("1", "true", "yes", "on"):
        print("-" * 70)
        print("LLM (Gemini) verification:")
        start = time.time()
        try:
            llm_cat, llm_conf, llm_reasons = llm_classify(
                raw_title,
                files=files,
                signals=signals,
                preliminary={"category": "?", "confidence": 0.0, "reasons": []},
            )
            elapsed = time.time() - start
            if llm_cat:
                print(f"✓ LLM          ({elapsed:.2f}s): {llm_cat:10s} conf={llm_conf:.2f} {llm_reasons}")
            else:
                print(f"✗ LLM          ({elapsed:.2f}s): no response / disabled / cooldown")
        except Exception as e:
            elapsed = time.time() - start
            print(f"✗ LLM          ({elapsed:.2f}s): error={e}")

    print("-" * 70)
    print("Tip: providers get the cleaned title; LLM gets the raw release name.")


if __name__ == "__main__":
    main()
