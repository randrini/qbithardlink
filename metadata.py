#!/usr/bin/env python3
"""
Metadata lookup layer for the qBittorrent classifier.

Queries 2-3 free/no-key providers per content type (manga, ebooks, bd,
comics, light-novel, webtoon) to resolve ambiguous release names and
derive the category from provider metadata.

Provider selection is based on the MetaKavita community-scraper catalog
(https://github.com/raukorim-bot/community-scraper-metakavita), which grades
each provider's payload quality. We use the free/no-key, API-based providers
first (most reliable), then HTML providers where they are the best fit.

Each provider returns a normalized dict:
    {
        "title": str,
        "format": "manga"|"webtoon"|"comic"|"book"|"light_novel",
        "publisher": str|None,
        "language": str|None,   # ISO-639-1
        "country": str|None,   # ISO-3166-1
        "genres": [str],
        "isbn": str|None,
        "confidence": float,   # 0..1 provider match confidence
    }
"""
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("metadata")

# ── Optional deps (BeautifulSoup for HTML providers, requests for FlareSolverr) ──
try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except Exception:
    HAS_BS4 = False

try:
    import requests as _requests
    HAS_REQUESTS = True
except Exception:
    HAS_REQUESTS = False

# FlareSolverr endpoint (for Cloudflare/anti-bot protected sites)
FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://10.0.0.42:8191")

# Google Books API key (optional; improves ebook/comic/manga resolution)
GOOGLE_BOOKS_API_KEY = os.environ.get("GOOGLE_BOOKS_API_KEY", "").strip()

# Provider enable/disable + rate limits from config.yaml
try:
    import config as _cfg
    _PROVIDER_SETTINGS = _cfg.get_provider_settings()
    _META_ENABLED = bool(_cfg.get("metadata.enabled", True))
    if _cfg.get("metadata.flaresolverr_url"):
        FLARESOLVERR_URL = _cfg.get("metadata.flaresolverr_url")
    if _cfg.get("metadata.google_books_api_key"):
        GOOGLE_BOOKS_API_KEY = _cfg.get("metadata.google_books_api_key")
except Exception:
    _PROVIDER_SETTINGS = {}
    _META_ENABLED = True


# Cached FlareSolverr availability flag.
_FLARESOLVERR_OK = None
_FLARESOLVERR_WARNED = False


def _flaresolverr_available():
    """Check (and cache) whether the solver is reachable.

    Tries Trawl's /health first, then FlareSolverr/Byparr GET /v1.
    """
    global _FLARESOLVERR_OK, _FLARESOLVERR_WARNED
    if not HAS_REQUESTS or not FLARESOLVERR_URL:
        _FLARESOLVERR_OK = False
        return False
    if _FLARESOLVERR_OK is not None:
        return _FLARESOLVERR_OK

    _FLARESOLVERR_OK = False
    for path in ("/health", "/v1"):
        try:
            r = _requests.get(f"{FLARESOLVERR_URL}{path}", timeout=3)
            if r.status_code == 200:
                _FLARESOLVERR_OK = True
                break
        except Exception:
            continue

    if not _FLARESOLVERR_OK and not _FLARESOLVERR_WARNED:
        _FLARESOLVERR_WARNED = True
        log.warning("FlareSolverr/Trawl API unavailable at %s; disabling JS-dependent providers", FLARESOLVERR_URL)
    return _FLARESOLVERR_OK


def _flaresolverr_get(url, max_timeout=15000):
    """Fetch a URL through FlareSolverr/Trawl (bypasses Cloudflare/anti-bot)."""
    if not HAS_REQUESTS:
        return None
    if not _flaresolverr_available():
        return None
    try:
        resp = _requests.post(
            f"{FLARESOLVERR_URL}/v1",
            json={"cmd": "request.get", "url": url, "maxTimeout": max_timeout},
            timeout=max_timeout / 1000 + 5,
        )
        data = resp.json()
        if data.get("status") != "ok":
            log.warning("FlareSolverr error: %s", data.get("message"))
            return None
        return data.get("solution", {}).get("response")
    except Exception as e:
        log.warning("FlareSolverr request failed: %s", e)
        return None

# ── HTTP helper (stdlib only, no external deps) ──────────────────────────
_UA = "qbit-classifier/1.0 (metadata lookup)"


