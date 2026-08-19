#!/usr/bin/env python3
"""
qBittorrent category classifier daemon.

Watches qBittorrent for torrents in the "books" category, classifies them
into manga/manhwa/webtoon/comics/bd/light-novel/ebooks/mags/audiobooks,
and sets the category.

Design: PRIORITY-ORDERED rules, not weight-summing. The first category that
hits a strong signal wins. Format tags ([EPUB]/[PDF]/[CBZ]/[CBR]/[M4B]) are
ambiguous, so content-type signals (publisher, language, characters, source
app) disambiguate.

Conservative by design: low-confidence results stay in "books" + tag "review".

Usage:
  classifier.py            # run as a daemon (poll loop)
  classifier.py --once     # run a single pass (for Qui automation)
  classifier.py --test     # run against corpus.txt and report accuracy
"""
from __future__ import annotations

import fcntl
import json
import logging
import logging.handlers
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Configuration (config.yaml + env overrides)
import config as cfg
import requests

log = logging.getLogger("classifier")

# Optional metadata lookup (free/no-key providers). Imported lazily so the
# classifier still works if metadata.py is missing or its deps are absent.
try:
    from metadata import lookup_category, llm_classify
    HAS_METADATA = True
except Exception:
    lookup_category = None
    llm_classify = None
    HAS_METADATA = False


class ClassificationContext:
    """Simple mutable container for the last metadata candidate seen."""
    last_metadata_candidate: Dict[str, Any] | None = None

# ── Configuration (from config.yaml, env-overridable) ────────────────────
QB_URL = cfg.get("qb.url", "http://192.168.1.116:8084")
QB_USER = cfg.get("qb.user", "bidalos")


def _get_qb_password() -> str:
    pw = os.environ.get("QB_PASS") or cfg.get("qb.password", "")
    if not pw:
        raise RuntimeError(
            "qBittorrent password not configured. Set QB_PASS env var or qb.password in config."
        )
    return pw


AUTO_THRESHOLD = float(cfg.get("thresholds.auto", 0.90))
REVIEW_THRESHOLD = float(cfg.get("thresholds.review", 0.70))
POLL_INTERVAL = int(cfg.get("poll_interval", 10))
DEFAULT_CATEGORY = cfg.get("default_category", "ebooks")

# qBit tags that act as a manual override (highest priority).
TAG_OVERRIDES = set(cfg.get_tag_overrides().keys())

# ── CJK detection ────────────────────────────────────────────────────────
# Japanese/Chinese/Korean characters are a strong manga signal.
_CJK_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]")
# Japanese volume markers: 第X巻, 第X号, 第X部
_JP_VOL_RE = re.compile(r"第\s*[0-9０-９]+\s*[巻号部]")
# Japanese magazine names (weekly/monthly)
_JP_MAG_RE = re.compile(r"(?:週刊|月刊|ビッグコミック|ヤングマガジン|少年|少女|ジャンプ|マガジン|サンデー|チャンピオン)")

# ── Video/TV/movie skip detection ──────────────────────────────────────────
# The daemon only processes "books" category torrents, but selftest.py may
# analyze all. Skip obvious video releases instead of routing them to ebooks.
_VIDEO_RE = re.compile(
    r"(?i)\b("
    r"S\d{1,3}E\d{1,4}|S\d{1,3}\s+E\d{1,4}|\d{1,2}x\d{1,2}|"
    r"\b\d{3,4}p\b|\b(?:720|1080|2160)p\b|"
    r"x264\b|x265\b|\bHEVC\b|\bH\.264\b|\bH\.265\b|"
    r"\bWEB[- ]?DL\b|\bWEB[- ]?Rip\b|\bBluRay\b|\bBDRip\b|\bHDRip\b|\bDVDRip\b|\bHDTV\b|\bCAM\b|\bTS\b|"
    r"\bDDP\d\.\d\b|\bDTS[- ]?HD\b|\bTrueHD\b|\bEAC3\b|"
    r"\bHDR10\b|\bDV\b|\bDolby\s*Vision\b|\bREPACK\b|\bPROPER\b|\bUNRATED\b|\bEXTENDED\b|\bRERiP\b|"
    r"\bAMZN\b|\bNF\b|\bDSNP\b|\bHULU\b|\bATVP\b|\bHBO\b|\bMAX\b|"
    r"\bXXX\b|\bNubiles\b|\bNew Sensations\b|\bHOTWIFE\b|\bporn\b|\badult\b|\bOnlyFans\b"
    r")"
)


def is_video(name):
    """Return True if the release name is clearly a TV/movie/adult video."""
    return bool(_VIDEO_RE.search(name))


def normalize(name):
    """Release names use dots/underscores instead of spaces (Harley.Quinn).
    Normalize to spaces so character/publisher regexes match. Keep the
    original for format-tag detection."""
    return re.sub(r"[._]", " ", name)

