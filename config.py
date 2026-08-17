#!/usr/bin/env python3
"""Configuration loader for the qBittorrent classifier.

Loads config.yaml (or CONFIG_PATH env override) and exposes typed accessors.
Secrets can be overridden via env vars (QB_PASS, GOOGLE_BOOKS_API_KEY, ...).
"""
import os
import re

try:
    import yaml
    HAS_YAML = True
except Exception:
    HAS_YAML = False

CONFIG_PATH = os.environ.get("CONFIG_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"))

#: Defaults used when config.yaml is missing or a key is absent.
DEFAULTS = {
    "qb": {"url": "http://192.168.1.116:8084", "user": "bidalos", "password": "your-password"},
    "poll_interval": 10,
    "thresholds": {"auto": 0.90, "review": 0.70},
    "default_category": "ebooks",
    "tag_overrides": {
        "manga": "manga", "manhwa": "manhwa", "webtoon": "webtoon",
        "comics": "comics", "bd": "bd", "light-novel": "light-novel",
        "ebooks": "ebooks", "mags": "mags", "audiobooks": "audiobooks",
    },
    "metadata": {
        "enabled": True,
        "flaresolverr_url": "http://10.0.0.42:8191",
        "google_books_api_key": "",
        "providers": {},
    },
    "rules": {},
}


def _deep_merge(base, override):
    """Recursively merge override into base (override wins)."""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    """Load config.yaml merged over DEFAULTS. Returns a dict."""
    cfg = _deep_merge(DEFAULTS, {})
    if not HAS_YAML:
        return cfg
    try:
        with open(CONFIG_PATH) as f:
            user_cfg = yaml.safe_load(f) or {}  # noqa: F821 (guarded by HAS_YAML)
        cfg = _deep_merge(cfg, user_cfg)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"warning: could not load {CONFIG_PATH}: {e}")
    return cfg


# ── Env-var secret overrides ──────────────────────────────────────────────
def _env_override(cfg):
    """Apply env-var overrides for secrets/connection."""
    if os.environ.get("QB_URL"):
        cfg["qb"]["url"] = os.environ["QB_URL"]
    if os.environ.get("QB_USER"):
        cfg["qb"]["user"] = os.environ["QB_USER"]
    if os.environ.get("QB_PASS"):
        cfg["qb"]["password"] = os.environ["QB_PASS"]
    if os.environ.get("POLL_INTERVAL"):
        cfg["poll_interval"] = int(os.environ["POLL_INTERVAL"])
    if os.environ.get("FLARESOLVERR_URL"):
        cfg["metadata"]["flaresolverr_url"] = os.environ["FLARESOLVERR_URL"]
    if os.environ.get("GOOGLE_BOOKS_API_KEY"):
        cfg["metadata"]["google_books_api_key"] = os.environ["GOOGLE_BOOKS_API_KEY"]
    return cfg


#: Module-level singleton config (loaded once).
CONFIG = _env_override(load_config())


def get(key, default=None):
    """Dot-path accessor: get('qb.url') → value."""
    node = CONFIG
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def get_rules():
    """Return rules as [(category, [(regex, weight), ...], min_score), ...]."""
    rules = get("rules", {}) or {}
    out = []
    for cat, spec in rules.items():
        min_score = float(spec.get("min_score", 0.9))
        patterns = []
        for pat, weight in spec.get("patterns", []):
            patterns.append((pat, float(weight)))
        out.append((cat, patterns, min_score))
    return out


def get_tag_overrides():
    """Return {tag: category} for manual overrides."""
    return get("tag_overrides", {}) or {}


def get_provider_settings():
    """Return {provider_id: {enabled, rate_limit}}."""
    return get("metadata.providers", {}) or {}
