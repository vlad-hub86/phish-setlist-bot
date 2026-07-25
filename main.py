#!/usr/bin/env python3
"""phish-setlist-bot entry point.

Commands:
  python main.py run                              # live mode: poll during tonight's show window
  python main.py once --date DATE                 # single tick for a date (testing)
  python main.py replay FIXTURE                   # replay a fixture file through the pipeline (dry-run)
  python main.py test-post "text"                 # send one real post to Truth Social (credentials check)
  python main.py test-post "text" --platform x    # send one real post to X (credentials check)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from datetime import date, datetime

from dotenv import load_dotenv

from bot.phishnet import PhishNetClient
from bot.publishers import DryRunPublisher, PhishPicksPublisher, TruthPublisher, XPublisher
from bot.runner import Runner
from bot.state import State

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

POLL_SECS = 75          # base poll interval during a show
POLL_JITTER = 15        # +/- jitter so we're not metronomic
WINDOW_START = (18, 30) # 6:30pm venue-agnostic local time (bot host runs in ET for US tours)
WINDOW_END = (1, 0)     # 1:00am


# Post kinds X is allowed to publish. Truth Social has no allowlist and takes
# everything, so it acts as the sandbox: a new post kind runs there first and
# is promoted to X by adding its name here (via the X_POST_KINDS env var).
# Deliberately excluded by default: "milestone" (documented false positives —
# a set closer looks like an endless jam once the feed goes quiet) and
# "lengths" (live durations are inferred from poll timestamps; the verified
# phish.in thread the next morning is the trustworthy one).
DEFAULT_X_POST_KINDS = "show_start,song,set_recap_1,show_recap"
DEFAULT_X_PREFIX = "\U0001f98e "  # lizard, prepended to every X post


def _x_from_env() -> XPublisher:
    """Build an X publisher from the four X_* OAuth env vars."""
    raw = os.environ.get("X_POST_KINDS", DEFAULT_X_POST_KINDS)
    kinds = {k.strip() for k in raw.split(",") if k.strip()}
    if "*" in kinds:  # escape hatch: accept every kind
        kinds = None
    return XPublisher(
        api_key=os.environ.get("X_API_KEY", ""),
        api_secret=os.environ.get("X_API_SECRET", ""),
        access_token=os.environ.get("X_ACCESS_TOKEN", ""),
        access_token_secret=os.environ.get("X_ACCESS_TOKEN_SECRET", ""),
        kinds=kinds,
        prefix=os.environ.get("X_PREFIX", DEFAULT_X_PREFIX),
    )


def _phishpicks_from_env() -> PhishPicksPublisher:
    """Build the phishpicks.net ingest publisher from its env vars.

    Structured song ingest (POST {"song","set"} with a bearer token). Only the
    ``song`` post kind is ever routed here (the endpoint is /ingest/song), so
    there is no post-kind allowlist to configure — unlike X.
    """
    # An empty URL makes the publisher fall back to its own DEFAULT_URL.
    return PhishPicksPublisher(
        token=os.environ.get("PHISHPICKS_TOKEN", ""),
        url=os.environ.get("PHISHPICKS_INGEST_URL", ""),
    )


def build_publishers(dry_run: bool):
    if dry_run:
        return [DryRunPublisher("truthsocial-dry")]
    pubs = []
    platforms = [p.strip() for p in os.environ.get("PLATFORMS", "truthsocial").split(",") if p.strip()]
    # Each publisher is constructed defensively: a platform whose credentials
    # are missing/revoked (or whose client library failed to install) must NOT
    # take down the other platform's live show coverage. Construction failures
    # were the one place the publisher isolation guarantee leaked — build_runner
    # runs before the first poll, so an exception here killed the whole night.
    for name, factory in (
        ("truthsocial", lambda: TruthPublisher(os.environ.get("TRUTH_BEARER_TOKEN", ""))),
        ("x", _x_from_env),
        ("phishpicks", _phishpicks_from_env),
    ):
        if name not in platforms:
            continue
        try:
            pubs.append(factory())
        except Exception:
            log.exception("publisher %r unavailable — continuing without it", name)
    if not pubs:
        raise RuntimeError(f"no publishers could be built for PLATFORMS={platforms!r}")
    return pubs


def build_runner(dry_run: bool) -> Runner:
    client = PhishNetClient(os.environ.get("PHISHNET_API_KEY", ""))
    state = State(os.environ.get("STATE_DB", "botstate.db"))
    return Runner(
        client,
        state,
        build_publishers(dry_run),
        post_set_recaps=os.environ.get("POST_SET_RECAPS", "1") in ("1", "true", "yes"),
    )


def in_window(now: datetime) -> bool:
    hm = (now.hour, now.minute)
    return hm >= WINDOW_START or hm < WINDOW_END


def cmd_run(args):
    runner = build_runner(args.dry_run)
    log.info("live mode; dry_run=%s", args.dry_run)
    while True:
        now = datetime.now()
        showdate = date.today().isoformat() if now.hour > 6 else None
        if showdate and in_window(now):
            runner.tick(showdate)
            time.sleep(POLL_SECS + random.uniform(-POLL_JITTER, POLL_JITTER))
        else:
            # outside window: if a show just ended, fire the recap once
            if runner._entries:
                runner.post_show_recap(runner._entries[0].showdate)
            time.sleep(300)


def cmd_show_window(args):
    """Single-job mode for GitHub Actions: poll for N minutes, then recap and exit.

    The show date is computed ONCE at start using US/Pacific — during any US
    evening show window, the Pacific calendar date equals the venue-local show
    date regardless of coast (UTC/Eastern flip at midnight mid-show; Pacific
    doesn't flip until 3am ET).
    """
    from zoneinfo import ZoneInfo

    showdate = datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    runner = build_runner(args.dry_run)

    # Skip the whole window on non-show days (one polite API call)
    try:
        year = int(showdate[:4])
        shows = runner.client.shows_for_year(year)
        if not any(s.get("showdate") == showdate for s in shows):
            log.info("no Phish show on %s — exiting", showdate)
            return
        log.info("show tonight (%s) — polling for %d minutes", showdate, args.minutes)
    except Exception:
        log.exception("show-schedule check failed; polling anyway")

    site_dir = os.environ.get("SITE_DIR", "")
    site_push = os.environ.get("SITE_PUSH", "") in ("1", "true", "yes")

    deadline = time.time() + args.minutes * 60
    while time.time() < deadline:
        runner.tick(showdate)
        if site_dir:
            from bot.site import update_site
            update_site(runner._entries, runner.estimated_durations(showdate), site_dir, push=site_push)
        time.sleep(POLL_SECS + random.uniform(-POLL_JITTER, POLL_JITTER))

    log.info("window closed — posting show recap + lengths thread")
    runner.post_show_recap(showdate)
    if site_dir:
        from bot.site import update_site
        update_site(runner._entries, runner.estimated_durations(showdate), site_dir, push=site_push, complete=True)


def cmd_once(args):
    runner = build_runner(args.dry_run)
    n = runner.tick(args.date)
    log.info("tick complete; %d new songs posted", n)


def cmd_replay(args):
    """Replay a captured API response file, song by song, through the pipeline."""
    runner = build_runner(dry_run=True)
    with open(args.fixture) as f:
        full = json.load(f)

    if full.get("song_stats"):
        runner.state.upsert_song_stats(full["song_stats"])

    rows = full.get("data", [])
    showdate = rows[0]["showdate"]
    base = 1_000_000_000  # synthetic clock
    t = 0.0
    for i in range(1, len(rows) + 1):
        partial = {"error": False, "data": rows[:i]}
        runner.client.setlist_for_date = lambda d, _p=partial: PhishNetClient.parse_setlist(_p)  # type: ignore
        runner.tick(showdate, now=base + t)
        # simulate 75s polling until the next song starts
        song_minutes = float(rows[i - 1].get("sim_minutes", 8))
        end = t + song_minutes * 60
        while t + 75 < end:
            t += 75
            runner.tick(showdate, now=base + t)
        t = end
    runner.post_show_recap(showdate)
    pub = runner.publishers[0]
    print(f"\n=== replay complete: {len(pub.sent)} posts generated ===")


def cmd_verified_recap(args):
    """Post exact song lengths from phish.in (run the morning after a show)."""
    from bot.composer import lengths_recap_posts
    from bot.phishin import PhishinClient

    tracks = PhishinClient().show_tracks(args.date)
    if not tracks:
        print(f"phish.in has no recording for {args.date} yet — try again later")
        return

    per_set: list = []
    for t in tracks:
        secs = (t["duration_ms"] or 0) // 1000 or None
        set_name = t["set_name"] or "Set"
        if per_set and per_set[-1][0] == set_name:
            per_set[-1][1].append((t["title"], secs))
        else:
            per_set.append((set_name, [(t["title"], secs)]))

    posts = lengths_recap_posts(args.date, per_set, estimated=False)
    state = State(os.environ.get("STATE_DB", "botstate.db"))
    for pub in build_publishers(args.dry_run):
        if state.recap_posted(args.date, "LENGTHS_VERIFIED", pub.name):
            print(f"already posted verified lengths to {pub.name}")
            continue
        reply_to = None
        for text in posts:
            reply_to = pub.post(text, in_reply_to=reply_to)
        state.mark_recap(args.date, "LENGTHS_VERIFIED", pub.name)
        print(f"posted verified lengths thread ({len(posts)} posts) to {pub.name}")


def cmd_test_post(args):
    if args.platform == "x":
        pub = _x_from_env()
        remote_id = pub.post(args.text)
    elif args.platform == "phishpicks":
        # phishpicks ingests structured fields, not text: the positional arg is
        # the SONG NAME and --set carries the set (default "1").
        pub = _phishpicks_from_env()
        remote_id = pub.post(args.text, meta={"song": args.text, "set": args.set})
    else:
        pub = TruthPublisher(os.environ.get("TRUTH_BEARER_TOKEN", ""))
        remote_id = pub.post(args.text)
    print(f"posted to {pub.name}, id={remote_id}")


def main():
    load_dotenv()
    p = argparse.ArgumentParser(prog="phish-setlist-bot")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run"); r.add_argument("--dry-run", action="store_true"); r.set_defaults(fn=cmd_run)
    sw = sub.add_parser("show-window"); sw.add_argument("--minutes", type=int, default=340); sw.add_argument("--dry-run", action="store_true"); sw.set_defaults(fn=cmd_show_window)
    o = sub.add_parser("once"); o.add_argument("--date", default=date.today().isoformat()); o.add_argument("--dry-run", action="store_true"); o.set_defaults(fn=cmd_once)
    rp = sub.add_parser("replay"); rp.add_argument("fixture"); rp.set_defaults(fn=cmd_replay)
    tp = sub.add_parser("test-post"); tp.add_argument("text"); tp.add_argument("--platform", default="truthsocial", choices=["truthsocial", "x", "phishpicks"]); tp.add_argument("--set", default="1", help="set label for --platform phishpicks (the 'text' arg is the song name)"); tp.set_defaults(fn=cmd_test_post)
    vr = sub.add_parser("verified-recap"); vr.add_argument("--date", default=date.today().isoformat()); vr.add_argument("--dry-run", action="store_true"); vr.set_defaults(fn=cmd_verified_recap)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
