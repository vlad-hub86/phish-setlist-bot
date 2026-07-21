# phish-setlist-bot

Posts every Phish song as it's played live in the classic @PhishSet / Phish From The Road style — `SET TWO: Oblivion` for set openers, bare song titles after — plus jam-length milestone posts (20/30/40 min), an end-of-show recap with notable stats (bustouts, debuts), and a song-lengths thread. Phase 1 targets **Truth Social**; the X publisher is stubbed for phase 2.

Set recaps ("Set 1 (7 songs): …") exist but are **off by default** (FTR style doesn't do them); enable with `POST_SET_RECAPS=1`. See `phish-setlist-bot-design.md` (project doc) for the full design.

## How it works

```
Phish.net API v5 ──▶ Poller (show windows only) ──▶ Diff engine (SQLite) ──▶ Composer ──▶ Publishers
```

- Polls `setlists/showdate/{today}` every ~75s (jittered) during show windows (6:30pm–1am).
- New songs are detected by **(showdate, set, position)** — repeats and editor corrections are safe; only additions post.
- Gap comes from the setlist record itself; play count + debut from a locally cached song table (refreshed weekly).
- Set recap posts when the next set starts (or after 35 min idle); show recap after the window closes, followed by a **song-lengths thread** (one post per set).
- **Jam milestones:** when the current song crosses 20 / 30 / 40 minutes (measured from when it appeared on the live feed), the bot posts — 40 gets the big-deal treatment. Only the highest newly-crossed threshold posts (no 20/30/40 spam after downtime). Each fires at most once per song per platform.
- **Milestone caveat:** the feed only records song *starts*, so a set closer can look "still going" during setbreak — a milestone may rarely fire falsely then. Encores are excluded entirely (a false positive there is guaranteed). Lengths in the live recap are estimates (~); run `python main.py verified-recap --date YYYY-MM-DD` the next morning to post exact times from phish.in's recording.
- Idempotency: every post is recorded per-platform in SQLite before/after send — restarts never double-post.
- Posts contain **no URLs** (X pay-per-use charges ~13× for posts with links; attribution belongs in the bio).

## Setup

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env   # then fill in keys (see comments in the file)
```

1. **Phish.net API key** — request at api.phish.net; also email the Phish.net team describing the bot (their ToS asks apps to be polite; they disable greedy ones). Put "Data: Phish.net" in your account bios.
2. **Truth Social bearer token** — instructions in `.env.example`. Use a dedicated bot account. Note this uses Truth Social's internal (unofficial) API — small suspension risk, endpoints may change.

## Test before going live

```bash
python tests/test_pipeline.py                      # unit/integration tests, no network
python main.py replay tests/fixtures/sample_show.json   # replay a show, prints every post
python main.py test-post "beep boop test"          # one REAL post to Truth Social
python main.py once --date 2026-07-20 --dry-run    # real API fetch, logged posts only
python main.py run --dry-run                       # full shadow-run during a live show
```

Recommended first live show: run `--dry-run` and compare output against phish.net in real time; flip to real posting the next show.

## Deploy (any $5 VPS)

```bash
sudo cp -r . /opt/phish-setlist-bot
sudo cp deploy/phish-setlist-bot.service /etc/systemd/system/
sudo systemctl enable --now phish-setlist-bot
journalctl -u phish-setlist-bot -f
```

## Repo map

| Path | What |
|---|---|
| `bot/phishnet.py` | Phish.net v5 client + setlist parsing |
| `bot/state.py` | SQLite: posted-song log, recap log, song-stats cache |
| `bot/differ` logic | lives in `bot/runner.py` (diff by set/position) |
| `bot/composer.py` | post templates (song / set recap / show recap) |
| `bot/phishin.py` | phish.in v2 client (verified song durations) |
| `bot/publishers/truth.py` | Truth Social publisher (unofficial API, bearer token) |
| `bot/publishers/x.py` | phase-2 stub |
| `main.py` | CLI: `run`, `once`, `replay`, `test-post` |
| `tests/` | fixture show + 6 pipeline tests |

## Phase 2 (X)

Create an X developer app with pay-per-use billing, implement `XPublisher` with tweepy (`POST /2/tweets`, OAuth 1.0a), set `PLATFORMS=truthsocial,x`. Expected cost ~$5–6 in a busy tour month at $0.015/post.
