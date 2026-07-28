#!/usr/bin/env python3
"""Backfill docs/setlists/ for a whole year and maintain docs/upcoming.json.

Runs on a GitHub-hosted runner via the push-inbox job hook (see
deploy/inbox_apply.py). Requires PHISHNET_API_KEY in the environment —
push-inbox.yml passes it from Actions secrets. The key must NEVER be
written into any output file: everything under docs/ is public.

What it does, per run (idempotent, incremental):

1. Pulls the year's shows from Phish.net (/v5/shows/showyear/<year>).
2. Writes docs/upcoming.json — announced shows from today onward.
3. For each PAST show with no docs/setlists/<date>.json yet, builds the
   feed file in bot/site.py's schema: sets/songs with transitions and
   footnotes from Phish.net, plus a "phishin" track map (listen links,
   real durations, tags with note + banter excerpt) aligned by set and
   position, plus a "gaps" map (song slug -> shows since last played)
   from Phish.net's per-song gap field.
4. Maintains docs/setlists/index.json exactly like bot/site.py does.
5. Maintains docs/song_meta_auto.json — per-song debut date (earliest
   phish.in recording), cover/original + artist, and recorded-play
   count, fetched once per new song and cached forever.

Existing files are never overwritten: the four curated MSG/Syracuse
shows keep their hand-written notes and track maps, and the live
show-night pipeline owns tonight's file.

Banter transcripts are phish.in's work: only a ~280-character excerpt
is stored, never the whole thing. The site clips further and links to
the full transcript on phish.in.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

YEAR = os.environ.get("BACKFILL_YEAR", "2026")
KEY = os.environ.get("PHISHNET_API_KEY", "")
SETLISTS = Path("docs/setlists")
UPCOMING = Path("docs/upcoming.json")
META_AUTO = Path("docs/song_meta_auto.json")
UA = "phish-setlist-bot-backfill/1.0 (mikeside.com; credit: phish.net + phish.in)"
SLEEP = 0.25          # be a polite API citizen
EXCERPT = 280         # max stored banter excerpt length


def get_json(url: str, tries: int = 3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if attempt == tries - 1:
                raise
        except Exception:
            if attempt == tries - 1:
                raise
        time.sleep(1.5 * (attempt + 1))
    return None


def pn(path: str):
    """Phish.net v5 call. Never let the key echo into logs or files."""
    data = get_json(f"https://api.phish.net/v5/{path}.json?apikey={KEY}")
    time.sleep(SLEEP)
    if not data or data.get("error"):
        return []
    return data.get("data") or []


def pi(path: str):
    data = get_json(f"https://phish.in/api/v2/{path}")
    time.sleep(SLEEP)
    return data


def slugify(title: str) -> str:
    """EXACTLY build_setlistlizard.py's slugify — these keys join the pies."""
    out = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return out or "untitled"


def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (t or "").lower())


SET_DISPLAY = {"1": "Set 1", "2": "Set 2", "3": "Set 3", "4": "Set 4",
               "e": "Encore", "e2": "Encore 2", "e3": "Encore 3"}
PI_SET_LABEL = {"Set 1": "1", "Set 2": "2", "Set 3": "3", "Set 4": "4",
                "Encore": "e", "Encore 2": "e2", "Encore 3": "e3"}


def trans_mark(row: dict) -> str:
    m = (row.get("trans_mark") or "").strip()
    if m:
        return m
    return {1: ",", 2: ">", 3: "->"}.get(row.get("transition"), "")


def build_feed(date: str, rows: list[dict]) -> dict | None:
    """One docs/setlists/<date>.json payload from Phish.net rows + phish.in."""
    rows = [r for r in rows if (r.get("artist_name") or "Phish") == "Phish"]
    if not rows:
        return None
    rows.sort(key=lambda r: (str(r.get("set") or "1"), int(r.get("position") or 0)))

    sets: list[dict] = []
    gaps: dict[str, int] = {}
    for r in rows:
        label = str(r.get("set") or "1").lower()
        if not sets or sets[-1]["label"] != label:
            sets.append({"label": label,
                         "display": SET_DISPLAY.get(label, f"Set {label}"),
                         "songs": []})
        title = (r.get("song") or "").strip()
        sets[-1]["songs"].append({
            "title": title,
            "transition": trans_mark(r),
            "length_secs": None,
            "footnote": (r.get("footnote") or "").strip() or None,
        })
        try:
            g = int(r.get("gap") or 0)
        except (TypeError, ValueError):
            g = 0
        if title and g > 0:
            gaps.setdefault(slugify(title), g)

    first = rows[0]
    payload = {
        "updated_at": datetime.utcnow().replace(microsecond=0).isoformat() + "+00:00",
        "showdate": date,
        "venue": first.get("venue"),
        "city": first.get("city"),
        "state": first.get("state"),
        "complete": True,
        "phishnet_url": first.get("permalink") or None,
        "sets": sets,
        "gaps": gaps,
    }

    tmap = phishin_map(date, sets)
    if tmap:
        payload["phishin"] = {"show": f"https://phish.in/{date}", "tracks": tmap}
    return payload


