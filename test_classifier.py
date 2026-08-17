#!/usr/bin/env python3
"""Test the classifier rules against the real-world corpus."""
import re
import sys

# Import the RULES from the current classifier
sys.path.insert(0, "/opt/qbithardlink")
from classifier import RULES, TAG_OVERRIDES, AUTO_THRESHOLD, REVIEW_THRESHOLD


def classify(name):
    """Replicate the current classify() logic (regex-only, no tags)."""
    reasons = []
    best_cat = None
    best_score = 0.0
    for cat, patterns in RULES.items():
        score = 0.0
        for pattern, weight in patterns:
            if re.search(pattern, name):
                score += weight
                reasons.append(f"{cat}:{pattern}")
        if score > best_score:
            best_score = score
            best_cat = cat
    if best_cat and best_score >= AUTO_THRESHOLD:
        return best_cat, best_score
    if best_cat and best_score >= REVIEW_THRESHOLD:
        return best_cat, best_score
    return "books", best_score


def load_corpus(path):
    items = []
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cat, _, name = line.partition("|")
        items.append((cat.strip(), name.strip()))
    return items


def main():
    corpus = load_corpus("/opt/qbithardlink/corpus.txt")
    # Group by true category
    from collections import defaultdict
    by_cat = defaultdict(list)
    for cat, name in corpus:
        by_cat[cat].append(name)

    total = 0
    correct = 0
    print(f"{'TRUE CAT':<14} {'PRED':<12} {'CONF':<6} NAME")
    print("-" * 100)
    for cat in sorted(by_cat):
        for name in by_cat[cat]:
            pred, conf = classify(name)
            total += 1
            ok = (pred == cat)
            correct += ok
            mark = "OK " if ok else "XX "
            print(f"{mark}{cat:<12} {pred:<12} {conf:<6.2f} {name[:60]}")
    print("-" * 100)
    print(f"\nAccuracy: {correct}/{total} = {correct/total*100:.1f}%")


if __name__ == "__main__":
    main()
