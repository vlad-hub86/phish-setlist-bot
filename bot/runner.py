"""Core loop: poll setlist, diff against state, compose, publish.

Designed so a single `tick()` is fully testable — main.py just calls it on a
schedule during show windows.
"""
from __future__ import annotations

import logging
import time
from typing import Iterable, Optional

from . import composer
from .phishnet import PhishNetClient, SetlistEntry
from .publishers.base import Publisher
from .state import State

log = logging.getLogger(__name__)

SET_COMPLETE_IDLE_SECS = 35 * 60  # no new songs for 35 min => set is over
STATS_REFRESH_SECS = 7 * 24 * 3600

# Jam-length milestones (minutes), highest first. Length is estimated from when
# the song appeared on the live feed, so only the highest newly-crossed
# threshold is posted (lower ones are marked done silently).
MILESTONES = (40, 30, 20)
MILESTONES_IN_ENCORE = False  # encore end == show end, no "next song" ever
                              # arrives, so false positives are guaranteed


class Runner:
    def __init__(
        self,
        client: PhishNetClient,
        state: State,
        publishers: Iterable[Publisher],
        post_set_recaps: bool = False,
    ):
        self.client = client
        self.state = state
        self.publishers = list(publishers)
        self.post_set_recaps = post_set_recaps
        self._last_new_song_at: Optional[float] = None
        self._entries: list[SetlistEntry] = []

    # ------------------------------------------------------------------ ticks

    def tick(self, showdate: str, now: Optional[float] = None) -> int:
        """One poll cycle. Returns number of new songs posted."""
        now = now or time.time()
        self.maybe_refresh_stats()
        try:
            entries = self.client.setlist_for_date(showdate)
        except Exception:
            log.exception("setlist fetch failed; will retry next tick")
            return 0

        posted = 0
        for entry in entries:
            if not entry.song:
                continue
            self.state.record_sighting(entry.key, entry.song, int(now))
            if self._post_song(entry, entries):
                posted += 1

        if posted:
            self._last_new_song_at = now
        self._entries = entries

        self._check_milestones(entries, now)
        self._maybe_post_set_recaps(entries, now)
        return posted

    # ------------------------------------------------------------- posting

    def _post_song(self, entry: SetlistEntry, all_entries: list[SetlistEntry]) -> bool:
        new_anywhere = False
        num_in_set = sum(
            1 for e in all_entries
            if e.set_label == entry.set_label and e.position <= entry.position
        )
        # headline + stats block (gap / debut / original artist)
        stats = self.state.song_stats(songid=entry.songid, song=entry.song)
        if (
            stats
            and stats.get("debut")
            and not stats.get("debut_venue")
            and getattr(self.client, "api_key", None)
        ):
            try:  # one-time lookup per song, cached in the state DB
                venue = self.client.venue_for_date(stats["debut"])
                if venue:
                    self.state.set_debut_venue(stats["songid"], venue)
                    stats["debut_venue"] = venue
            except Exception:
                log.exception("debut-venue lookup failed for %s", entry.song)
        text = composer.song_post_stats(
            entry,
            stats,
            first_in_set=(num_in_set == 1),
            started_at=self.state.first_seen(entry.key),
        )

        for pub in self.publishers:
            if self.state.already_posted(entry.key, pub.name):
                continue
            try:
                remote_id = pub.post(text)
            except Exception:
                log.exception("post to %s failed for %s; will retry next tick", pub.name, entry.song)
                continue
            self.state.mark_posted(entry.key, entry.song, pub.name, remote_id)
            new_anywhere = True
            log.info("posted %s (%s) to %s", entry.song, entry.set_display, pub.name)
        return new_anywhere

    def _check_milestones(self, entries: list[SetlistEntry], now: float):
        """If the most recent song has been 'current' past a threshold, post.

        Caveat: we only know when songs START. If the set ends and nothing new
        is added, the set closer looks like it's still going — so a milestone
        can occasionally fire during a setbreak. Encores are excluded entirely
        (the show ending guarantees a false positive there).
        """
        if not entries:
            return
        current = entries[-1]
        if current.set_label.lower().startswith("e") and not MILESTONES_IN_ENCORE:
            return
        first_seen = self.state.first_seen(current.key)
        if first_seen is None:
            return
        elapsed_min = (now - first_seen) / 60
        crossed = [t for t in MILESTONES if elapsed_min >= t]
        if not crossed:
            return

        highest = crossed[0]
        text = composer.milestone_post(current, highest, int(elapsed_min))
        for pub in self.publishers:
            if self.state.milestone_posted(current.key, highest, pub.name):
                continue
            try:
                pub.post(text)
            except Exception:
                log.exception("milestone post to %s failed", pub.name)
                continue
            # mark this and all lower thresholds so they never fire late
            for t in crossed:
                self.state.mark_milestone(current.key, t, pub.name)
            log.info("posted %d-min milestone for %s to %s", highest, current.song, pub.name)

    # -------------------------------------------------------------- recaps

    def estimated_durations(self, showdate: str) -> dict[tuple, Optional[int]]:
        """Seconds per song, from consecutive first-seen timestamps.

        Duration is only computable when the NEXT song is in the same set
        (crossing a set boundary would include the setbreak). Last song of
        each set: None.
        """
        rows = self.state.sightings_for_show(showdate)
        durations: dict[tuple, Optional[int]] = {}
        for i, row in enumerate(rows):
            key = (row["showdate"], row["set_label"], row["position"])
            nxt = rows[i + 1] if i + 1 < len(rows) else None
            if nxt and nxt["set_label"] == row["set_label"]:
                durations[key] = int(nxt["first_seen"] - row["first_seen"])
            else:
                durations[key] = None
        return durations

    def post_lengths_recap(self, showdate: str, entries: Optional[list[SetlistEntry]] = None):
        """Threaded recap: every song with its (estimated) length."""
        entries = entries or self._entries or self.client.setlist_for_date(showdate)
        if not entries:
            return
        durations = self.estimated_durations(showdate)
        per_set: list[tuple[str, list[tuple[str, Optional[int]]]]] = []
        for e in entries:
            secs = durations.get(e.key)
            if per_set and per_set[-1][0] == e.set_display:
                per_set[-1][1].append((e.song, secs))
            else:
                per_set.append((e.set_display, [(e.song, secs)]))

        posts = composer.lengths_recap_posts(showdate, per_set, estimated=True)
        for pub in self.publishers:
            if self.state.recap_posted(showdate, "LENGTHS", pub.name):
                continue
            try:
                reply_to = None
                for text in posts:
                    reply_to = pub.post(text, in_reply_to=reply_to)
                self.state.mark_recap(showdate, "LENGTHS", pub.name)
            except Exception:
                log.exception("lengths recap to %s failed", pub.name)

    def _maybe_post_set_recaps(self, entries: list[SetlistEntry], now: float):
        """Post a recap for set N once set N+1 has started, or after long idle.

        Off by default (FTR style doesn't do set recaps); enable with
        POST_SET_RECAPS=1.
        """
        if not self.post_set_recaps or not entries:
            return
        sets_seen = []
        for e in entries:
            if e.set_label not in sets_seen:
                sets_seen.append(e.set_label)

        # every set except the latest is definitely complete
        complete = sets_seen[:-1]
        # the latest set is complete if we've been idle long enough
        idle = self._last_new_song_at and (now - self._last_new_song_at) > SET_COMPLETE_IDLE_SECS
        if idle:
            complete = sets_seen

        showdate = entries[0].showdate
        for set_label in complete:
            text = composer.set_recap_post(entries, set_label)
            if not text:
                continue
            for pub in self.publishers:
                if self.state.recap_posted(showdate, set_label, pub.name):
                    continue
                try:
                    pub.post(text)
                except Exception:
                    log.exception("set recap to %s failed", pub.name)
                    continue
                self.state.mark_recap(showdate, set_label, pub.name)
                log.info("posted recap for set %s to %s", set_label, pub.name)

    def post_show_recap(self, showdate: str):
        """Called once after the show window closes."""
        entries = self._entries or self.client.setlist_for_date(showdate)
        if not entries:
            return
        stats_by_key = {
            e.key: (self.state.song_stats(songid=e.songid, song=e.song) or {})
            for e in entries
        }
        text = composer.show_recap_post(entries, stats_by_key)
        for pub in self.publishers:
            if self.state.recap_posted(showdate, "SHOW", pub.name):
                continue
            try:
                pub.post(text)
                self.state.mark_recap(showdate, "SHOW", pub.name)
            except Exception:
                log.exception("show recap to %s failed", pub.name)

        # follow with the song-lengths thread
        self.post_lengths_recap(showdate, entries)

    # ------------------------------------------------------------- stats cache

    def maybe_refresh_stats(self):
        if not getattr(self.client, "api_key", None):
            return  # no key (replay/test mode) — use whatever is cached
        age = self.state.stats_age_seconds()
        if age is not None and age < STATS_REFRESH_SECS:
            return
        try:
            songs = self.client.all_songs()
        except Exception:
            log.exception("song stats refresh failed; keeping stale cache")
            return
        if songs:
            self.state.upsert_song_stats(songs)
            log.info("refreshed song stats cache (%d songs)", len(songs))