def phishin_map(date: str, sets: list[dict]) -> dict:
    """Align phish.in's tracks to our set/position keys.

    Same one map both paths use: a fresh backfill and the enrichment pass over
    a show the live pipeline captured. Returns {} when the recording hasn't
    posted yet, which is the normal state for a day or two after the show.
    """
    show = pi(f"shows/{date}.json")
    tracks = (show or {}).get("tracks") or []
    if not tracks:
        return {}

    by_set: dict[str, list[dict]] = {}
    for t in tracks:
        lab = PI_SET_LABEL.get(t.get("set_name") or "", None)
        if lab is None:
            lab = "e" if "encore" in (t.get("set_name") or "").lower() else "1"
        by_set.setdefault(lab, []).append(t)

    tmap: dict[str, dict] = {}
    for s in sets:
        lab = str(s.get("label", "1"))
        songs = s.get("songs") or []
        plist = by_set.get(lab, [])
        n_eq = len(plist) == len(songs)
        used: set[int] = set()
        for i, song in enumerate(songs):
            t = None
            if n_eq:
                t = plist[i]
            else:  # fall back to title matching within the set
                want = norm_title(song.get("title"))
                for j, cand in enumerate(plist):
                    if j not in used and norm_title(cand.get("title")) == want:
                        t, _ = cand, used.add(j)
                        break
            if not t or not t.get("slug"):
                continue
            entry: dict = {"u": f"https://phish.in/{date}/{t['slug']}"}
            if t.get("duration"):
                entry["d"] = int(round(t["duration"] / 1000))
            tags = []
            for tg in t.get("tags") or []:
                name = tg.get("name")
                if not name:
                    continue
                tag: dict = {"name": name}
                if tg.get("notes"):
                    tag["notes"] = tg["notes"]
                tr = (tg.get("transcript") or "").strip()
                if tr:  # excerpt only — the full transcript stays phish.in's
                    clip = " ".join(tr.split())[:EXCERPT].rstrip()
                    if len(tr) > EXCERPT:
                        clip = clip.rsplit(" ", 1)[0] + " …"
                    tag["transcript"] = clip
                tags.append(tag)
            if tags:
                entry["tags"] = tags
            tmap[f"{lab}:{i + 1}"] = entry
    return tmap


def gap_map(date: str) -> tuple[dict, str | None]:
    """Per-song gaps + the show's Phish.net permalink."""
    rows = pn(f"setlists/showdate/{date}")
    gaps: dict[str, int] = {}
    permalink = None
    for r in rows:
        if (r.get("artist_name") or "Phish") != "Phish":
            continue
        permalink = permalink or r.get("permalink")
        title = (r.get("song") or "").strip()
        try:
            g = int(r.get("gap") or 0)
        except (TypeError, ValueError):
            g = 0
        if title and g > 0:
            gaps.setdefault(slugify(title), g)
    return gaps, permalink


def enrich_existing(path: Path, date: str) -> bool:
    """Fill in what a live-captured show file is still missing.

    The show-night pipeline writes the setlist as it happens, so its file has
    no phish.in track map (the recording posts a day or two later) and no gap
    map. This adds both WITHOUT touching anything already present — curated
    notes, footnotes, media and hand-built track maps all survive untouched.
    Returns True if the file changed.
    """
    try:
        show = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return False

    changed = []
    if not ((show.get("phishin") or {}).get("tracks")):
        tmap = phishin_map(date, show.get("sets") or [])
        if tmap:
            show["phishin"] = {"show": f"https://phish.in/{date}", "tracks": tmap}
            changed.append(f"{len(tmap)} listen links")

    if not show.get("gaps") or not show.get("phishnet_url"):
        gaps, permalink = gap_map(date)
        if gaps and not show.get("gaps"):
            show["gaps"] = gaps
            changed.append(f"{len(gaps)} gaps")
        if permalink and not show.get("phishnet_url"):
            show["phishnet_url"] = permalink
            changed.append("phish.net link")

    if not changed:
        return False
    path.write_text(json.dumps(show, indent=1, ensure_ascii=False) + "\n")
    print(f"  {date}: enriched — {', '.join(changed)}")
    return True