def _http_get_json(url, params=None, headers=None, timeout=12):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": _UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _http_post_json(url, payload, headers=None, timeout=12):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"User-Agent": _UA, "Content-Type": "application/json", **(headers or {})},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


# ── Normalization ────────────────────────────────────────────────────────
_ACCENTS = str.maketrans("àâäáãçéèêëíìîïñóòôöõúùûüýÿ", "aaaaaceeeeiiiinooooouuuuyy")


#: Words too weak to anchor a title match (avoid "Le"/"The" matching anything).
_STOPWORDS = {
    "le", "la", "les", "l", "un", "une", "des", "de", "du", "au", "aux",
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
}


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower().translate(_ACCENTS)).strip()


def _tokens(text):
    """Non-stopword tokens of a normalized title."""
    return {t for t in _norm(text).split() if len(t) > 2 and t not in _STOPWORDS}


def _title_similar(a, b, min_overlap=0.6):
    """True if the two titles match strongly enough.

    Uses two safe strategies:
      1. Short query (<=2 non-stop tokens): every query token must appear as a
         whole word in the provider title. This lets "Chi" match
         "Chi's Sweet Home" but not "Le chat".
      2. Longer query: token overlap >= min_overlap relative to the shorter
         non-stopword set.

    This prevents false positives like "Chi Une vie de chat" matching
    PlanèteBD's "Le chat T15" (only "chat" shared = weak) while still accepting
    "The Savage Garden Cultivating Carnivorous Plants" →
    "The Savage Garden, Revised" (3 shared non-stop tokens = strong).
    """
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False

    # Strategy 1: short query -> require whole-word presence of every token.
    if len(ta) <= 2:
        nb_words = set(nb.split())
        if all(t in nb_words for t in ta):
            return True
        return False

    # Strategy 2: token overlap for longer titles.
    shared = ta & tb
    if not shared:
        return False
    return len(shared) / min(len(ta), len(tb)) >= min_overlap


# ── Provider base ─────────────────────────────────────────────────────────
class Provider:
    id = ""
    display_name = ""
    #: content types this provider can resolve
    types = set()
    rate_limit = 1.0  # seconds between requests

    def __init__(self):
        self._last = 0.0

    def _throttle(self):
        wait = self.rate_limit - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def lookup(self, title):
        """Return normalized dict or None. Subclasses implement."""
        raise NotImplementedError

    def _candidate(self, **kw):
        base = {
            "title": None, "format": None, "publisher": None,
            "language": None, "country": None, "genres": [],
            "isbn": None, "confidence": 0.0, "provider": self.id,
        }
        base.update(kw)
        return base


# ════════════════════════════════════════════════════════════════════════
# MANGA providers
# ════════════════════════════════════════════════════════════════════════
class MangaDexProvider(Provider):
    """MangaDex public API — free, no key. format from original language."""
    id = "MANGADEX"
    display_name = "MangaDex"
    types = {"manga", "webtoon"}
    rate_limit = 0.25

    def lookup(self, title):
        self._throttle()
        try:
            params = [
                ("title", title), ("limit", "5"), ("order[relevance]", "desc"),
                ("contentRating[]", "safe"), ("contentRating[]", "suggestive"),
                ("contentRating[]", "erotica"), ("contentRating[]", "pornographic"),
                ("includes[]", "author"), ("includes[]", "artist"), ("includes[]", "cover_art"),
            ]
            url = "https://api.mangadex.org/manga?" + urllib.parse.urlencode(params)
            data = _http_get_json(url, headers={"User-Agent": "qbit-classifier/1.0"})
            items = data.get("data", [])
            if not items:
                return None
            best = items[0]
            attrs = best.get("attributes", {})
            orig_lang = (attrs.get("originalLanguage") or "").lower()
            fmt = "webtoon" if orig_lang in ("ko", "zh") else "manga"
            # title: prefer en, then ja-ro, then any
            title_map = attrs.get("title", {}) or {}
            t = title_map.get("en") or title_map.get("ja-ro") or next(iter(title_map.values()), None)
            if not t or not _title_similar(t, title):
                return None
            return self._candidate(
                title=t, format=fmt, language=orig_lang,
                genres=[g.get("attributes", {}).get("name", {}).get("en", "")
                        for g in attrs.get("tags", []) if g.get("attributes", {}).get("name", {}).get("en")],
                confidence=0.9,
            )
        except Exception as e:
            log.debug("MangaDex lookup failed: %s", e)
            return None