# ── Priority-ordered rules (from config.yaml) ─────────────────────────────
# Each entry: (category, [(regex, weight), ...], min_score)
# The FIRST category whose total score >= min_score wins.
# Order matters: more specific categories come first.
RULES = cfg.get_rules()


def has_cjk(name):
    return bool(_CJK_RE.search(name))


def has_jp_volume(name):
    return bool(_JP_VOL_RE.search(name))


def has_jp_magazine(name):
    return bool(_JP_MAG_RE.search(name))


# ── Release-name signal detection ────────────────────────────────────────
# Signals are content-type hints extracted from the raw release name. They
# let us (a) keep the relevant words in the cleaned search title and
# (b) target a small subset of metadata providers first, falling back to all
# providers when the signals are wrong or missing.
_MANGA_TOKENS = {"manga", "manhwa", "webtoon", "shonen", "shojo", "seinen", "josei", "scanlation"}
_MANHUA_TOKENS = {"manhua", "chinese comic", "cn comic", "long strip", "vertical scroll"}
_ARTBOOK_TOKENS = {"artbook", "art book", "illustrations", "visual works", "setting materials", "character book", "fanbook"}
_DOUJINSHI_TOKENS = {"doujin", "doujinshi"}
_COMIC_TOKENS = {"comic", "comics", "graphic novel", "superhero", "annual", "one shot", "one-shot", "crossover", "event"}
# Strong origin/publisher signals that should win over French translation/publisher signals.
_US_COMIC_ORIGIN_TOKENS = {
    "marvel", "marvel comics", "mcu", "ultimate marvel", "max", "icon",
    "dc", "dc comics", "d.c.", "vertigo", "wildstorm", "dc black label", "dc universe",
    "image comics", "image", "dark horse", "dark horse comics",
    "idw", "idw publishing", "boom", "boom! studios", "boom studios", "aftershock", "valiant", "dynamite",
    "titan comics", "oni press", "archie", "archie comics",
    "superman", "batman", "spider-man", "spiderman", "x-men", "xmen", "wolverine",
    "iron man", "iron-man", "captain america", "thor", "hulk", "avengers", "justice league",
    "harley quinn", "wonder woman", "green lantern", "the flash", "daredevil", "punisher",
    "miles morales", "gwenpool", "deadpool", "venom", "carnage", "black panther",
    "fantastic four", "x-force", "new mutants", "teen titans", "batgirl", "nightwing",
    "guardians of the galaxy", "inhumans", " eternals", " eternals", "shazam", "aquaman",
    "absolute batman", "absolute superman", "absolute wonder woman", "absolute batgirl",
    "absolute flash", "absolute green lantern", "absolute justice league",
    "panini", "panini comics", "100% marvel", "delcourt marvel",
    "urban comics", "dc deluxe", "dc collectibles",
    "swamp thing", "hellblazer", "constantine",
    "bloodborne", "invincible", "robert kirkman",
}
_COMIC_PUBLISHERS = {"marvel", "dc comics", "dc", "image", "dark horse", "idw", "boom", "vertigo", "panini", "urban comics"}
_BD_TOKENS = {
    "bd", "bande dessinee", "franco belge",
    "glénat", "glenat", "dupuis", "casterman", "le lombard", "dargaud",
    "delcourt", "bamboo", "albin michel", "soleil", "ombres noires",
    "flblb", "humanoides associes", "clair de lune",
}
_LN_TOKENS = {"light novel", "ln", "ranobe", "web novel"}
_AUDIOBOOK_TOKENS = {"audiobook", "m4b", "audible"}
_FRENCH_TOKENS = {"french", "fr", "vostfr", "truefrench", "francais"}
#: French accented characters commonly found in BD/comic titles but rare in
#: English ebook releases. When present with no other strong signal, they
#: suggest querying BD/comic providers first.
_FRENCH_ACCENT_RE = re.compile(r"[éèêëàâùûîôçœæ]")
#: Words that suggest a music release rather than an audiobook (mp3 alone is weak).
_MUSIC_CONTEXT_TOKENS = {
    "flac", "wav", "ogg", "lossless", "discography", "ost", "soundtrack",
    "album", "mixtape", "vinyl", "cd rip", "various artists",
}


def _has_any_token(name, tokens):
    """True if any token appears as a whole word in the normalized name."""
    norm = normalize(name)
    for tok in tokens:
        if re.search(rf"(?i)\b{re.escape(tok)}\b", norm):
            return True
    return False


