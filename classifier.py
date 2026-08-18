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

# Configuration (config.yaml + env overrides)
import config as cfg
import requests

log = logging.getLogger("classifier")

# Optional metadata lookup (free/no-key providers). Imported lazily so the
# classifier still works if metadata.py is missing or its deps are absent.
try:
    from metadata import lookup_category
    HAS_METADATA = True
except Exception:
    lookup_category = None
    HAS_METADATA = False

# ── Configuration (from config.yaml, env-overridable) ────────────────────
QB_URL = cfg.get("qb.url", "http://192.168.1.116:8084")
QB_USER = cfg.get("qb.user", "bidalos")
QB_PASS = cfg.get("qb.password", "your-password")

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
    r"(?i)\b(S\d{1,3}E\d{1,4}|S\d{1,3}\s+E\d{1,4}|\d{1,2}x\d{1,2}|\b\d{3,4}p\b|\b(?:720|1080|2160)p\b|"
    r"x264|x265|HEVC|WEB[- ]?DL|WEB[- ]?Rip|BluRay|HDTV|HDRip|DVDRip|CAM|TS\b|"
    r"XXX\b|Nubiles|New Sensations|HOTWIFE|porn|adult)"
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
_MANGA_TOKENS = {"manga", "manhwa", "webtoon", "shonen", "shojo", "seinen", "josei"}
_COMIC_TOKENS = {"comic", "comics", "graphic novel", "superhero", "cbz", "cbr"}
_COMIC_PUBLISHERS = {"marvel", "dc comics", "dc", "image", "dark horse", "idw", "boom", "vertigo"}
_BD_TOKENS = {
    "bd", "bande dessinee", "tome", "franco belge", "integrale", "glenat",
    "dupuis", "casterman", "le lombard", "dargaud", "delcourt", "bamboo",
    "albin michel", "soleil", "tonkam", "ki-oon", "jungle",
}
_LN_TOKENS = {"light novel", "ln"}
_AUDIOBOOK_TOKENS = {"audiobook", "m4b"}
_FRENCH_TOKENS = {"french", "fr", "vostfr", "truefrench"}
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


def extract_signals(name):
    """Detect content-type signals from a raw release name.

    Returns a dict with boolean flags (manga, comics, bd, light_novel,
    audiobook, french) plus a `matched` list of the tokens that fired, for
    logging.
    """
    signals = {
        "manga": False, "comics": False, "bd": False,
        "light_novel": False, "audiobook": False, "french": False,
        "matched": [],
    }

    if has_cjk(name):
        signals["manga"] = True
        signals["matched"].append("cjk")

    if _has_any_token(name, _MANGA_TOKENS):
        signals["manga"] = True
        signals["matched"].append("manga-token")

    if _has_any_token(name, _COMIC_TOKENS) or _has_any_token(name, _COMIC_PUBLISHERS):
        signals["comics"] = True
        signals["matched"].append("comics-token")

    if _has_any_token(name, _BD_TOKENS):
        # "tome" is a French volume marker that also appears in audiobook
        # releases; don't let it fire a BD signal when the release is clearly
        # an audiobook.
        if not (_has_any_token(name, _AUDIOBOOK_TOKENS) or _has_any_token(name, {"mp3"})):
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

    # French accented characters (é, è, ê, à, ç, etc.) are a weak signal
    # for BD/comics. Only activate when no stronger signal is present and
    # the title looks like a short proper name (not an English ebook dump).
    if not any([signals["manga"], signals["comics"], signals["bd"],
                signals["light_novel"], signals["audiobook"]]):
        if _FRENCH_ACCENT_RE.search(name):
            signals["bd"] = True
            signals["french"] = True
            signals["matched"].append("french-accent")

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
    r"|RETAiL|SCAN|eBOOK|HYBRiD|HYBRID|WEB)\b",
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