class KitsuProvider(Provider):
    """Kitsu JSON:API — free, no key. format from mangaType."""
    id = "KITSU"
    display_name = "Kitsu"
    types = {"manga", "webtoon"}
    rate_limit = 1.5

    def lookup(self, title):
        self._throttle()
        try:
            url = "https://kitsu.io/api/edge/manga"
            params = {"filter[text]": title, "page[limit]": 5, "include": "categories"}
            data = _http_get_json(url, params=params, headers={"Accept": "application/vnd.api+json"})
            items = data.get("data", [])
            if not items:
                return None
            best = items[0]
            attrs = best.get("attributes", {})
            manga_type = (attrs.get("mangaType") or "").lower()
            fmt = "webtoon" if manga_type in ("manhwa", "manhua", "webtoon") else "manga"
            t = attrs.get("canonicalTitle") or attrs.get("titles", {}).get("en")
            if not t or not _title_similar(t, title):
                return None
            return self._candidate(
                title=t, format=fmt,
                genres=[c.get("attributes", {}).get("title") for c in data.get("included", []) if c.get("type") == "categories"],
                confidence=0.9,
            )
        except Exception as e:
            log.debug("Kitsu lookup failed: %s", e)
            return None


class ShikimoriProvider(Provider):
    """Shikimori JSON API — free, no key. format from kind."""
    id = "SHIKIMORI"
    display_name = "Shikimori"
    types = {"manga", "webtoon", "light_novel"}
    rate_limit = 0.75

    def lookup(self, title):
        self._throttle()
        try:
            url = "https://shikimori.one/api/mangas"
            params = {"search": title, "limit": 5}
            items = _http_get_json(url, params=params)
            if not isinstance(items, list) or not items:
                return None
            best = items[0]
            kind = str(best.get("kind", "")).lower()
            if kind in ("manhwa", "manhua"):
                fmt = "webtoon"
            elif kind in ("light_novel", "novel"):
                fmt = "light_novel"
            else:
                fmt = "manga"
            t = best.get("name") or best.get("russian")
            if not t or not _title_similar(t, title):
                return None
            return self._candidate(
                title=t, format=fmt,
                genres=[g.get("name") for g in best.get("genres", []) if isinstance(g, dict)],
                confidence=0.9,
            )
        except Exception as e:
            log.debug("Shikimori lookup failed: %s", e)
            return None


# ════════════════════════════════════════════════════════════════════════
# EBOOK providers
# ════════════════════════════════════════════════════════════════════════
class OpenLibraryProvider(Provider):
    """Open Library search.json — free, no key. format=book."""
    id = "OPENLIBRARY"
    display_name = "Open Library"
    types = {"book"}
    rate_limit = 1.1

    def lookup(self, title):
        self._throttle()
        try:
            url = "https://openlibrary.org/search.json"
            params = {"q": title, "limit": 5}
            data = _http_get_json(url, params=params)
            docs = data.get("docs", [])
            if not docs:
                return None
            best = docs[0]
            t = best.get("title")
            if not t or not _title_similar(t, title):
                return None
            isbn = None
            if best.get("isbn"):
                isbn = str(best["isbn"][0]).replace("-", "").replace(" ", "")
            return self._candidate(
                title=t, format="book",
                publisher=best.get("publisher", [None])[0] if best.get("publisher") else None,
                language=best.get("language", [None])[0] if best.get("language") else None,
                genres=best.get("subject", [])[:5],
                isbn=isbn, confidence=0.9,
            )
        except Exception as e:
            log.debug("OpenLibrary lookup failed: %s", e)
            return None