def _extract_extension_signals(files):
    """Infer signals from the file extensions inside the torrent.

    `files` is the qBittorrent `torrents/files` response: a list of dicts
    with a `name` key (relative path). The dominant non-trivial extension
    wins. Hidden/support files (.nfo, .jpg covers, .sfv) are ignored.

    Note: .cbz/.cbr/.pdf are FORMAT indicators, not content-type indicators.
    They tell us the file is a comic archive or document, but not whether
    it's manga, BD, comics, or manhua. They are recorded as format tags but
    do NOT set boolean content signals (comics/bd) — those must come from
    the release name tokens or metadata.
    """
    signals = {
        "manga": False, "comics": False, "bd": False,
        "light_novel": False, "audiobook": False, "french": False,
        "matched": [],
    }
    ext_counts = {}
    skip_exts = {".nfo", ".sfv", ".md5", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".txt", ".xml", ".json"}
    for f in files:
        path = str(f.get("name") or "")
        ext = os.path.splitext(path)[1].lower()
        if not ext or ext in skip_exts:
            continue
        ext_counts[ext] = ext_counts.get(ext, 0) + 1
    if not ext_counts:
        return signals

    dominant_ext = None
    dominant_count = 0
    total = 0
    for ext, count in ext_counts.items():
        total += count
        if count > dominant_count:
            dominant_count = count
            dominant_ext = ext
    if dominant_ext is None or dominant_count < total * 0.5:
        # No dominant format; mixed bag, don't draw conclusions.
        return signals

    if dominant_ext in {".cbz", ".cbr"}:
        # Format tag only — do NOT set comics/bd signals.
        # .cbz/.cbr are used by manga, BD, comics, and manhua alike.
        signals["matched"].append(f"comic-archive:{dominant_ext}")
    elif dominant_ext == ".epub":
        signals["matched"].append("epub")
    elif dominant_ext == ".mobi":
        signals["matched"].append("mobi")
    elif dominant_ext == ".pdf":
        # PDF is ambiguous (ebook, comic, BD); record format only.
        signals["matched"].append("pdf")
    elif dominant_ext in {".m4b", ".mp3", ".ogg", ".flac", ".aac"}:
        signals["audiobook"] = True
        signals["matched"].append(f"audio:{dominant_ext}")
    return signals


def _classify_by_extension(signals):
    """Return a concrete category if the dominant file extension plus
    content signals give a confident result; otherwise return None.

    File extensions alone are ambiguous — .cbz/.cbr are used by manga,
    BD, comics, and manhua alike. They only contribute meaningfully when
    combined with strong content-type signals from the release name.
    The French flag alone is NOT enough to classify as BD because many
    French translations of manga/comics exist.
    """
    matched = signals.get("matched", [])
    has_cbz_cbr = any(m.startswith("comic-archive:") for m in matched)
    has_epub = "epub" in matched
    has_mobi = "mobi" in matched
    has_audio = any(m.startswith("audio:") for m in matched)
    us_origin = "us-comic-origin" in matched

    # Audio formats are content-specific → strong signal.
    if has_audio:
        return "audiobooks", 0.95, ["dominant audio extension"]
    # Epub/mobi are fairly content-specific → moderate signal.
    if has_epub or has_mobi:
        if signals.get("light_novel"):
            return "light-novel", 0.9, [f"dominant {('epub' if has_epub else 'mobi')} + light-novel signal"]
        return "ebooks", 0.9, [f"dominant {('epub' if has_epub else 'mobi')} extension"]
    # Comic archives (.cbz/.cbr) are FORMAT indicators. They only help
    # disambiguate when strong content signals already exist.
    if has_cbz_cbr:
        if us_origin:
            return "comics", 0.85, ["comic-archive + US comic origin signal"]
        if signals.get("manhua"):
            return "manhua", 0.85, ["comic-archive + manhua signal"]
        if signals.get("manga") or signals.get("manhwa") or signals.get("webtoon"):
            return "manga", 0.85, ["comic-archive + manga/manhwa/webtoon signal"]
        if signals.get("bd"):
            return "bd", 0.85, ["comic-archive + BD signal"]
        # No content signals — .cbz/.cbr alone cannot determine category.
        # Let metadata, regex, or LLM decide.
        return None
    # PDF is too ambiguous to classify from extension alone.
    return None


