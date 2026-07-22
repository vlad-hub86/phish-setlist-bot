"""Turn setlist entries + stats into post text. No URLs, ever (X charges 13x for links)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from .phishnet import SetlistEntry

BUSTOUT_GAP = 50  # gap threshold for bustout flair
MAX_LEN = 280     # X limit; Truth Social allows more but we compose once


def _fmt_date(iso: Optional[str]) -> str:
    if not iso:
        return "?"
    try:
        d = datetime.strptime(iso, "%Y-%m-%d")
        return f"{d.month}/{d.day}/{d.strftime('%y')}"
    except ValueError:
        return iso


SET_WORDS = {"1": "ONE", "2": "TWO", "3": "THREE", "4": "FOUR"}

TZ_LABELS = {
    "America/New_York": "ET",
    "America/Chicago": "CT",
    "America/Denver": "MT",
    "America/Los_Angeles": "PT",
}


def _fmt_clock(ts: float, tz_name: Optional[str] = None) -> str:
    """Unix timestamp -> '8:39 PM ET' in the show's timezone (SHOW_TZ env)."""
    import os
    from zoneinfo import ZoneInfo

    tz_name = tz_name or os.environ.get("SHOW_TZ", "America/New_York")
    d = datetime.fromtimestamp(ts, ZoneInfo(tz_name))
    label = TZ_LABELS.get(tz_name, d.strftime("%Z"))
    hour = str(int(d.strftime("%I")))  # no leading zero, portable
    return f"{hour}:{d.strftime('%M %p')} {label}"


def ftr_song_post(
    entry: SetlistEntry,
    first_in_set: bool,
    started_at: Optional[float] = None,
) -> str:
    """FTR/@PhishSet style plus a start-time stamp: 'SET TWO: Sand [8:39 PM ET]'."""
    if first_in_set:
        s = entry.set_label.lower()
        if s.startswith("e"):
            label = "ENCORE" if s == "e" else f"ENCORE {s[1:]}"
        else:
            label = f"SET {SET_WORDS.get(entry.set_label, entry.set_label)}"
        text = f"{label}: {entry.song}"
    else:
        text = entry.song
    if started_at:
        text += f" [{_fmt_clock(started_at)}]"
    return text


def song_post_stats(
    entry: SetlistEntry,
    stats: Optional[dict],
    first_in_set: bool,
    started_at: Optional[float] = None,
) -> str:
    """Per-song post with a stats block:

    SET TWO: Rock and Roll [9:47 PM ET]

    Gap: 23 shows
    Debut: 1998 · Thomas & Mack Center, Las Vegas, NV
    Originally performed by: The Velvet Underground
    """
    lines = [ftr_song_post(entry, first_in_set, started_at), ""]
    if entry.gap is not None and entry.gap >= 1:
        lines.append(f"Gap: {entry.gap} show" + ("s" if entry.gap != 1 else ""))
    if stats and stats.get("debut"):
        year = str(stats["debut"])[:4]
        venue = stats.get("debut_venue")
        lines.append(f"Debut: {year} · {venue}" if venue else f"Debut: {year}")
    artist = (stats or {}).get("artist")
    if artist and artist.strip().lower() != "phish":
        lines.append(f"Originally performed by: {artist}")
    if len(lines) == 2:  # no stats available — post just the headline
        lines = lines[:1]
    return _clamp("\n".join(lines))


def song_post(entry: SetlistEntry, stats: Optional[dict], song_number_in_set: int) -> str:
    lines = []

    # headline flair
    if entry.gap is not None and entry.gap >= BUSTOUT_GAP:
        lines.append(f"\U0001f6a8 BUSTOUT \U0001f6a8")
    elif stats and (stats.get("times_played") or 0) == 0:
        lines.append("\U0001f195 DEBUT")

    lines.append(f"\U0001f3b5 {entry.song}")
    loc = ", ".join(x for x in (entry.venue, entry.city, entry.state) if x)
    lines.append(f"{entry.set_display} · Song {song_number_in_set} · {loc}")

    if entry.gap is not None and entry.gap > 1:
        last = stats.get("last_played") if stats else None
        tail = f" (last {_fmt_date(last)})" if last else ""
        lines.append(f"Gap: {entry.gap} shows{tail}")

    if stats and stats.get("times_played"):
        n = stats["times_played"] + 1  # counting tonight
        debut_s = _fmt_date(stats.get("debut"))
        lines.append(f"Play #{n} since debut {debut_s}")

    if entry.footnote:
        lines.append(f"† {entry.footnote}")

    return _clamp("\n".join(lines))