class GoogleBooksProvider(Provider):
    """Google Books API — needs a key (optional; works without for low volume)."""
    id = "GOOGLEBOOKS"
    display_name = "Google Books"
    types = {"book", "comic", "manga"}
    rate_limit = 1.0

    def __init__(self, api_key=None):
        super().__init__()
        self.api_key = api_key

    @staticmethod
    def _format_from_categories(categories):
        """Derive format from Google Books `categories` (comics/manga detection).

        Google Books categories are free-text; look for strong markers.
        Manga is checked first because "Comics & Graphic Novels / Manga" also
        contains "comic".
        """
        joined = " ".join(categories or []).lower()
        if "manga" in joined:
            return "manga"
        if "comics & graphic novels" in joined or "comic" in joined:
            return "comic"
        return "book"

    def lookup(self, title):
        self._throttle()
        try:
            url = "https://www.googleapis.com/books/v1/volumes"
            params = {"q": title, "maxResults": 5, "country": "US", "printType": "books"}
            if self.api_key:
                params["key"] = self.api_key
            data = _http_get_json(url, params=params)
            items = data.get("items", [])
            if not items:
                return None
            best = items[0].get("volumeInfo", {})
            t = best.get("title")
            if not t or not _title_similar(t, title):
                return None
            isbn = None
            for ident in best.get("industryIdentifiers", []):
                if ident.get("type") in ("ISBN_13", "ISBN_10"):
                    isbn = str(ident.get("identifier")).replace("-", "").replace(" ", "")
                    break
            categories = best.get("categories", [])
            fmt = self._format_from_categories(categories)
            return self._candidate(
                title=t, format=fmt,
                publisher=best.get("publisher"),
                language=best.get("language"),
                genres=categories,
                isbn=isbn, confidence=0.9,
            )
        except Exception as e:
            log.debug("GoogleBooks lookup failed: %s", e)
            return None


# ════════════════════════════════════════════════════════════════════════
# BD / COMIC providers (HTML via FlareSolverr + BeautifulSoup)
# ════════════════════════════════════════════════════════════════════════
class PlaneteBDProvider(Provider):
    """Planète BD — French BD + US comics (HTML via FlareSolverr). format=comic."""
    id = "PLANETEBD"
    display_name = "Planète BD"
    types = {"comic", "bd"}
    rate_limit = 2.5

    _BASE = "https://www.planetebd.com"
    _ALBUM_RE = re.compile(
        r"^/(?P<kind>bd|comics|mangas)/(?P<publisher>[^/]+)/(?P<series>[^/]+)/(?P<album>[^/]+)/(?P<id>\d+)\.html",
        re.I,
    )

    def lookup(self, title):
        if not HAS_BS4:
            return None
        self._throttle()
        try:
            # 1. Search
            search_url = f"{self._BASE}/recherche/?mot-clef={urllib.parse.quote(title)}"
            html = _flaresolverr_get(search_url)
            if not html:
                return None
            soup = BeautifulSoup(html, "html.parser")
            hits = []
            for art in soup.select("article.featured"):
                a = art.select_one(".image a[href], a[href*='/bd/'], a[href*='/comics/']")
                if not a:
                    continue
                href = a.get("href")
                if not href:
                    continue
                path = urllib.parse.urlparse(href).path
                m = self._ALBUM_RE.match(path)
                if not m:
                    continue
                label = (a.get("title") or a.get_text(" ", strip=True) or "").strip()
                label = re.split(r",\s*(?:bd|comics)\s+chez\s+", label, maxsplit=1, flags=re.I)[0].strip()
                hits.append({"url": href, "label": label, "kind": m.group("kind")})
            if not hits:
                return None

            # 2. Pick best hit by title similarity. Require a real match —
            #    never accept the first hit blindly (false positives).
            best = None
            for h in hits:
                if _title_similar(h["label"], title):
                    best = h
                    break
            if not best:
                return None

            # 3. Fetch detail page
            detail_url = best["url"] if best["url"].startswith("http") else f"{self._BASE}{best['url']}"
            detail_html = _flaresolverr_get(detail_url)
            if not detail_html:
                return None
            dsoup = BeautifulSoup(detail_html, "html.parser")

            # title
            h1 = dsoup.find("h1")
            fetched_title = h1.get_text(" ", strip=True) if h1 else best["label"]

            # format: /bd/ → bd (Franco-Belgian), /comics/ → comics (US)
            fmt = "bd" if best["kind"] == "bd" else "comic"

            # publisher from "bd chez <publisher> de ..."
            publisher = None
            m = re.search(r"bd chez\s+([^<]+)", detail_html)
            if m:
                publisher = re.split(r"\s+de\s+", m.group(1).strip(), maxsplit=1)[0].strip()

            # year
            year = None
            m = re.search(r"\b(19|20)\d{2}\b", detail_html)
            if m:
                year = int(m.group(0))

            # isbn
            isbn = None
            m = re.search(r"\b(?:978|979)\d{10}\b", detail_html)
            if m:
                isbn = m.group(0)

            return self._candidate(
                title=fetched_title, format=fmt, publisher=publisher,
                country="FR", year=year, isbn=isbn, confidence=0.9,
            )
        except Exception as e:
            log.debug("PlaneteBD lookup failed: %s", e)
            return None


