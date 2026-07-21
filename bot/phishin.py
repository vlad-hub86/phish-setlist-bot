"""phish.in API v2 client — verified song durations from actual recordings.

phish.in hosts audience recordings with exact track times; a show's audio
usually appears within hours after the show. Used for the "verified lengths"
recap (run the morning after via `python main.py verified-recap --date ...`).

API v2 is public/read-only: https://phish.in/api-docs
"""
from __future__ import annotations

from typing import Optional

import requests

BASE = "https://phish.in/api/v2"
UA = "phish-setlist-bot/0.1 (verified lengths; contact: vlad@miajunefacialbar.com)"


class PhishinClient:
    def __init__(self, session: Optional[requests.Session] = None):
        self.http = session or requests.Session()
        self.http.headers["User-Agent"] = UA

    def show_tracks(self, showdate: str) -> list[dict]:
        """Returns [{'title', 'duration_ms', 'set_name', 'position'}, ...] or [] if not yet up."""
        resp = self.http.get(f"{BASE}/shows/{showdate}", timeout=20)
        if resp.status_code == 404:
            return []  # recording not posted yet
        resp.raise_for_status()
        data = resp.json()
        tracks = data.get("tracks", []) or []
        out = []
        for t in tracks:
            out.append(
                {
                    "title": t.get("title", ""),
                    "duration_ms": t.get("duration"),  # phish.in duration is in ms
                    "set_name": t.get("set_name") or t.get("set", ""),
                    "position": t.get("position"),
                }
            )
        return out