def classify(name, tags=None, use_metadata=False):
    """Return (category, confidence, reasons).

    Priority:
      0. Manual tag override (highest)
      0.5 CJK characters → manga (very strong signal)
      1. Metadata lookup (authoritative): strip noise from the release name,
         query providers, use the provider's `format` as the category.
         Formats/extensions are NOT deterministic (a PDF can be manga, ebook,
         comic, or BD) — only provider metadata decides.
      2. Regex rules (fast-path fallback when metadata is off/unavailable)
      3. books/review
    """
    tags = [t.strip().lower() for t in (tags or "").split(",") if t.strip()]
    reasons = []
    norm = normalize(name)  # dots/underscores → spaces for content matching

    # 0. Manual override via tag (highest priority)
    for tag in tags:
        if tag in TAG_OVERRIDES:
            return tag, 1.0, [f"manual tag override: {tag}"]

    # 0.25 Skip obvious video/TV/movie/adult releases when run outside the
    #    "books" category (e.g. selftest.py over all torrents).
    if is_video(name):
        return "skip", 0.0, ["video release: skip"]

    # 0.5 CJK characters → manga (very strong signal)
    if has_cjk(name):
        if has_jp_volume(name) or has_jp_magazine(name):
            return "manga", 1.0, ["CJK + Japanese volume/magazine marker"]
        return "manga", 0.95, ["CJK characters"]

    # 1. Metadata lookup (authoritative). Strip noise → query → category.
    #    Signals from the raw release name target a small provider subset
    #    first; if that fails, metadata.py falls back to all providers.
    if use_metadata and HAS_METADATA and lookup_category is not None:
        try:
            signals = extract_signals(name)
            cat, conf, prov, title = lookup_category(
                clean_release_name(name, signals), signals=signals
            )
            if cat:
                sig_str = ",".join(signals.get("matched") or [])
                reasons.append(f"metadata:{prov} → {title!r} (signals: {sig_str})")
                return cat, conf, reasons
        except Exception as e:
            reasons.append(f"metadata error: {e}")

    # 2. Regex rules (fast-path fallback when metadata is off/unavailable)
    for cat, patterns, min_score in RULES:
        score = 0.0
        for pattern, weight in patterns:
            if re.search(pattern, norm):
                score += weight
                reasons.append(f"{cat}:{pattern}")
        if score >= min_score:
            return cat, min(score, 1.0), reasons

    # 3. Default: ebooks. If the whole cascade (metadata + regex) can't
    #    determine the type, the safest default for a book/comics library is
    #    ebooks — most undetermined releases are prose ebooks. Tag "review"
    #    so the user can correct it.
    return DEFAULT_CATEGORY, 0.5, reasons + [f"cascade undetermined → default {DEFAULT_CATEGORY} (review)"]


# ── qBittorrent API helpers ───────────────────────────────────────────────
class QBClient:
    def __init__(self, url, user, password):
        self.url = url.rstrip("/")
        self.user = user
        self.password = password
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

    def set_category(self, hashes, category):
        # qBittorrent returns 409 if the category does not exist yet.
        try:
            self._request("torrents/setCategory", {"hashes": hashes, "category": category})
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 409:
                self._request("torrents/createCategory", {"category": category, "savePath": ""})
                self._request("torrents/setCategory", {"hashes": hashes, "category": category})
            else:
                raise

    def add_tags(self, hashes, tags):
        self._request("torrents/addTags", {"hashes": hashes, "tags": tags})

    def set_auto_management(self, hashes, enable=True):
        try:
            self._request(
                "torrents/setAutoManagement",
                {"hashes": hashes, "enable": "true" if enable else "false"},
            )
        except requests.exceptions.HTTPError as e:
            # Older/newer qBittorrent builds may use enableAutoTMM or disagree
            # on parameter names; auto-management is optional, so log and continue.
            log.warning("setAutoManagement failed (%s %s) — continuing", e.response.status_code, e.response.reason)



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


def _load_state():
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f).get("processed", []))
    except Exception:
        return set()


def _save_state(processed):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump({"processed": sorted(processed)}, f)
    except Exception as e:
        log.warning("could not write state file: %s", e)


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

        cat, conf, reasons = classify(t.get("name", ""), t.get("tags", ""), use_metadata=use_metadata)
        if cat == "skip":
            # Non-book torrent that somehow landed in "books"; leave it untouched.
            continue
        # Tag "review" when the cascade fell back to the default (low conf),
        # so the user can correct it. High-confidence results are auto-routed.
        if conf < 0.7:
            qb.add_tags(h, "review")
        else:
            qb.add_tags(h, "classified")
        qb.set_category(h, cat)
        qb.set_auto_management(h, True)
        state.add(h)
        changed = True
        log.info("[%s] → %s (conf=%.2f) %s", t.get("name"), cat, conf, reasons)
        # Save state immediately after each successful classification so a
        # later crash does not cause re-processing / duplicate hardlinks.
        _save_state(state)

        # Hardlink the completed content into the library (if enabled).
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
                        log.info("hardlink ok: %s → %s (rc=%d)", torrent_name, content_path, result.returncode)
                    else:
                        log.warning(
                            "hardlink failed: %s (rc=%d) stderr=%s",
                            torrent_name, result.returncode, (result.stderr or "").strip(),
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

    qb = QBClient(QB_URL, QB_USER, QB_PASS)
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
