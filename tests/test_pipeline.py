"""End-to-end pipeline tests using the sample fixture (no network)."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.composer import song_post, set_recap_post
from bot.phishnet import PhishNetClient
from bot.publishers import DryRunPublisher
from bot.runner import Runner
from bot.state import State

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "sample_show.json").read_text())


def make_runner(rows, set_recaps=False):
    client = PhishNetClient(api_key="")  # no network in tests
    state = State(":memory:")
    state.upsert_song_stats(FIXTURE["song_stats"])
    pub = DryRunPublisher()
    runner = Runner(client, state, [pub], post_set_recaps=set_recaps)
    client.setlist_for_date = lambda d: PhishNetClient.parse_setlist({"data": rows})
    return runner, pub


def test_posts_each_song_once():
    rows = FIXTURE["data"]
    runner, pub = make_runner(rows)
    n1 = runner.tick("2026-07-21")
    assert n1 == len(rows)
    # second tick with identical data: nothing new
    n2 = runner.tick("2026-07-21")
    assert n2 == 0


def test_incremental_reveal_posts_in_order():
    rows = FIXTURE["data"]
    runner, pub = make_runner(rows[:1])
    runner.tick("2026-07-21")
    assert pub.sent[0].startswith("SET ONE: Free [")

    runner.client.setlist_for_date = lambda d: PhishNetClient.parse_setlist({"data": rows[:3]})
    runner.tick("2026-07-21")
    assert pub.sent[1].startswith("Sample in a Jar [")
    assert pub.sent[2].startswith("Slave to the Traffic Light [")


def test_ftr_set_and_encore_labels():
    rows = FIXTURE["data"]
    runner, pub = make_runner(rows)
    runner.tick("2026-07-21")
    assert any(p.startswith("SET ONE: Free [") for p in pub.sent)
    assert any(p.startswith("SET TWO: Tweezer [") for p in pub.sent)
    assert any(p.startswith("ENCORE: Icculus [") for p in pub.sent)
    assert any(p.startswith("Ghost [") for p in pub.sent)  # bare title + time
    # stats block appears under the headline
    icculus = [p for p in pub.sent if p.startswith("ENCORE: Icculus [")][0]
    assert "Gap: 214 shows" in icculus
    assert "Debut: 1984" in icculus
    assert not [p for p in pub.sent if "\U0001f3b5" in p]


def test_bustout_in_show_recap():
    rows = FIXTURE["data"]
    runner, pub = make_runner(rows)
    runner.tick("2026-07-21")
    runner.post_show_recap("2026-07-21")
    recap = [p for p in pub.sent if "Notable:" in p][0]
    assert "Icculus (gap 214)" in recap


def test_set_recap_after_new_set_starts():
    rows = FIXTURE["data"]
    runner, pub = make_runner(rows, set_recaps=True)
    runner.tick("2026-07-21")
    recaps = [p for p in pub.sent if p.startswith("SET ONE RECAP")]
    assert len(recaps) == 1
    assert "3 songs" in recaps[0]


def test_no_urls_anywhere():
    rows = FIXTURE["data"]
    runner, pub = make_runner(rows)
    runner.tick("2026-07-21", now=time.time())
    runner.post_show_recap("2026-07-21")
    for p in pub.sent:
        assert "http" not in p, f"URL found in post (would 13x the X cost): {p!r}"


def test_posts_fit_x_limit():
    rows = FIXTURE["data"]
    runner, pub = make_runner(rows)
    runner.tick("2026-07-21")
    for p in pub.sent:
        assert len(p) <= 280


def test_milestones_fire_once_each():
    rows = FIXTURE["data"][:4]  # ends with Tweezer (set 2) as current song
    runner, pub = make_runner(rows)
    t0 = 1_000_000_000
    runner.tick("2026-07-21", now=t0)

    runner.tick("2026-07-21", now=t0 + 21 * 60)
    m20 = [p for p in pub.sent if "20+ MINUTES" in p]
    assert len(m20) == 1 and "Tweezer" in m20[0]

    runner.tick("2026-07-21", now=t0 + 22 * 60)  # no duplicate
    assert len([p for p in pub.sent if "20+ MINUTES" in p]) == 1

    runner.tick("2026-07-21", now=t0 + 31 * 60)
    assert len([p for p in pub.sent if "30+ MINUTES" in p]) == 1

    runner.tick("2026-07-21", now=t0 + 41 * 60)
    big = [p for p in pub.sent if "40+ MINUTE JAM" in p]
    assert len(big) == 1 and "rarefied air" in big[0]


def test_milestone_skips_straight_to_highest():
    rows = FIXTURE["data"][:4]
    runner, pub = make_runner(rows)
    t0 = 1_000_000_000
    runner.tick("2026-07-21", now=t0)
    # bot was down, comes back at 43 min: only the 40 post, not 20/30 spam
    runner.tick("2026-07-21", now=t0 + 43 * 60)
    assert len([p for p in pub.sent if "40+ MINUTE JAM" in p]) == 1
    assert not [p for p in pub.sent if "20+ MINUTES" in p or "30+ MINUTES" in p]


def test_no_milestone_for_encore():
    rows = FIXTURE["data"]  # ends with Icculus in the encore
    runner, pub = make_runner(rows)
    t0 = 1_000_000_000
    runner.tick("2026-07-21", now=t0)
    runner.tick("2026-07-21", now=t0 + 45 * 60)
    assert not [p for p in pub.sent if "MINUTE" in p and "\U0001f552" in p or "40+ MINUTE JAM" in p]


def test_lengths_recap_thread():
    rows = FIXTURE["data"]
    runner, pub = make_runner(rows[:1])
    t0 = 1_000_000_000
    t = t0
    # reveal songs one at a time with realistic spacing from sim_minutes
    for i in range(1, len(rows) + 1):
        runner.client.setlist_for_date = (
            lambda d, _r=rows[:i]: __import__("bot.phishnet", fromlist=["PhishNetClient"]).PhishNetClient.parse_setlist({"data": _r})
        )
        runner.tick("2026-07-21", now=t)
        t += rows[i - 1]["sim_minutes"] * 60
    runner.post_show_recap("2026-07-21")

    lengths_posts = [p for p in pub.sent if "Song lengths" in p or p.startswith("Set ") and "—" in p or p.startswith("Encore:")]
    header = [p for p in pub.sent if "Song lengths" in p]
    assert len(header) == 1
    set2 = [p for p in pub.sent if p.startswith("Set 2:")][0]
    assert "Tweezer — ~23m" in set2
    assert "Ghost — ~12m" in set2
    # last song of a set has unknown length
    assert "Harry Hood — –" in set2
    set1 = [p for p in pub.sent if p.startswith("Set 1:")][0]
    assert "Slave to the Traffic Light — –" in set1


def test_no_milestones_after_show_recap():
    """Storm-cutoff scenario: once the show recap has posted, the closer's
    still-running clock must not fire jam milestones."""
    rows = FIXTURE["data"][:4]  # Tweezer (set 2) is current
    runner, pub = make_runner(rows)
    t0 = 1_000_000_000
    runner.tick("2026-07-21", now=t0)
    runner.post_show_recap("2026-07-21")  # show over (e.g. announced early end)
    runner.tick("2026-07-21", now=t0 + 25 * 60)
    runner.tick("2026-07-21", now=t0 + 45 * 60)
    assert not [p for p in pub.sent if "20+ MINUTES" in p or "40+ MINUTE JAM" in p]


def test_stale_first_crossing_never_fires():
    """Feed stalled for an hour (setbreak/outage): a FIRST-time threshold
    crossing that deep is not a jam — no milestone."""
    rows = FIXTURE["data"][:4]
    runner, pub = make_runner(rows)
    t0 = 1_000_000_000
    runner.tick("2026-07-21", now=t0)
    runner.tick("2026-07-21", now=t0 + 55 * 60)  # first check at 55 min
    assert not [p for p in pub.sent if "MINUTE" in p and "+" in p]


def test_live_jam_unaffected_by_stale_cap():
    """A jam tracked live (20 fired at 21m) keeps firing 30/40 past the cap."""
    rows = FIXTURE["data"][:4]
    runner, pub = make_runner(rows)
    t0 = 1_000_000_000
    runner.tick("2026-07-21", now=t0)
    runner.tick("2026-07-21", now=t0 + 21 * 60)   # 20 fires — tracked live
    runner.tick("2026-07-21", now=t0 + 52 * 60)   # past cap, but jam is real
    assert len([p for p in pub.sent if "40+ MINUTE JAM" in p]) == 1


def test_catchup_burst_lengths_unknown_not_zero():
    """Bot starting mid-show sees existing songs in one burst — their
    'durations' are seconds apart and must report as unknown, not ~0m."""
    rows = FIXTURE["data"]
    runner, pub = make_runner(rows)
    t0 = 1_000_000_000
    runner.tick("2026-07-21", now=t0)  # all songs at once = catch-up burst
    runner.post_show_recap("2026-07-21")
    for p in pub.sent:
        assert "~0m" not in p, f"bogus zero-length in recap: {p!r}"
        assert "[0 min]" not in p, f"bogus zero-length in recap: {p!r}"
    set1 = [p for p in pub.sent if p.startswith("Set 1:")][0]
    assert "Free — –" in set1  # unknown, not fabricated


def test_live_observed_lengths_still_estimated():
    """Songs observed live (realistic gaps) still get real estimates."""
    rows = FIXTURE["data"]
    runner, pub = make_runner(rows[:1])
    t0 = 1_000_000_000
    t = t0
    for i in range(1, len(rows) + 1):
        runner.client.setlist_for_date = (
            lambda d, _r=rows[:i]: PhishNetClient.parse_setlist({"data": _r})
        )
        runner.tick("2026-07-21", now=t)
        t += rows[i - 1]["sim_minutes"] * 60
    runner.post_show_recap("2026-07-21")
    set2 = [p for p in pub.sent if p.startswith("Set 2:")][0]
    assert "Tweezer — ~23m" in set2  # real observation unaffected by the gate


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