class BedethequeProvider(Provider):
    """Bédéthèque — Franco-Belgian comics (HTML via FlareSolverr). format=comic."""
    id = "BEDETHEQUE"
    display_name = "Bédéthèque"
    types = {"comic", "bd"}
    rate_limit = 2.0

    _BASE = "https://www.bedetheque.com"

    def lookup(self, title):
        if not HAS_BS4:
            return None
        self._throttle()
        try:
            # 1. Get CSRF token
            token_html = _flaresolverr_get(f"{self._BASE}/search/albums")
            if not token_html:
                return None
            tsoup = BeautifulSoup(token_html, "html.parser")
            token = None
            tag = tsoup.find("input", {"name": "csrf_token_bel"})
            if tag and tag.get("value"):
                token = tag["value"]
            if not token:
                return None

            # 2. Search with token
            search_url = f"{self._BASE}/search/albums?RechSerie={urllib.parse.quote(title)}&csrf_token_bel={token}"
            search_html = _flaresolverr_get(search_url)
            if not search_html:
                return None
            ssoup = BeautifulSoup(search_html, "html.parser")

            # 3. Find album link
            album_url = None
            results_ul = ssoup.find("ul", class_="search-list")
            if results_ul:
                for li in results_ul.find_all("li"):
                    a = li.find("a", class_="image-tooltip") or li.find("a")
                    if a and a.get("href"):
                        album_url = a["href"]
                        break
            if not album_url:
                return None
            if not album_url.startswith("http"):
                album_url = f"{self._BASE}{album_url}"

            # 4. Fetch detail
            detail_html = _flaresolverr_get(album_url)
            if not detail_html:
                return None
            dsoup = BeautifulSoup(detail_html, "html.parser")

            h1 = dsoup.find("h1")
            fetched_title = h1.get_text(" ", strip=True) if h1 else title

            publisher = None
            m = re.search(r"éditeur\s*[:\s]+([^<]+)", detail_html)
            if m:
                publisher = m.group(1).strip()

            year = None
            m = re.search(r"\b(19|20)\d{2}\b", detail_html)
            if m:
                year = int(m.group(0))

            isbn = None
            m = re.search(r"\b(?:978|979)\d{10}\b", detail_html)
            if m:
                isbn = m.group(0)

            return self._candidate(
                title=fetched_title, format="comic", publisher=publisher,
                country="FR", year=year, isbn=isbn, confidence=0.9,
            )
        except Exception as e:
            log.debug("Bedetheque lookup failed: %s", e)
            return None


# ════════════════════════════════════════════════════════════════════════
# LIGHT NOVEL providers
# ════════════════════════════════════════════════════════════════════════
class NovelUpdatesProvider(Provider):
    """Novel Updates — EN light novels (HTML/CF). format=book."""
    id = "NOVELUPDATES"
    display_name = "Novel Updates"
    types = {"light_novel", "book"}
    rate_limit = 3.0

    def lookup(self, title):
        return None  # HTML/Cloudflare placeholder


# ════════════════════════════════════════════════════════════════════════
# Registry
# ════════════════════════════════════════════════════════════════════════
def _provider_enabled(pid):
    """Whether a provider is enabled in config.yaml (default: enabled)."""
    s = _PROVIDER_SETTINGS.get(pid)
    if s is None:
        return True
    return bool(s.get("enabled", True))


def _provider_rate_limit(pid, default):
    """Per-provider rate limit from config.yaml (default: class default)."""
    s = _PROVIDER_SETTINGS.get(pid)
    if s is None:
        return default
    return float(s.get("rate_limit", default))