def extract_signals(name, files=None):
    """Detect content-type signals from a raw release name and optionally
    the torrent's file list.

    Returns a dict with boolean flags (manga, manhwa, webtoon, manhua,
    comics, bd, light_novel, audiobook, artbook, doujinshi, french) plus a
    `matched` list of the tokens that fired, for logging.
    """
    signals = {
        "manga": False, "manhwa": False, "webtoon": False, "manhua": False,
        "comics": False, "bd": False, "light_novel": False,
        "audiobook": False, "artbook": False, "doujinshi": False,
        "french": False,
        "matched": [],
    }

    # First: inspect actual file extensions when the torrent content is known.
    if files:
        file_signals = _extract_extension_signals(files)
        for k, v in file_signals.items():
            if k != "matched" and v:
                signals[k] = True
        signals["matched"].extend(file_signals.get("matched", []))

    # US/English comics origin signals win over French translation markers.
    us_origin = _has_any_token(name, _US_COMIC_ORIGIN_TOKENS)
    if us_origin:
        signals["comics"] = True
        signals["matched"].append("us-comic-origin")

    if has_cjk(name):
        signals["manga"] = True
        signals["matched"].append("cjk")

    if _has_any_token(name, _MANGA_TOKENS):
        signals["manga"] = True
        signals["matched"].append("manga-token")

    if _has_any_token(name, _MANHUA_TOKENS):
        signals["manhua"] = True
        signals["matched"].append("manhua-token")

    if _has_any_token(name, _ARTBOOK_TOKENS):
        signals["artbook"] = True
        signals["matched"].append("artbook-token")

    if _has_any_token(name, _DOUJINSHI_TOKENS):
        signals["doujinshi"] = True
        signals["matched"].append("doujinshi-token")

    # Only add generic comic tokens if not already flagged as US-origin comics.
    if not us_origin and (_has_any_token(name, _COMIC_TOKENS) or _has_any_token(name, _COMIC_PUBLISHERS)):
        signals["comics"] = True
        signals["matched"].append("comics-token")

    if _has_any_token(name, _BD_TOKENS):
        # "tome" is a French volume marker that also appears in audiobook
        # releases; don't let it fire a BD signal when the release is clearly
        # an audiobook. Also suppress BD if US origin signal is present.
        if not (_has_any_token(name, _AUDIOBOOK_TOKENS) or _has_any_token(name, {"mp3"}) or us_origin):
            signals["bd"] = True
            signals["matched"].append("bd-token")

    if _has_any_token(name, _LN_TOKENS):
        signals["light_novel"] = True
        signals["matched"].append("ln-token")

    if _has_any_token(name, _AUDIOBOOK_TOKENS):
        signals["audiobook"] = True
        signals["matched"].append("audiobook-token")
    elif _has_any_token(name, {"mp3"}) and not _has_any_token(name, _MUSIC_CONTEXT_TOKENS):
        signals["audiobook"] = True
        signals["matched"].append("audiobook-token")

    if _has_any_token(name, _FRENCH_TOKENS):
        signals["french"] = True
        signals["matched"].append("french-token")

    # French accented characters suggest BD/comics, but we do NOT set bd=True
    # here because accents also appear in travel guides, textbooks, and novels.
    # The accent is used as a weak tiebreaker in _preliminary_classify instead.
    if _FRENCH_ACCENT_RE.search(name):
        signals["french"] = True
        signals["matched"].append("french-accent")

    # Detect format tags from the release name when file list is unavailable.
    # These are FORMAT indicators (how it's packaged), not content-type signals.
    name_lower = name.lower()
    if not any(m.startswith("comic-archive:") for m in signals["matched"]):
        if re.search(r"\[cb[rz]\]", name, re.I) or re.search(r"\bcb[rz]\b", name_lower):
            signals["matched"].append("comic-archive:name")
    if "epub" not in signals["matched"]:
        if re.search(r"\[epub\]", name, re.I) or re.search(r"\bepub\b", name_lower):
            signals["matched"].append("epub")
    if "mobi" not in signals["matched"]:
        if re.search(r"\[mobi\]", name, re.I) or re.search(r"\bmobi\b", name_lower):
            signals["matched"].append("mobi")

    return signals


# ── Release-name cleaning for metadata lookup ────────────────────────────
# Less aggressive than before: extensions, release groups, years, volume
# markers, and language/format noise are always stripped, but content-type
# words that are active signals (e.g. "Manga", "BD", "Comics") are kept so
# providers can use them to disambiguate.
_META_EXT_RE = re.compile(r"\.(?:epub|pdf|cbz|cbr|mobi|azw3?|m4b|aac|mp3)\b", re.I)
_META_TAG_RE = re.compile(
    r"\[(?:epub|pdf|cbz|cbr|mobi|azw3?|m4b|aac|mp3|int?egrale|collection|bonus|scan|retail|web|hybrid|raw)\]",
    re.I,
)
_META_GROUP_RE = re.compile(
    r"[-_ ](?:NOTAG|TRADEME|kop1|AmisMed|PiXeL|RACHE|PRO|DiVER|iDiB|CTO|21A1|aKraa|ebdz|Team-Moi|NoFace696)\b",
    re.I,
)
_META_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_META_VOL_RE = re.compile(
    r"\b(?:vol\.?\s*\d+|tome\s*\d+|part\s*\d+|ch\.?\s*\d+(?:\s*[-–]\s*\d+)?"
    r"|v\d{1,3}(?:\s*[-–]\s*\d+)?|t\d{1,4}(?:\.\d+)?|to\d{1,4}(?:\.\d+)?)\b",
    re.I,
)
#: Language / format noise words — always stripped (not content signals).
_META_LANG_RE = re.compile(
    r"\b(?:FR|FRENCH|ENGLISH|iTALiAN|JP|JPN|KR|KOR|CN|CHN|VOSTFR|TRUEFRENCH"
    r"|RETAiL|SCAN|eBOOK|HYBRiD|HYBRID|WEB|COMiC|COMIC|BD|MANGA|GRAPHIC\s+NOVEL|LIGHT\s+NOVEL|AUDIOBOOK)\b",
    re.I,
)
#: Content-type words stripped ONLY when the matching signal is absent.
_META_CONTENT_WORDS = {
    "manga": re.compile(r"\bMANGA\b", re.I),
    "comics": re.compile(r"\bCOMICS?\b", re.I),
    "bd": re.compile(r"\bBD\b", re.I),
    "light_novel": re.compile(r"\bLIGHT\s+NOVEL\b|\bLN\b", re.I),
    "audiobook": re.compile(r"\bAUDIOBOOK\b", re.I),
}


