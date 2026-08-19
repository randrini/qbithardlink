#!/usr/bin/env python3
"""Shared qBittorrent API client for qbithardlink tools."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

import config as cfg


class QBClient:
    """Minimal qBittorrent WebUI API client (no external dependencies)."""

    def __init__(
        self,
        url: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        self.url: str = (url or cfg.get("qb.url", "http://192.168.1.116:8084")).rstrip("/")
        self.user: str = user or cfg.get("qb.user", "")
        self.password: str = password or os.environ.get("QB_PASS") or cfg.get("qb.password", "")
        if not self.password:
            raise RuntimeError(
                "qBittorrent password not configured. Set QB_PASS env var or qb.password in config."
            )
        self._cookies: Dict[str, str] = {}

    def _request(self, path: str, data: Optional[Dict[str, Any]] = None) -> bytes:
        url = f"{self.url}/api/v2/{path}"
        body = urllib.parse.urlencode(data).encode() if data else None
        req = urllib.request.Request(url, data=body)
        if self._cookies:
            req.add_header(
                "Cookie", "; ".join(f"{k}={v}" for k, v in self._cookies.items())
            )
        with urllib.request.urlopen(req) as resp:
            # qBittorrent sends Set-Cookie on login; capture it for subsequent calls.
            cookie_header = resp.headers.get("Set-Cookie")
            if cookie_header:
                for part in cookie_header.split(";"):
                    if "=" in part:
                        key, _, value = part.partition("=")
                        self._cookies[key.strip()] = value.strip()
            return resp.read()

    def login(self) -> None:
        self._request(
            "auth/login",
            {"username": self.user, "password": self.password},
        )

    def get_categories(self) -> Dict[str, Dict[str, Any]]:
        return json.loads(self._request("torrents/categories") or b"{}")

    def create_category(self, category: str, save_path: str) -> None:
        self._request(
            "torrents/createCategory",
            {"category": category, "savePath": save_path},
        )

    def edit_category(self, category: str, save_path: str) -> None:
        self._request(
            "torrents/editCategory",
            {"category": category, "savePath": save_path},
        )

    def get_torrents(self) -> List[Dict[str, Any]]:
        return json.loads(self._request("torrents/info") or b"[]")

    def set_category(self, hashes: List[str], category: str) -> None:
        if not hashes:
            return
        joined = "|".join(hashes)
        try:
            self._request("torrents/setCategory", {"hashes": joined, "category": category})
        except urllib.error.HTTPError as e:
            if e.code == 409:
                # Category missing — create it with an empty path (caller should set path separately).
                self._request(
                    "torrents/createCategory",
                    {"category": category, "savePath": ""},
                )
                self._request("torrents/setCategory", {"hashes": joined, "category": category})
            else:
                raise

    def set_location(self, hashes: List[str], location: str) -> None:
        if not hashes:
            return
        joined = "|".join(hashes)
        self._request("torrents/setLocation", {"hashes": joined, "location": location})

    def delete_torrents(self, hashes: List[str], delete_files: bool = False) -> None:
        if not hashes:
            return
        joined = "|".join(hashes)
        self._request(
            "torrents/delete",
            {"hashes": joined, "deleteFiles": "true" if delete_files else "false"},
        )


def get_password() -> str:
    """Return configured qBittorrent password, or raise if missing."""
    pw = os.environ.get("QB_PASS") or cfg.get("qb.password", "")
    if not pw:
        raise RuntimeError(
            "qBittorrent password not configured. Set QB_PASS env var or qb.password in config."
        )
    return pw
