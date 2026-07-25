"""Turn setlist entries + stats into post text. No URLs, ever (X charges 13x for links)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from .phishnet import SetlistEntry

BUSTOUT_GAP = 50  # gap threshold for bustout flair
MAX_LEN = 280     # X limit; Truth Social allows more but we compose once

# Footnote markers. Superscript digits, numbered per post so a recap with
# several teases maps each mark to exactly one note. (Previously a dagger "†",
# which reads as a cross in a social feed.) Past 9 notes we fall back to "(10)"
# — no superscript glyph exists for multi-digit numbers that renders reliably.
_SUPERSCRIPTS = "¹²³⁴⁵⁶⁷⁸⁹"


def _sup(n: int) -> str:
    """Superscript footnote marker for note n (1-based)."""
    return _SUPERSCRIPTS[n - 1] if 1 <= n <= len(_SUPERSCRIPTS) else f"({n})"


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


def show_start_post(entry: SetlistEntry, started_at: Optional[float] = None) -> str:
    """Fired once, when the opener first appears in the feed.

    🦎 Lights down at Madison Square Garden.
    Phish, New York, NY — 8:07 PM ET.
    Your trip is short 🚀

    The timestamp is when the OPENER reached the feed, not when the band
    physically walked on — phish.net editors enter songs by hand and we poll
    every ~75s, so the true walk-on is somewhat earlier. The copy deliberately
    states a time without claiming it is the walk-on.
    """
    venue = (entry.venue or "").strip()
    loc = ", ".join(x for x in (entry.city, entry.state) if x)
    lines = [f"\U0001f98e Lights down at {venue}." if venue else "\U0001f98e Lights down."]
    when = _fmt_clock(started_at) if started_at else ""
    if loc and when:
        lines.append(f"Phish, {loc} — {when}.")
    elif loc:
        lines.append(f"Phish, {loc}.")
    elif when:
        lines.append(f"Phish — {when}.")
    lines.append("Your trip is short \U0001f680")
    return _clamp("\n".join(lines))


def song_post_stats(
    entry: SetlistEntry,
    stats: Optional[dict],
    first_in_set: bool,
    started_at: Optional[float] = None,
    prev_song: Optional[str] = None,
    prev_minutes: Optional[int] = None,
) -> str:
    """Per-song post with a stats block:

    SET TWO: Rock and Roll [9:47 PM ET]
    Gap: 23 shows
    Debut: 1998 · Thomas & Mack Center, Las Vegas, NV
    Originally performed by: The Velvet Underground

    Previous Song: Everything's Right [12 min]
    """
    lines = [ftr_song_post(entry, first_in_set, started_at)]
    if entry.gap is not None and entry.gap >= 1:
        lines.append(f"Gap: {entry.gap} show" + ("s" if entry.gap != 1 else ""))
    if stats and stats.get("debut"):
        year = str(stats["debut"])[:4]
        venue = stats.get("debut_venue")
        lines.append(f"Debut: {year} · {venue}" if venue else f"Debut: {year}")
    artist = (stats or {}).get("artist")
    if artist and artist.strip().lower() != "phish":
        lines.append(f"Originally performed by: {artist}")
    if prev_song:
        lines.append("")
        tail = f" [{prev_minutes} min]" if prev_minutes else ""
        lines.append(f"Previous Song: {prev_song}{tail}")
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
        lines.append(f"{_sup(1)} {entry.footnote}")

    return _clamp("\n".join(lines))


def set_recap_post(
    entries: list[SetlistEntry],
    set_label: str,
    durations: Optional[dict] = None,
) -> str:
    """End-of-set recap with per-song lengths and footnotes:

    SET TWO RECAP (8 songs)
    Sand [20 min] ¹
    Everything's Right [12 min]
    ...

    ¹ Sand: Sanford and Son tease
    """
    in_set = [e for e in entries if e.set_label == set_label]
    if not in_set:
        return ""
    s = set_label.lower()
    if s.startswith("e"):
        name = "ENCORE" if s == "e" else f"ENCORE {s[1:]}"
    else:
        name = f"SET {SET_WORDS.get(set_label, set_label)}"
    header = f"{name} RECAP ({len(in_set)} songs)"

    # Footnote markers are assigned once, in set order, so numbering is stable
    # no matter which rendering we end up emitting.
    notes: list[str] = []
    marked: list[tuple] = []
    for e in in_set:
        mark = ""
        if e.footnote:
            marker = _sup(len(notes) + 1)
            notes.append(f"{marker} {e.song}: {e.footnote}")
            mark = marker
        marked.append((e, mark))

    def render(with_lengths: bool) -> str:
        parts = []
        for i, (e, mark) in enumerate(marked):
            secs = (durations or {}).get(e.key)
            length = f" [{round(secs / 60)} min]" if with_lengths and secs and secs >= 60 else ""
            piece = f"{e.song}{length}{mark}"
            if i < len(marked) - 1:
                tr = (getattr(e, "transition", "") or "").strip()
                # ">" / "->" are segues and get spaces so they read as arrows;
                # anything else (a comma, or no mark recorded yet) is a plain
                # separator.
                piece += f" {tr} " if tr in (">", "->") else ", "
            parts.append(piece)
        body = "".join(parts)
        out = [header, body]
        if notes:
            out.append("")
            out.extend(notes)
        return "\n".join(out)

    # Prefer lengths, but a long set with them can blow past MAX_LEN and get
    # truncated mid-setlist — losing the encore is worse than losing estimated
    # durations, which the phish.in thread reports accurately anyway.
    text = render(True)
    if len(text) > MAX_LEN:
        text = render(False)
    return _clamp(text)


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