def clean_release_name(name, signals=None):
    """Reduce a release name to a searchable title for metadata providers.

    Always strips extensions, bracketed format tags, release groups, standalone
    years, volume/chapter markers, and language/format noise. Words that are
    active content-type signals (manga/comics/bd/light_novel/audiobook) are
    kept so the provider search can use them.
    """
    signals = signals or {}
    cleaned = _META_EXT_RE.sub(" ", name)
    cleaned = re.sub(r"[._]+", " ", cleaned)  # dots/underscores → spaces
    cleaned = _META_TAG_RE.sub(" ", cleaned)
    cleaned = _META_GROUP_RE.sub(" ", cleaned)
    cleaned = _META_YEAR_RE.sub(" ", cleaned)
    cleaned = _META_VOL_RE.sub(" ", cleaned)
    cleaned = _META_LANG_RE.sub(" ", cleaned)
    for sig, pat in _META_CONTENT_WORDS.items():
        if not signals.get(sig):
            cleaned = pat.sub(" ", cleaned)
    # Drop empty brackets left behind by stripped volume markers (e.g. "[TO1 TO26]").
    cleaned = re.sub(r"\[\s*\]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # Drop dangling separators left behind by stripped tokens (e.g. "Part 05 Vol 01 -").
    cleaned = re.sub(r"\s*[-–]\s*$", "", cleaned).strip()
    return cleaned


def _preliminary_classify(name, tags, files, signals, use_metadata):
    """Run the non-LLM classification cascade and return (cat, conf, reasons)."""
    reasons = []
    norm = normalize(name)

    # 0. Manual override via tag
    for tag in tags:
        if tag in TAG_OVERRIDES:
            return tag, 1.0, [f"manual tag override: {tag}"]

    # Skip obvious video releases
    if is_video(name):
        return "skip", 0.0, ["video release: skip"]

    # CJK fast-path
    if has_cjk(name):
        if has_jp_volume(name) or has_jp_magazine(name):
            return "manga", 1.0, ["CJK + Japanese volume/magazine marker"]
        return "manga", 0.95, ["CJK characters"]

    # New categories fast-path
    if signals.get("manhua"):
        return "manhua", 0.95, ["manhua signal"]
    if signals.get("doujinshi"):
        return "doujinshi", 0.95, ["doujinshi signal"]
    if signals.get("artbook"):
        return "artbook", 0.90, ["artbook signal"]

    # Metadata lookup
    if use_metadata and HAS_METADATA and lookup_category is not None:
        try:
            cat, conf, prov, cand = lookup_category(
                clean_release_name(name, signals), signals=signals
            )
            if cat:
                if isinstance(cand, dict):
                    ClassificationContext.last_metadata_candidate = cand
                    title = cand.get("title")
                else:
                    title = cand
                sig_str = ",".join(signals.get("matched") or [])
                reasons.append(f"metadata:{prov} → {title!r} (signals: {sig_str})")
                return cat, conf, reasons
        except Exception as e:
            reasons.append(f"metadata error: {e}")

    # Signal-based classification: use content-type signals from the release
    # name to determine category. These are stronger than format extensions
    # because they reflect actual content (US origin, BD publisher, manga
    # volume markers) rather than just the archive format (.cbz/.cbr).
    us_origin = "us-comic-origin" in signals.get("matched", [])
    if us_origin:
        # US comic origin wins over French translation markers.
        return "comics", 0.90, ["US comic origin signal"] + reasons
    if signals.get("bd"):
        return "bd", 0.85, ["BD signal"] + reasons
    if signals.get("manga") or signals.get("manhwa") or signals.get("webtoon"):
        return "manga", 0.85, ["manga/manhwa/webtoon signal"] + reasons
    if signals.get("audiobook"):
        return "audiobooks", 0.85, ["audiobook signal"] + reasons
    # French accent in the title with no stronger signal → likely BD.
    # Many BD titles use accented French (Tirésias, Astérix, etc.)
    if "french-accent" in signals.get("matched", []) and signals.get("french"):
        has_comic_format = any(m.startswith("comic-archive:") for m in signals.get("matched", []))
        if has_comic_format:
            return "bd", 0.80, ["French accent + comic-archive format"] + reasons

    # Extension-based classification (only when content signals confirm format)
    ext_result = _classify_by_extension(signals)
    if ext_result:
        ext_cat, ext_conf, ext_reasons = ext_result
        return ext_cat, ext_conf, reasons + ext_reasons

    # Regex rules
    for cat, patterns, min_score in RULES:
        score = 0.0
        cat_reasons = []
        for pattern, weight in patterns:
            if re.search(pattern, norm):
                score += weight
                cat_reasons.append(f"{cat}:{pattern}")
        if score >= min_score:
            return cat, min(score, 1.0), reasons + cat_reasons

    # Default ebooks
    return DEFAULT_CATEGORY, 0.5, reasons + [f"cascade undetermined → default {DEFAULT_CATEGORY} (review)"]


def classify(name, tags=None, files=None, use_metadata=False):
    """Return (category, confidence, reasons)."""
    cat, conf, reasons, _metadata = classify_with_metadata(
        name, tags=tags, files=files, use_metadata=use_metadata
    )
    return cat, conf, reasons


def classify_with_metadata(name, tags=None, files=None, use_metadata=False):
    """Return (category, confidence, reasons, metadata_dict).

    Priority:
      0. Manual tag override (highest)
      0.5 Video skip
      1. CJK characters → manga (very strong signal)
      2. Preliminary cascade: metadata → extensions → regex → default ebooks
      3. LLM final arbiter (when enabled): reviews the preliminary result and
         overrides it if wrong. The LLM verdict has higher rank than the
         cascade, but it runs last so providers/regex can keep working when
         the LLM is unavailable.

    The returned metadata_dict is the provider candidate dict when metadata
    drove the classification, otherwise an empty dict.
    """
    ClassificationContext.last_metadata_candidate = None
    tags = [t.strip().lower() for t in (tags or "").split(",") if t.strip()]
    signals = extract_signals(name, files=files)

    # ── Preliminary cascade ────────────────────────────────────────────────
    prelim_cat, prelim_conf, prelim_reasons = _preliminary_classify(
        name, tags, files, signals, use_metadata
    )

    metadata = ClassificationContext.last_metadata_candidate or {}

    # ── LLM final arbiter ──────────────────────────────────────────────────
    llm_enabled = cfg.get("llm.enabled", False)
    _llm = llm_classify if llm_classify is not None else None
    if llm_enabled and _llm and prelim_cat != "skip":
        try:
            llm_cat, _llm_conf, llm_reasons = _llm(
                name,
                files=files,
                signals=signals,
                preliminary={
                    "category": prelim_cat,
                    "confidence": prelim_conf,
                    "reasons": prelim_reasons,
                },
            )
            if llm_cat:
                if llm_cat == prelim_cat:
                    # LLM confirms: keep the cascade category but boost confidence.
                    conf = max(prelim_conf, 0.90)
                    return prelim_cat, conf, prelim_reasons + ["llm-confirmed"] + llm_reasons, metadata
                # LLM disagrees: authoritative override.
                return llm_cat, 0.95, [f"llm-override:{prelim_cat}→{llm_cat}"] + llm_reasons, metadata
        except Exception as e:
            prelim_reasons.append(f"llm error: {e}")

    return prelim_cat, prelim_conf, prelim_reasons, metadata


# ── qBittorrent API helpers ───────────────────────────────────────────────
class QBClient:
    def __init__(self, url=None, user=None, password=None):
        self.url = (url or QB_URL).rstrip("/")
        self.user = user or QB_USER
        self.password = password or _get_qb_password()
        self.session = requests.Session()
        self.session.headers.update({"Referer": self.url, "User-Agent": "qbithardlink/1.0"})

    def _request(self, path, data=None):
        url = f"{self.url}/api/v2/{path}"
        if data is not None:
            resp = self.session.post(url, data=data)
        else:
            resp = self.session.get(url)
        resp.raise_for_status()
        return resp.content

    def login(self):
        r = self.session.post(
            f"{self.url}/api/v2/auth/login",
            data={"username": self.user, "password": self.password},
        )
        if not r.ok:
            log.error("qBittorrent login failed: HTTP %s — %s", r.status_code, r.text[:200])
            raise RuntimeError(f"qBittorrent login failed: HTTP {r.status_code}")
        if "SID" not in {c.name for c in self.session.cookies}:
            log.warning("qBittorrent login OK but no SID cookie; bypass-auth may be enabled")

    def get_torrents(self):
        return json.loads(self._request("torrents/info"))

    def get_torrent_files(self, hash_hex):
        """Return file list for a torrent, or empty list on error."""
        try:
            return json.loads(self._request("torrents/files", {"hash": hash_hex}))
        except Exception as e:
            log.debug("failed to fetch file list for %s: %s", hash_hex, e)
            return []

    def set_category(self, hashes: str | List[str], category: str, max_retries: int = 2) -> None:
        if isinstance(hashes, list):
            hashes = "|".join(hashes)
        # qBittorrent returns 409 if the category does not exist yet.
        for attempt in range(max_retries):
            try:
                self._request(
                    "torrents/setCategory", {"hashes": hashes, "category": category}
                )
                return
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 409 and attempt == 0:
                    try:
                        self._request(
                            "torrents/createCategory",
                            {"category": category, "savePath": ""},
                        )
                    except requests.exceptions.HTTPError:
                        # Category may have been created concurrently; ignore.
                        pass
                    # Loop will retry setCategory.
                else:
                    raise

    def add_tags(self, hashes: str | List[str], tags: str) -> None:
        if isinstance(hashes, list):
            hashes = "|".join(hashes)
        self._request("torrents/addTags", {"hashes": hashes, "tags": tags})

    def set_auto_management(self, hashes: str | List[str], enable: bool = True) -> None:
        if isinstance(hashes, list):
            hashes = "|".join(hashes)
        try:
            self._request(
                "torrents/setAutoManagement",
                {"hashes": hashes, "enable": "true" if enable else "false"},
            )
        except requests.exceptions.HTTPError as e:
            # Older/newer qBittorrent builds may use enableAutoTMM or disagree
            # on parameter names; auto-management is optional, so log and continue.
            log.warning(
                "setAutoManagement failed (%s %s) — continuing",
                e.response.status_code,
                e.response.reason,
            )



# ── Idempotency state ─────────────────────────────────────────────────────
# Tags are the visible, user-controllable signal:
#   "review"     → cascade couldn't determine type; user should correct it.
#   "classified" → already routed to a category; skip on future polls.
# A state file is the invisible guard: even if tags are cleared, we never
# re-query metadata for a hash we've already processed.
STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
STATE_FILE = os.path.join(STATE_DIR, ".classifier_state.json")

#: Tags that mark a torrent as already handled → skip on future polls.
DONE_TAGS = frozenset({"review", "classified"})


def _load_state() -> set[str]:
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
            return set(data.get("processed", []))
    except Exception:
        return set()


def _save_state(processed: set[str]) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"processed": sorted(processed)}, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.warning("could not write state file: %s", e)


