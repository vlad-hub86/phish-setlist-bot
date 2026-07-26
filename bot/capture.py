"""Live-only signal capture — things that are gone forever if not recorded live.

Runs alongside the show-night poll and writes docs/capture/<showdate>.json,
which rides to GitHub in the same git push that ships setlist.json. Nothing
here can affect posting: construction and every tick are wrapped so a capture
failure logs and moves on.

What it records:

    first_seen   wall-clock timestamp per song, from the sightings the state
                 DB already keeps. Phish.net publishes no clock times and
                 phish.in only durations, so this is the one dataset nobody
                 else has: set lengths, setbreaks, encore breaks, pacing.
    edits        diffs to already-seen setlist rows (title / footnote /
                 transition changes) — "the setlist as believed at 10:47pm"
                 versus what it finally said.
    reddit       comment-count + score counters for the busiest fresh r/phish
                 thread, every ~5 minutes. Counters only, never comment text —
                 the site links out for the words.
    rating       Phish.net rating snapshots every ~20 minutes, for the
                 "how the take settled" curve.
    weather      one Open-Meteo reading (keyless) at the venue when the show
                 first appears. Matters for sheds; see 7/21 Syracuse.

Not attempted: live audio (LivePhish is paid/DRM) and soundcheck scraping.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

UA = "phish-setlist-bot/0.1 capture (contact: vlad@miajunefacialbar.com)"
REDDIT_EVERY_SECS = 300     # counters only; keep this polite
RATING_EVERY_SECS = 1200
REDDIT_FRESH_SECS = 16 * 3600


def _iso(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else datetime.now(timezone.utc)
    return dt.isoformat(timespec="seconds")


class Capture:
    def __init__(self, showdate: str, site_dir, state, client):
        self.showdate = showdate
        self.path = Path(site_dir) / "capture" / f"{showdate}.json"
        self.state = state
        self.client = client
        self.data = {
            "showdate": showdate,
            "first_seen": {},
            "edits": [],
            "reddit": [],
            "rating": [],
            "weather": None,
        }
        self._snapshot: dict = {}   # "set:pos" -> {title, footnote, transition}
        self._last_reddit = 0.0
        self._last_rating = 0.0
        self._weather_tried = False
        self._dirty = False
        # Survive runner restarts mid-show: reload what we already captured.
        # The edit snapshot rebuilds silently on the first tick (changes are
        # only recorded for keys the snapshot already holds), so a restart
        # can never fabricate a wave of bogus "edits".
        try:
            if self.path.exists():
                old = json.loads(self.path.read_text())
                if isinstance(old, dict):
                    for k in self.data:
                        if k in old:
                            self.data[k] = old[k]
        except Exception:
            log.exception("capture: could not load existing file; starting fresh")

    # ------------------------------------------------------------------ tick

    def tick(self, entries, now: float | None = None) -> None:
        """Record everything observable this poll. Never raises."""
        try:
            self._tick(list(entries or []), now or time.time())
        except Exception:
            log.exception("capture tick failed (posting unaffected)")

    def _tick(self, entries, now: float) -> None:
        for e in entries:
            if not e.song:
                continue
            k = f"{e.set_label}:{e.position}"
            if k not in self.data["first_seen"]:
                fs = None
                try:
                    fs = self.state.first_seen(e.key)
                except Exception:
                    pass
                self.data["first_seen"][k] = {
                    "song": e.song,
                    "set": e.set_label,
                    "pos": e.position,
                    "at": _iso(fs or now),
                }
                self._dirty = True
            cur = {
                "title": e.song,
                "footnote": e.footnote or "",
                "transition": (e.transition or "").strip(),
            }
            prev = self._snapshot.get(k)
            if prev is not None and prev != cur:
                self.data["edits"].append({
                    "at": _iso(now),
                    "set": e.set_label,
                    "pos": e.position,
                    "changes": {
                        f: {"from": prev.get(f, ""), "to": cur[f]}
                        for f in cur if prev.get(f) != cur[f]
                    },
                })
                self._dirty = True
            self._snapshot[k] = cur

        if entries:
            if not self._weather_tried:
                self._weather_tried = True
                self._grab_weather(entries[0])
            if now - self._last_reddit >= REDDIT_EVERY_SECS:
                self._last_reddit = now
                self._grab_reddit(now)
            if now - self._last_rating >= RATING_EVERY_SECS:
                self._last_rating = now
                self._grab_rating(now)
        self._flush()

    # ----------------------------------------------------------- collectors

    def _grab_weather(self, first) -> None:
        """One reading at the venue city when the show first appears."""
        try:
            g = requests.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": first.city or "", "count": 1},
                timeout=10, headers={"User-Agent": UA},
            ).json()
            res = (g.get("results") or [None])[0]
            if not res:
                return
            w = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": res["latitude"], "longitude": res["longitude"],
                    "current": "temperature_2m,apparent_temperature,precipitation,"
                               "weather_code,wind_speed_10m",
                },
                timeout=10, headers={"User-Agent": UA},
            ).json()
            self.data["weather"] = {
                "at": _iso(),
                "place": ", ".join(p for p in (first.city, first.state) if p),
                "lat": res["latitude"], "lon": res["longitude"],
                "current": w.get("current"),
            }
            self._dirty = True
        except Exception:
            log.exception("capture: weather fetch failed")

    def _grab_reddit(self, now: float) -> None:
        """Counters for the busiest fresh r/phish thread. No comment text."""
        try:
            r = requests.get(
                "https://www.reddit.com/r/phish/hot.json",
                params={"limit": 30}, timeout=10, headers={"User-Agent": UA},
            )
            if r.status_code != 200:
                return
            posts = [c.get("data", {}) for c in r.json().get("data", {}).get("children", [])]
            fresh = [
                p for p in posts
                if p.get("created_utc") and (now - p["created_utc"]) < REDDIT_FRESH_SECS
            ]
            if not fresh:
                return
            top = max(fresh, key=lambda p: p.get("num_comments") or 0)
            self.data["reddit"].append({
                "at": _iso(now),
                "thread_id": top.get("id"),
                "title": (top.get("title") or "")[:120],
                "num_comments": top.get("num_comments"),
                "score": top.get("score"),
            })
            self._dirty = True
        except Exception:
            log.exception("capture: reddit fetch failed")

    def _grab_rating(self, now: float) -> None:
        """Phish.net rating snapshot; the fields drift, so keep what exists."""
        try:
            data = self.client._get(f"shows/showdate/{self.showdate}.json")
        except Exception:
            return
        for row in data.get("data", []):
            keep = {
                k: row[k]
                for k in ("rating", "avg_rating", "reviews", "review_count", "votes")
                if row.get(k) not in (None, "", 0, "0")
            }
            if keep:
                self.data["rating"].append({"at": _iso(now), **keep})
                self._dirty = True
                break

    # ---------------------------------------------------------------- flush

    def _flush(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data["updated_at"] = _iso()
        self.path.write_text(json.dumps(self.data, indent=1))
        self._dirty = False