def set_recap_post(entries: list[SetlistEntry], set_label: str) -> str:
    in_set = [e for e in entries if e.set_label == set_label]
    if not in_set:
        return ""
    parts = []
    for e in in_set:
        parts.append(e.song + (e.transition if e is not in_set[-1] else ""))
    body = " ".join(parts).replace(" ,", ",")
    head = f"{in_set[0].set_display} ({len(in_set)} songs):"
    return _clamp(f"{head}\n{body}")


def show_recap_post(entries: list[SetlistEntry], stats_by_key: dict) -> str:
    if not entries:
        return ""
    first = entries[0]
    loc = ", ".join(x for x in (first.venue, first.city, first.state) if x)
    lines = [f"{_fmt_date(first.showdate)} — {loc}", f"{len(entries)} songs"]

    notable = []
    for e in entries:
        st = stats_by_key.get(e.key)
        if st and (st.get("times_played") or 0) == 0:
            notable.append(f"\U0001f195 debut of {e.song}")
        elif e.gap is not None and e.gap >= BUSTOUT_GAP:
            notable.append(f"\U0001f6a8 {e.song} (gap {e.gap})")
    if notable:
        lines.append("Notable: " + ", ".join(notable[:4]))
    return _clamp("\n".join(lines))


def milestone_post(entry: SetlistEntry, threshold: int, elapsed_min: int) -> str:
    """A song has crossed a jam-length threshold (estimated from the live feed)."""
    loc = ", ".join(x for x in (entry.venue, entry.city) if x)
    if threshold >= 40:
        return _clamp(
            f"\U0001f6a8\U0001f410 40+ MINUTE JAM \U0001f410\U0001f6a8\n"
            f"{entry.song} has passed the 40-minute mark ({entry.set_display}, {loc}).\n"
            f"This is rarefied air — a handful of jams in Phish history have gone this long.\n"
            f"(est. from live feed)"
        )
    if threshold >= 30:
        return _clamp(
            f"\U0001f525 30+ MINUTES \U0001f525\n"
            f"{entry.song} is still going past the half-hour mark ({entry.set_display}, {loc}).\n"
            f"(est. from live feed)"
        )
    return _clamp(
        f"\U0001f552 20+ MINUTES\n"
        f"{entry.song} has passed the 20-minute mark ({entry.set_display}, {loc}).\n"
        f"(est. from live feed)"
    )


def _fmt_len(seconds: Optional[int], estimated: bool) -> str:
    if seconds is None:
        return "–"
    m, s = divmod(int(seconds), 60)
    if estimated:
        return f"~{m}m"
    return f"{m}:{s:02d}"


def lengths_recap_posts(
    showdate: str,
    per_set: list[tuple[str, list[tuple[str, Optional[int]]]]],
    estimated: bool = True,
) -> list[str]:
    """Thread: header + one post per set listing each song with its length.

    per_set: [(set_display, [(song, seconds_or_None), ...]), ...]
    """
    if not per_set:
        return []
    src = "est. from live feed — verified lengths when the recording posts" if estimated else "via phish.in"
    posts = [f"\U0001f553 Song lengths for {_fmt_date(showdate)} ({src}):"]
    for set_display, songs in per_set:
        lines = [f"{set_display}:"]
        for song, secs in songs:
            lines.append(f"{song} — {_fmt_len(secs, estimated)}")
        posts.append(_clamp("\n".join(lines)))
    return posts


def _clamp(text: str) -> str:
    if len(text) <= MAX_LEN:
        return text
    return text[: MAX_LEN - 1] + "…"