def _with_state_lock[T](fn, *args, **kwargs) -> T:
    os.makedirs(STATE_DIR, exist_ok=True)
    lock_path = os.path.join(STATE_DIR, ".classifier_state.lock")
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            return fn(*args, **kwargs)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _best_torrent_name(torrent_name, files):
    """Return the most informative name to classify from.

    qBittorrent sometimes shortens the display name (e.g. "Yuna" instead of
    "Yuna.Lamontagne.Yi.[INTEGRALE].2010.FR.[CBR]-NOTAG"). When files are
    available, prefer the longest file/folder name that contains more tokens.
    """
    if not files:
        return torrent_name
    candidates = [torrent_name]
    for f in files:
        path = f.get("name") if isinstance(f, dict) else str(f)
        if path:
            # Use the top-level entry only (strip subfolders).
            top = path.split("/")[0].split("\\")[0]
            candidates.append(top)
    # Prefer the candidate with the most dots/underscores (more informative),
    # but only if it is not absurdly longer than the display name.
    def score(c):
        c = c.strip()
        if not c:
            return -1
        separators = c.count(".") + c.count("_") + c.count("-")
        return separators * 10 + len(c)
    best = max(candidates, key=score)
    return best or torrent_name


def _format_reasons(reasons):
    """Compact single-line representation of classification reasons."""
    if not reasons:
        return ""
    out = " | ".join(str(r) for r in reasons)
    if len(out) > 300:
        out = out[:297] + "..."
    return out


