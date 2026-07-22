"""SQLite state: what we've posted, cached song stats, post log."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS posted_songs (
    showdate TEXT NOT NULL,
    set_label TEXT NOT NULL,
    position INTEGER NOT NULL,
    song TEXT NOT NULL,
    platform TEXT NOT NULL,
    posted_at INTEGER NOT NULL,
    remote_id TEXT,
    PRIMARY KEY (showdate, set_label, position, platform)
);
CREATE TABLE IF NOT EXISTS song_stats (
    songid INTEGER PRIMARY KEY,
    song TEXT NOT NULL,
    slug TEXT,
    times_played INTEGER,
    debut TEXT,
    last_played TEXT,
    artist TEXT,
    debut_venue TEXT,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sightings (
    showdate TEXT NOT NULL,
    set_label TEXT NOT NULL,
    position INTEGER NOT NULL,
    song TEXT NOT NULL,
    first_seen INTEGER NOT NULL,
    PRIMARY KEY (showdate, set_label, position)
);
CREATE TABLE IF NOT EXISTS milestones (
    showdate TEXT NOT NULL,
    set_label TEXT NOT NULL,
    position INTEGER NOT NULL,
    threshold INTEGER NOT NULL,
    platform TEXT NOT NULL,
    posted_at INTEGER NOT NULL,
    PRIMARY KEY (showdate, set_label, position, threshold, platform)
);
CREATE TABLE IF NOT EXISTS set_recaps (
    showdate TEXT NOT NULL,
    set_label TEXT NOT NULL,
    platform TEXT NOT NULL,
    posted_at INTEGER NOT NULL,
    PRIMARY KEY (showdate, set_label, platform)
);
"""


class State:
    def __init__(self, path: str | Path = "botstate.db"):
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        # migrate older state DBs (restored from cache) in place
        for col in ("artist", "debut_venue"):
            try:
                self.db.execute(f"ALTER TABLE song_stats ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists

    # -- posted songs (idempotency) -----------------------------------------

    def already_posted(self, key: tuple, platform: str) -> bool:
        showdate, set_label, position = key
        row = self.db.execute(
            "SELECT 1 FROM posted_songs WHERE showdate=? AND set_label=? AND position=? AND platform=?",
            (showdate, set_label, position, platform),
        ).fetchone()
        return row is not None

    def mark_posted(self, key: tuple, song: str, platform: str, remote_id: Optional[str] = None):
        showdate, set_label, position = key
        self.db.execute(
            "INSERT OR IGNORE INTO posted_songs VALUES (?,?,?,?,?,?,?)",
            (showdate, set_label, position, song, platform, int(time.time()), remote_id),
        )
        self.db.commit()

    def recap_posted(self, showdate: str, set_label: str, platform: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM set_recaps WHERE showdate=? AND set_label=? AND platform=?",
            (showdate, set_label, platform),
        ).fetchone() is not None

    def mark_recap(self, showdate: str, set_label: str, platform: str):
        self.db.execute(
            "INSERT OR IGNORE INTO set_recaps VALUES (?,?,?,?)",
            (showdate, set_label, platform, int(time.time())),
        )
        self.db.commit()

    # -- sightings (when we first saw each song appear) ----------------------

    def record_sighting(self, key: tuple, song: str, now: int):
        showdate, set_label, position = key
        self.db.execute(
            "INSERT OR IGNORE INTO sightings VALUES (?,?,?,?,?)",
            (showdate, set_label, position, song, int(now)),
        )
        self.db.commit()

    def first_seen(self, key: tuple) -> Optional[int]:
        showdate, set_label, position = key
        row = self.db.execute(
            "SELECT first_seen FROM sightings WHERE showdate=? AND set_label=? AND position=?",
            (showdate, set_label, position),
        ).fetchone()
        return int(row["first_seen"]) if row else None

    def sightings_for_show(self, showdate: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM sightings WHERE showdate=? ORDER BY position", (showdate,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- jam milestones ------------------------------------------------------

    def milestone_posted(self, key: tuple, threshold: int, platform: str) -> bool:
        showdate, set_label, position = key
        return self.db.execute(
            "SELECT 1 FROM milestones WHERE showdate=? AND set_label=? AND position=? AND threshold=? AND platform=?",
            (showdate, set_label, position, threshold, platform),
        ).fetchone() is not None

    def mark_milestone(self, key: tuple, threshold: int, platform: str):
        showdate, set_label, position = key
        self.db.execute(
            "INSERT OR IGNORE INTO milestones VALUES (?,?,?,?,?,?)",
            (showdate, set_label, position, threshold, platform, int(time.time())),
        )
        self.db.commit()

    # -- song stats cache ----------------------------------------------------

    def upsert_song_stats(self, rows: list[dict]):
        """Refresh song data without wiping the lazily-fetched debut_venue."""
        now = int(time.time())
        for r in rows:
            self.db.execute(
                """INSERT INTO song_stats (songid, song, slug, times_played, debut, last_played, artist, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(songid) DO UPDATE SET
                     song=excluded.song, slug=excluded.slug, times_played=excluded.times_played,
                     debut=excluded.debut, last_played=excluded.last_played,
                     artist=excluded.artist, updated_at=excluded.updated_at""",
                (
                    r.get("songid"),
                    r.get("song"),
                    r.get("slug"),
                    _i(r.get("times_played")),
                    r.get("debut"),
                    r.get("last_played"),
                    r.get("artist"),
                    now,
                ),
            )
        self.db.commit()

    def set_debut_venue(self, songid: int, venue: str):
        self.db.execute("UPDATE song_stats SET debut_venue=? WHERE songid=?", (venue, songid))
        self.db.commit()

    def song_stats(self, songid: Optional[int] = None, song: Optional[str] = None) -> Optional[dict]:
        if songid is not None:
            row = self.db.execute("SELECT * FROM song_stats WHERE songid=?", (songid,)).fetchone()
        elif song:
            row = self.db.execute(
                "SELECT * FROM song_stats WHERE song=? COLLATE NOCASE", (song,)
            ).fetchone()
        else:
            return None
        return dict(row) if row else None

    def stats_age_seconds(self) -> Optional[int]:
        row = self.db.execute("SELECT MAX(updated_at) AS m FROM song_stats").fetchone()
        return int(time.time()) - row["m"] if row and row["m"] else None


def _i(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