def _build_providers(google_books_key=None):
    """Build provider list ordered by speed / reliability.

    Fast no-key API providers go first so that lookup_category can reach a 2-provider
    consensus quickly. JS/FlareSolverr-dependent providers are queried last.
    """
    providers = []

    # 1. Manga / webtoon — fast, public API
    if _provider_enabled("mangadex"):
        p = MangaDexProvider()
        p.rate_limit = _provider_rate_limit("mangadex", p.rate_limit)
        providers.append(p)

    # 2. Ebooks — fast, public APIs
    if _provider_enabled("openlibrary"):
        p = OpenLibraryProvider()
        p.rate_limit = _provider_rate_limit("openlibrary", p.rate_limit)
        providers.append(p)
    if _provider_enabled("googlebooks"):
        p = GoogleBooksProvider(google_books_key)
        p.rate_limit = _provider_rate_limit("googlebooks", p.rate_limit)
        providers.append(p)

    # 3. Anime/manga metadata — public APIs, sometimes slow
    if _provider_enabled("shikimori"):
        p = ShikimoriProvider()
        p.rate_limit = _provider_rate_limit("shikimori", p.rate_limit)
        providers.append(p)
    if _provider_enabled("kitsu"):
        p = KitsuProvider()
        p.rate_limit = _provider_rate_limit("kitsu", p.rate_limit)
        providers.append(p)

    # 4. BD / comics — require FlareSolverr (JS rendering)
    if _provider_enabled("planetebd"):
        p = PlaneteBDProvider()
        p.rate_limit = _provider_rate_limit("planetebd", p.rate_limit)
        providers.append(p)
    if _provider_enabled("bedetheque"):
        p = BedethequeProvider()
        p.rate_limit = _provider_rate_limit("bedetheque", p.rate_limit)
        providers.append(p)
    return providers


#: Known BD/comics/manga publishers/imprints used to disambiguate generic "book".
_FRENCH_BD_PUBLISHERS = {
    "glénat", "dupuis", "casterman", "le lombard", "dargaud", "delcourt",
    "bamboo", "albin michel", "clair de lune", "soleil", "tonkam", "ki-oon",
    "jungle", "vex", "paquet", "le triangle", "flblb", "bd kids",
}
_US_COMIC_PUBLISHERS = {
    "marvel", "dc comics", "dc", "image comics", "dark horse comics",
    "dark horse", "idw publishing", "idw", "boom! studios", "boom studios",
    "vertigo", "wildstorm", "max", "icon", "valiant", "dynamite entertainment",
}
_MANGA_PUBLISHERS = {
    "viz media", "kodansha comics", "kodansha", "yen press", "seven seas",
    "tokyopop", "vertical", "denpa", "j-novel club", "sol press",
}

#: Strong French BD markers in title/genre text.
_BD_MARKERS = {"tome", "bande dessinee", "bd", "franco belge", "integrale"}
#: Strong manga/manhwa/webtoon markers.
_MANGA_MARKERS = {"manga", "manhwa", "webtoon", "shonen", "shojo", "seinen", "josei"}
#: Strong US comic markers.
_COMIC_MARKERS = {"comic", "graphic novel", "superhero", "dc comics", "marvel comics"}


def _norm_token(text):
    return _norm(text).lower()


def _publisher_tokens(publisher):
    """Yield normalized publisher tokens from a string."""
    if not publisher:
        return
    for part in re.split(r"[,;/&]|\band\b", str(publisher), flags=re.I):
        yield _norm_token(part)


def _publisher_match(publisher, known_set):
    for tok in _publisher_tokens(publisher):
        if tok in known_set:
            return True
    return False