def process_once(qb, use_metadata=False, state=None):
    """Classify new `books` torrents once. Idempotent: skips torrents already
    tagged review/classified or recorded in the state file."""
    if state is None:
        state = _load_state()
    changed = False
    source_category = cfg.get("qb.source_category", "books")
    hardlink_enabled = bool(cfg.get("hardlink.enabled", True))
    hardlink_script = cfg.get("hardlink.script", "/app/hardlink.sh")
    for t in qb.get_torrents():
        if t.get("category") != source_category:
            continue
        h = t.get("hash")
        raw_tags = t.get("tags", "")
        if isinstance(raw_tags, list):
            tags = {str(x).strip().lower() for x in raw_tags if str(x).strip()}
        else:
            tags = {x.strip().lower() for x in str(raw_tags).split(",") if x.strip()}

        # Idempotency: skip if already handled (tag or state file).
        if tags & DONE_TAGS or h in state:
            log.debug("skipping already-handled torrent %s (tags=%s, in_state=%s)", t.get("name"), tags, h in state)
            continue

        files = qb.get_torrent_files(h)
        display_name = t.get("name", "")
        classify_name = _best_torrent_name(display_name, files)
        cat, conf, reasons = classify(
            classify_name,
            t.get("tags", ""),
            files=files,
            use_metadata=use_metadata,
        )
        if cat == "skip":
            # Non-book torrent that somehow landed in "books"; leave it untouched.
            continue
        # Tag "review" when the cascade fell back to the default (low conf),
        # so the user can correct it. High-confidence results are auto-routed.
        if conf < 0.7:
            qb.add_tags(h, "review")
        else:
            qb.add_tags(h, "classified")
        state.add(h)
        changed = True
        if classify_name != display_name:
            log.info(
                "[%s] (from files: %s) → %s (conf=%.2f) %s",
                display_name, classify_name, cat, conf, _format_reasons(reasons)
            )
        else:
            log.info("[%s] → %s (conf=%.2f) %s", display_name, cat, conf, _format_reasons(reasons))
        # Save state immediately after each successful classification so a
        # later crash does not cause re-processing / duplicate hardlinks.
        _save_state(state)

        # Hardlink the completed content into the library BEFORE changing the
        # qBittorrent category. Changing category with auto-management enabled
        # can move the source files to the new category's folder, making the
        # path we hold stale and breaking the hardlink.
        hardlink_ok = False
        if hardlink_enabled:
            # Pick a source path that actually exists. qBittorrent's content_path
            # is usually the exact file/folder, while save_path is the parent.
            # For multi-file torrents, content_path is the folder root.
            candidates = [
                t.get("content_path"),
                t.get("save_path"),
                os.path.join(t.get("save_path", ""), t.get("name", "")) if t.get("save_path") and t.get("name") else None,
            ]
            content_path = None
            for c in candidates:
                if c and os.path.exists(c):
                    content_path = c
                    break
            if content_path:
                torrent_name = t.get("name", "")
                try:
                    result = subprocess.run(
                        [hardlink_script, torrent_name, content_path, cat],
                        check=False,
                        timeout=300,
                        capture_output=True,
                        text=True,
                    )
                    if result.returncode == 0:
                        log.info(
                            "hardlink ok: %s → %s (category=%s, rc=%d)",
                            torrent_name, content_path, cat, result.returncode,
                        )
                        hardlink_ok = True
                    else:
                        log.warning(
                            "hardlink failed: %s (category=%s, rc=%d) stderr=%s",
                            torrent_name, cat, result.returncode, (result.stderr or "").strip(),
                        )
                except subprocess.TimeoutExpired:
                    log.warning("hardlink timed out after 300s: %s", torrent_name)
                except Exception as e:
                    log.warning("hardlink error for %s: %s", torrent_name, e)
            else:
                log.warning(
                    "hardlink enabled but no existing source path for %s (tried %s)",
                    t.get("name"), [c for c in candidates if c],
                )

        # Change the qBittorrent category only after hardlinking, so
        # auto-management does not move the source files before we copy them.
        qb.set_category(h, cat)
        qb.set_auto_management(h, True)

    return state


