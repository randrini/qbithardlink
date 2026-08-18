#!/usr/bin/env python3
"""Quick classifier test script.

Usage:
    ./test_release.py "Release.Name.S01E01.FRENCH.1080p.HDTV"
    LLM_ENABLED=true LLM_API_KEY=xxx ./test_release.py "Release.Name"

Set LLM_ENABLED=true and LLM_API_KEY to test the Gemini verification step.
"""
import os
import sys

# Ensure the repo's venv Python is used if available
VENV = "/opt/qbithardlink/.venv/bin/python"
if sys.executable != VENV and os.path.exists(VENV):
    os.execv(VENV, [VENV, __file__] + sys.argv[1:])

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from classifier import classify


def main():
    if len(sys.argv) < 2:
        print("Usage: ./test_release.py '<release name>' [<extension>]")
        print("Examples:")
        print('  ./test_release.py "Journal.dun.prof.a.la.gomme.2024.RETAiL.COMiC.CBZ.eBOOK-NoTag" cbz')
        print('  LLM_ENABLED=true LLM_API_KEY=xxx ./test_release.py "Tirésias" cbz')
        sys.exit(1)

    name = sys.argv[1]
    ext = sys.argv[2] if len(sys.argv) > 2 else None
    files = []
    if ext:
        files = [{"name": f"{name}/{os.path.basename(name)}.{ext}"}]

    cat, conf, reasons = classify(name, files=files, use_metadata=True)
    print(f"Name:    {name}")
    print(f"Files:   {files}")
    print(f"Result:  {cat} (conf={conf:.2f})")
    print(f"Reasons: {reasons}")

    # Try to show the raw LLM JSON verdict if the LLM was used.
    if os.environ.get("LLM_ENABLED", "").strip().lower() in ("1", "true", "yes", "on"):
        try:
            from metadata import llm_classify
            from classifier import clean_release_name, extract_signals
            signals = extract_signals(name, files=files)
            llm_cat, llm_conf, llm_reasons = llm_classify(
                clean_release_name(name, signals),
                files=files,
                signals=signals,
                preliminary={"category": cat, "confidence": conf, "reasons": reasons},
            )
            print(f"LLM:     {llm_cat} (conf={llm_conf:.2f}) {llm_reasons}")
        except Exception as e:
            print(f"LLM raw: error={e}")


if __name__ == "__main__":
    main()