def _refine_format(cand):
    """Refine a candidate format using publisher/language/genres/title context.

    Google Books/OpenLibrary often return generic categories; this uses
    publisher, language, and subject markers to correct them without
    hardcoding series names.
    """
    fmt = cand.get("format")
    title = str(cand.get("title") or "")
    publisher = str(cand.get("publisher") or "")
    language = str(cand.get("language") or "").lower()
    genres = " ".join(g.lower() for g in (cand.get("genres") or []))
    combined = f"{title} {publisher} {genres}"
    combined_norm = _norm_token(combined)
    tokens = set(combined_norm.split())

    has_manga_marker = bool(tokens & _MANGA_MARKERS or _publisher_match(publisher, _MANGA_PUBLISHERS))
    has_french = "fr" in language or "fre" in language or "french" in combined_norm
    has_bd_marker = bool(tokens & _BD_MARKERS or _publisher_match(publisher, _FRENCH_BD_PUBLISHERS))
    has_comic_marker = bool(tokens & _COMIC_MARKERS or _publisher_match(publisher, _US_COMIC_PUBLISHERS))

    # Manga/manhwa/webtoon take precedence (strong genre/subject signals)
    if has_manga_marker:
        if "manhwa" in tokens or "webtoon" in tokens:
            return "webtoon"
        return "manga"

    # If provider says "comic" but it's French + BD markers, it's likely a BD.
    if fmt == "comic" and has_french and has_bd_marker and not has_comic_marker:
        return "bd"

    # Generic "book" disambiguation
    if fmt == "book":
        if has_french and has_bd_marker:
            return "bd"
        if has_comic_marker:
            return "comic"

    return fmt


#: format → qBittorrent category
FORMAT_TO_CATEGORY = {
    "manga": "manga",
    "webtoon": "webtoon",
    "comic": "comics",
    "bd": "bd",
    "book": "ebooks",
    "light_novel": "light-novel",
}


def _lookup_with_timeout(provider, title, timeout):
    """Run a single provider.lookup() in a thread; return candidate or None."""
    result = [None]
    def _run():
        try:
            result[0] = provider.lookup(title)
        except Exception as e:
            log.debug("%s lookup error: %s", provider.id, e)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    return result[0]


def lookup_category(title, google_books_key=None):
    """Query providers for a release title, return (category, confidence, provider, reason).

    Uses PROVIDER VOTING: query providers in priority order, tally the categories
    they return, and stop early once 2+ independent providers agree. Each
    provider call is wrapped with a thread-level timeout so a single slow
    provider cannot block the whole cascade.

    Returns (None, 0.0, None, reason) if no provider resolves it.
    """
    if not _META_ENABLED:
        return None, 0.0, None, "metadata disabled in config"
    if google_books_key is None:
        google_books_key = GOOGLE_BOOKS_API_KEY or None
    providers = _build_providers(google_books_key)
    per_call_timeout = float(_cfg.get("metadata.timeout_seconds", 25)) / max(len(providers), 1)
    per_call_timeout = max(per_call_timeout, 5.0)
    deadline = time.time() + float(_cfg.get("metadata.timeout_seconds", 25))

    votes = {}   # category -> [provider_ids]
    best_by_cat = {}
    for p in providers:
        if time.time() > deadline:
            log.debug("Metadata lookup deadline reached for %r", title)
            break
        cand = _lookup_with_timeout(p, title, per_call_timeout)
        if not cand or not cand.get("format"):
            continue
        # Disambiguate generic "book" using publisher/language/genres.
        cand["format"] = _refine_format(cand)
        cat = FORMAT_TO_CATEGORY.get(cand["format"])
        if not cat:
            continue
        votes.setdefault(cat, []).append(p.id)
        cur = best_by_cat.get(cat)
        if cur is None or cand.get("confidence", 0) > cur.get("confidence", 0):
            best_by_cat[cat] = cand
        # Early-exit: 2 independent providers agree → consensus reached.
        if len(votes[cat]) >= 2:
            break

    if not votes:
        return None, 0.0, None, "no provider match"

    # A category wins if >=2 providers agree on it, OR it's the only category
    # any provider returned (single strong signal).
    best_cat = None
    best_count = 0
    for cat, ids in votes.items():
        if len(ids) >= 2:
            if len(ids) > best_count:
                best_cat, best_count = cat, len(ids)
    if best_cat is None and len(votes) == 1:
        best_cat = next(iter(votes))

    if best_cat:
        cand = best_by_cat[best_cat]
        voters = votes[best_cat]
        conf = min(cand.get("confidence", 0.7) + 0.05 * (len(voters) - 1), 1.0)
        return best_cat, conf, "+".join(voters), cand.get("title")
    # Disagreement with no 2-provider agreement → unresolved, tag for review
    return None, 0.0, None, f"providers disagree: {dict(votes)}"


if __name__ == "__main__":
    import sys
    for t in sys.argv[1:]:
        cat, conf, prov, title = lookup_category(t)
        print(f"{t!r} → {cat} (conf={conf:.2f}, provider={prov}, title={title!r})")