def run_test(use_metadata=False):
    """Run against corpus.txt and report per-category accuracy."""
    from collections import defaultdict
    corpus_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corpus.txt")
    by_cat = defaultdict(list)
    for line in open(corpus_path):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cat, _, name = line.partition("|")
        by_cat[cat.strip()].append(name.strip())

    total = correct = 0
    print(f"{'TRUE':<12} {'PRED':<12} {'CONF':<6} NAME")
    print("-" * 100)
    for cat in sorted(by_cat):
        for name in by_cat[cat]:
            pred, conf, _ = classify(name, use_metadata=use_metadata)
            total += 1
            ok = pred == cat
            correct += ok
            mark = "OK " if ok else "XX "
            print(f"{mark}{cat:<10} {pred:<12} {conf:<6.2f} {name[:55]}")
    print("-" * 100)
    print(f"\nAccuracy: {correct}/{total} = {correct/total*100:.1f}%")


def main():
    # ── Logging setup: rotating file + console ────────────────────────────
    log_file = cfg.get("log.file", "/app/logs/classifier.log")
    log_level = getattr(logging, str(cfg.get("log.level", "INFO")).upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        handlers.insert(
            0,
            logging.handlers.RotatingFileHandler(
                log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
            ),
        )
    except Exception as e:
        log.warning("could not set up file logging (%s) — console only", e)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )

    if "--test-meta" in sys.argv:
        run_test(use_metadata=True)
        return
    if "--test" in sys.argv:
        run_test()
        return

    qb = QBClient(QB_URL, QB_USER)
    qb.login()

    if "--once" in sys.argv:
        process_once(qb, use_metadata=True)
        return

    log.info("Classifier watching %s every %ds...", QB_URL, POLL_INTERVAL)
    state = _load_state()
    while True:
        try:
            state = process_once(qb, use_metadata=True, state=state)
        except Exception as e:
            log.exception("error in poll loop: %s", e)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
