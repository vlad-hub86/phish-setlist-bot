"""Phish.net API v5 client.

Docs: https://docs.phish.net/
Etiquette: poll only during show windows, identify ourselves, cache aggressively.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import requests

log = logging.getLogger(__name__)

BASE = "https://api.phish.net/v5"
USER_AGENT = "phish-setlist-bot/0.1 (setlist poster; contact: vlad@miajunefacialbar.com)"

# Phish.net transition codes -> printable marks
TRANSITIONS = {1: ",", 2: " >", 3: " ->"}


@dataclass(frozen=True)
class SetlistEntry:
    showdate: str          # YYYY-MM-DD
    set_label: str         # "1", "2", "3", "e" (encore) as given by phish.net
    position: int          # 1-based position within the whole show as listed
    song: str
    songid: Optional[int]
    gap: Optional[int]     # shows since last played (phish.net computes this)
    transition: str        # ",", " >", " ->"
    venue: str
    city: str
    state: str
    footnote: str = ""

    @property
    def key(self) -> tuple:
        """Identity for diffing: position within a set of a given show."""
        return (self.showdate, self.set_label, self.position)

    @property
    def set_display(self) -> str:
        s = self.set_label.lower()
        if s in ("e", "e2", "e3"):
            return "Encore" if s == "e" else f"Encore {s[1]}"
        return f"Set {self.set_label}"


class PhishNetClient:
    def __init__(self, api_key: str, session: Optional[requests.Session] = None):
        self.api_key = api_key
        self.http = session or requests.Session()
        self.http.headers["User-Agent"] = USER_AGENT

    def _get(self, path: str, **params) -> dict:
        params["apikey"] = self.api_key
        resp = self.http.get(f"{BASE}/{path}", params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            raise RuntimeError(f"phish.net error on {path}: {data.get('error_message')}")
        return data

    # -- setlists ------------------------------------------------------------

    def setlist_for_date(self, showdate: str, artist: str = "phish") -> list[SetlistEntry]:
        """Fetch the (possibly in-progress) setlist for a show date."""
        data = self._get(f"setlists/showdate/{showdate}.json")
        return self.parse_setlist(data, artist=artist)

    @staticmethod
    def parse_setlist(data: dict, artist: str = "phish") -> list[SetlistEntry]:
        entries = []
        for row in data.get("data", []):
            if artist and row.get("artist_slug") and row["artist_slug"] != artist:
                continue
            entries.append(
                SetlistEntry(
                    showdate=row["showdate"],
                    set_label=str(row.get("set", "1")),
                    position=int(row.get("position", 0)),
                    song=row.get("song", "").strip(),
                    songid=row.get("songid"),
                    gap=_int_or_none(row.get("gap")),
                    transition=TRANSITIONS.get(row.get("transition"), ","),
                    venue=row.get("venue", ""),
                    city=row.get("city", ""),
                    state=row.get("state", "") or row.get("country", ""),
                    footnote=(row.get("footnote") or "").strip(),
                )
            )
        entries.sort(key=lambda e: e.position)
        return entries

    # -- song stats ----------------------------------------------------------

    def all_songs(self) -> list[dict]:
        """Full song table: name, times played, debut, last played. Cache me."""
        data = self._get("songs.json")
        return data.get("data", [])

    def venue_for_date(self, showdate: str, artist: str = "phish") -> Optional[str]:
        """'Venue, City, ST' for a show date — used for debut-venue lookups."""
        data = self._get(f"shows/showdate/{showdate}.json")
        for s in data.get("data", []):
            if not artist or s.get("artist_name", "").lower() == artist:
                parts = [s.get("venue", ""), s.get("city", ""), s.get("state", "") or s.get("country", "")]
                joined = ", ".join(p for p in parts if p)
                if joined:
                    return joined
        return None

    # -- shows ---------------------------------------------------------------

    def shows_for_year(self, year: int, artist: str = "phish") -> list[dict]:
        data = self._get(f"shows/showyear/{year}.json", order_by="showdate")
        return [
            s for s in data.get("data", [])
            if not artist or s.get("artist_name", "").lower() == artist
        ]


def _int_or_none(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