def upsert_index(payload: dict) -> None:
    idx_path = SETLISTS / "index.json"
    idx = {"shows": []}
    if idx_path.exists():
        try:
            idx = json.loads(idx_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    shows = idx.setdefault("shows", [])
    core = {"showdate": payload["showdate"], "venue": payload.get("venue"),
            "city": payload.get("city"), "state": payload.get("state"),
            "complete": True}
    entry = next((s for s in shows if s.get("showdate") == core["showdate"]), None)
    if entry:
        for k, v in core.items():
            entry.setdefault(k, v)
    else:
        shows.append(core)
    shows.sort(key=lambda s: s.get("showdate") or "", reverse=True)
    idx_path.write_text(json.dumps(idx, indent=1) + "\n")


def refresh_song_meta_auto() -> None:
    """Debut / artist / play-count per song, cached across runs."""
    auto = {"generated_at": "", "songs": {}}
    if META_AUTO.exists():
        try:
            auto = json.loads(META_AUTO.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    songs = auto.setdefault("songs", {})

    # every (site slug -> phish.in slug) pair seen in any feed's track map,
    # plus titles with no phish.in track (they get title-derived slugs only)
    wanted: dict[str, str | None] = {}
    for f in sorted(SETLISTS.glob("*.json")):
        if f.name == "index.json":
            continue
        try:
            show = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        tmap = (show.get("phishin") or {}).get("tracks") or {}
        for s in show.get("sets", []):
            lab = str(s.get("label", "1"))
            for i, song in enumerate(s.get("songs", [])):
                site_slug = slugify(song.get("title") or "")
                t = tmap.get(f"{lab}:{i + 1}")
                pi_slug = None
                if t and t.get("u"):
                    pi_slug = t["u"].rstrip("/").rsplit("/", 1)[-1]
                if site_slug not in wanted or pi_slug:
                    wanted[site_slug] = pi_slug

    todo = [(k, v) for k, v in wanted.items() if k not in songs and v]
    print(f"song_meta_auto: {len(songs)} cached, {len(todo)} to fetch")
    for n, (site_slug, pi_slug) in enumerate(sorted(todo), 1):
        rec: dict = {"pi_slug": pi_slug}
        song = pi(f"songs/{pi_slug}.json")
        if song:
            rec["original"] = bool(song.get("original"))
            if song.get("artist"):
                rec["artist"] = song["artist"]
            if song.get("tracks_count"):
                rec["plays_recorded"] = song["tracks_count"]
        tr = pi(f"tracks.json?song_slug={pi_slug}&sort=date:asc&per_page=1")
        first = ((tr or {}).get("tracks") or [None])[0] or {}
        debut = first.get("show_date") or first.get("date")
        if debut:
            rec["debut"] = str(debut)[:10]
        songs[site_slug] = rec
        if n % 20 == 0:
            print(f"  ... {n}/{len(todo)}")
    auto["generated_at"] = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    META_AUTO.write_text(json.dumps(auto, indent=1, ensure_ascii=False) + "\n")


def main() -> int:
    if not KEY:
        print("PHISHNET_API_KEY is not set — aborting (add it to the "
              "push-inbox.yml job env from Actions secrets)", file=sys.stderr)
        return 1
    SETLISTS.mkdir(parents=True, exist_ok=True)

    year_shows = [s for s in pn(f"shows/showyear/{YEAR}")
                  if (s.get("artist_name") or s.get("artistname") or "Phish") == "Phish"]
    seen: set[str] = set()
    shows = []
    for s in sorted(year_shows, key=lambda s: s.get("showdate") or ""):
        d = s.get("showdate")
        if d and d not in seen:
            seen.add(d)
            shows.append(s)
    print(f"phish.net lists {len(shows)} Phish shows in {YEAR}")

    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    upcoming = [{"showdate": s["showdate"], "venue": s.get("venue"),
                 "city": s.get("city"), "state": s.get("state")}
                for s in shows if s["showdate"] >= today]
    UPCOMING.write_text(json.dumps(
        {"generated_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
         "shows": upcoming}, indent=1, ensure_ascii=False) + "\n")
    print(f"upcoming.json: {len(upcoming)} announced shows from {today} on")

    done = skipped = enriched = 0
    for s in shows:
        d = s["showdate"]
        if d >= today:            # tonight belongs to the live pipeline
            continue
        path = SETLISTS / f"{d}.json"
        if path.exists():
            # A show the live pipeline captured still needs its audio map and
            # gaps once phish.in posts the recording — top it up in place.
            if enrich_existing(path, d):
                enriched += 1
            else:
                skipped += 1
            continue
        rows = pn(f"setlists/showdate/{d}")
        payload = build_feed(d, rows)
        if not payload:
            print(f"  {d}: no setlist on phish.net yet — skipped")
            continue
        (SETLISTS / f"{d}.json").write_text(
            json.dumps(payload, indent=1, ensure_ascii=False) + "\n")
        upsert_index(payload)
        n_songs = sum(len(x["songs"]) for x in payload["sets"])
        n_links = len((payload.get("phishin") or {}).get("tracks") or {})
        print(f"  {d}: {payload.get('venue')} — {n_songs} songs, "
              f"{n_links} listen links, {len(payload.get('gaps') or {})} gaps")
        done += 1

    refresh_song_meta_auto()
    print(f"backfill complete: {done} new shows, {enriched} enriched, "
          f"{skipped} already complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
