"""Tests for the phishpicks.net structured-ingest publisher and its wiring
into the runner's per-song fan-out. No network — a FakeSession records calls.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.publishers.phishpicks as pp_mod
from bot.phishnet import PhishNetClient
from bot.publishers import DryRunPublisher, PhishPicksPublisher
from bot.runner import Runner
from bot.state import State

# Sleeps would make the retry test slow; the retry path is what we assert, not
# the wall-clock delay.
pp_mod.time.sleep = lambda *a, **k: None

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "sample_show.json").read_text())


class FakeResp:
    def __init__(self, status, body=None, text=""):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    """Records POSTs; replays a queue of responses (last one repeats)."""

    def __init__(self, responses=None):
        self.responses = list(responses or [FakeResp(200, {"id": "srv-1"})])
        self.calls = []  # list of dicts: {url, headers, json}

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers or {}, "json": json})
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


# --------------------------------------------------------------- publisher unit

def test_requires_token():
    try:
        PhishPicksPublisher(token="")
        assert False, "expected ValueError for empty token"
    except ValueError:
        pass


def test_accepts_only_song_kind():
    pub = PhishPicksPublisher(token="t", session=FakeSession())
    assert pub.accepts("song") is True
    for kind in ("show_start", "set_recap_1", "set_recap_2", "set_recap_e",
                 "milestone", "show_recap", "lengths"):
        assert pub.accepts(kind) is False, kind


def test_sends_correct_payload_and_auth():
    sess = FakeSession([FakeResp(200, {"id": "abc123"})])
    pub = PhishPicksPublisher(token="SEKRIT", session=sess)
    rid = pub.post("ignored composed text", meta={
        "song": "Tweezer", "set": "2", "position": 12,
        "showdate": "2026-07-25", "transition_in": "->", "songid": 627,
    })
    assert rid == "abc123"
    assert len(sess.calls) == 1
    call = sess.calls[0]
    assert call["url"] == "https://phishpicks.net/api/ingest/song"
    assert call["headers"]["Authorization"] == "Bearer SEKRIT"
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["json"] == {
        "song": "Tweezer", "set": "2", "position": 12,
        "showdate": "2026-07-25", "transition_in": "->", "songid": 627,
    }


def test_optional_fields_present_as_null_when_unknown():
    """Schema is predictable, not sparse: keys always present, null if unknown."""
    sess = FakeSession()
    pub = PhishPicksPublisher(token="t", session=sess)
    pub.post("x", meta={"song": "Free", "set": "1"})
    assert sess.calls[0]["json"] == {
        "song": "Free", "set": "1", "position": None,
        "showdate": None, "transition_in": None, "songid": None,
    }


def test_encore_set_passthrough():
    sess = FakeSession()
    pub = PhishPicksPublisher(token="t", session=sess)
    pub.post("x", meta={"song": "Icculus", "set": "e", "position": 7})
    assert sess.calls[0]["json"]["set"] == "e"
    assert sess.calls[0]["json"]["position"] == 7


def test_custom_url_override():
    sess = FakeSession()
    pub = PhishPicksPublisher(token="t", url="https://staging.example/ingest", session=sess)
    pub.post("x", meta={"song": "Sand", "set": "2"})
    assert sess.calls[0]["url"] == "https://staging.example/ingest"


def test_missing_meta_skips_without_calling():
    sess = FakeSession()
    pub = PhishPicksPublisher(token="t", session=sess)
    assert pub.post("some text", meta=None) is None
    assert pub.post("some text", meta={"set": "2"}) is None          # no song
    assert pub.post("some text", meta={"song": "Sand"}) is None      # no set
    assert pub.post("some text", meta={"song": "Sand", "set": ""}) is None
    assert sess.calls == [], "must not hit the network without a song+set"


def test_text_is_ignored_only_meta_used():
    sess = FakeSession()
    pub = PhishPicksPublisher(token="t", session=sess)
    pub.post("SET TWO: Tweezer [9:47 PM ET]\nGap: 3 shows",
             meta={"song": "Tweezer", "set": "2", "position": 4})
    body = sess.calls[0]["json"]
    assert body["song"] == "Tweezer" and body["set"] == "2" and body["position"] == 4
    assert "9:47" not in json.dumps(body)


def test_retry_on_5xx_then_success():
    sess = FakeSession([FakeResp(503, text="busy"), FakeResp(200, {"id": "ok"})])
    pub = PhishPicksPublisher(token="t", session=sess)
    rid = pub.post("x", meta={"song": "Ghost", "set": "2"})
    assert rid == "ok"
    assert len(sess.calls) == 2


def test_4xx_raises():
    sess = FakeSession([FakeResp(403, text="bad token")])
    pub = PhishPicksPublisher(token="t", session=sess)
    try:
        pub.post("x", meta={"song": "Ghost", "set": "2"})
        assert False, "expected RuntimeError on 403"
    except RuntimeError as e:
        assert "403" in str(e)


def test_401_raises_loudly():
    """A wrong bearer must stay noisy — the repetition is the signal."""
    sess = FakeSession([FakeResp(401, {"error": "unauthorized"}, text='{"error":"unauthorized"}')])
    pub = PhishPicksPublisher(token="t", session=sess)
    try:
        pub.post("x", meta={"song": "Ghost", "set": "2", "position": 1})
        assert False, "expected RuntimeError on 401"
    except RuntimeError:
        pass


def test_409_raises_so_the_song_is_re_offered():
    """409 = show not featured yet, or setlist finalized. Must NOT be marked
    handled: the not-yet-featured case has to heal on a later tick."""
    sess = FakeSession([FakeResp(409, {"error": "no open featured show"},
                                 text='{"error":"no open featured show"}')])
    pub = PhishPicksPublisher(token="t", session=sess)
    try:
        pub.post("x", meta={"song": "Ghost", "set": "2", "position": 3})
        assert False, "expected a refusal to propagate"
    except RuntimeError as e:
        assert "no open featured show" in str(e)


def test_409_song_retried_on_next_tick_and_can_succeed():
    """End-to-end heal: refused while unfeatured, accepted once featured."""
    rows = [_row(1, "1", "Free", transition=0, songid=200)]
    runner, truth, pp, sess = _runner_with_phishpicks(rows, set_recaps=False)
    sess.responses = [FakeResp(409, {"error": "no open featured show"},
                               text='{"error":"no open featured show"}')]
    runner.tick("2026-07-25")
    assert not runner.state.already_posted(("2026-07-25", "1", 1), "phishpicks")

    sess.responses = [FakeResp(200, {"ok": True, "song": "Free", "set": "1", "position": 1})]
    runner.tick("2026-07-25")
    assert runner.state.already_posted(("2026-07-25", "1", 1), "phishpicks")
    assert len(sess.calls) == 2


def test_400_is_permanent_and_not_retried():
    """A malformed record can never succeed — mark handled instead of
    re-sending the identical bad payload every tick."""
    rows = [_row(1, "1", "Free", transition=0, songid=200)]
    runner, truth, pp, sess = _runner_with_phishpicks(rows, set_recaps=False)
    sess.responses = [FakeResp(400, {"error": "song required (100 chars max)"},
                               text='{"error":"song required (100 chars max)"}')]
    runner.tick("2026-07-25")
    assert runner.state.already_posted(("2026-07-25", "1", 1), "phishpicks")
    runner.tick("2026-07-25")
    assert len(sess.calls) == 1, "malformed payload must not be re-sent"


def test_ack_flags_are_logged_not_fatal():
    """duplicate / replaced / transitionWrittenTo acks are informational."""
    sess = FakeSession([FakeResp(200, {"ok": True, "replaced": "Cavern",
                                       "transitionWrittenTo": 11})])
    pub = PhishPicksPublisher(token="t", session=sess)
    assert pub.post("x", meta={"song": "Tweezer", "set": "2", "position": 12}) is None
    assert "replaced 'Cavern'" in PhishPicksPublisher._describe(FakeResp(200, {"replaced": "Cavern"}))


def test_id_none_when_no_json():
    sess = FakeSession([FakeResp(202, body=None)])  # accepted, empty/non-json body
    pub = PhishPicksPublisher(token="t", session=sess)
    assert pub.post("x", meta={"song": "Free", "set": "1"}) is None
    assert len(sess.calls) == 1  # 2xx = success, not retried


# ------------------------------------------------------- runner integration

def _runner_with_phishpicks(rows, set_recaps=True):
    client = PhishNetClient(api_key="")
    state = State(":memory:")
    state.upsert_song_stats(FIXTURE["song_stats"])
    truth = DryRunPublisher("truthsocial-dry")
    sess = FakeSession()
    pp = PhishPicksPublisher(token="t", session=sess)
    runner = Runner(client, state, [truth, pp], post_set_recaps=set_recaps)
    client.setlist_for_date = lambda d: PhishNetClient.parse_setlist({"data": rows})
    return runner, truth, pp, sess


def _row(pos, set_label, song, transition=1, songid=1):
    """Minimal phish.net-shaped row. transition: 1=',' 2='>' 3='->', 0=absent."""
    return {
        "showdate": "2026-07-25", "set": set_label, "position": pos, "song": song,
        "songid": songid, "gap": 3, "transition": transition, "footnote": "",
        "venue": "Madison Square Garden", "city": "New York", "state": "NY",
        "artist_slug": "phish",
    }


def test_runner_sends_position_showdate_songid_and_transition_in():
    rows = [
        _row(1, "1", "Free", transition=1, songid=200),          # ,  -> next
        _row(2, "1", "Sample in a Jar", transition=2, songid=449),  # >  -> next
        _row(3, "1", "Ghost", transition=0, songid=219),         # last of set: absent
        _row(4, "2", "Tweezer", transition=3, songid=627),       # -> -> next
        _row(5, "2", "Harry Hood", transition=0, songid=233),
    ]
    runner, truth, pp, sess = _runner_with_phishpicks(rows, set_recaps=False)
    runner.tick("2026-07-25")
    bodies = [c["json"] for c in sess.calls]

    assert [b["position"] for b in bodies] == [1, 2, 3, 4, 5]
    assert all(b["showdate"] == "2026-07-25" for b in bodies)
    assert [b["songid"] for b in bodies] == [200, 449, 219, 627, 233]

    # First song of a set has no incoming mark; others carry the PREVIOUS
    # song's outgoing mark. Position 4 opens Set 2 -> null across the break.
    assert [b["transition_in"] for b in bodies] == [None, ",", ">", None, "->"]


def test_transition_in_null_when_not_yet_recorded():
    """A prev song whose transition phish.net hasn't filled in yet -> null,
    never a guessed comma."""
    rows = [_row(1, "1", "Free", transition=0), _row(2, "1", "Simple", transition=0)]
    runner, truth, pp, sess = _runner_with_phishpicks(rows, set_recaps=False)
    runner.tick("2026-07-25")
    assert [c["json"]["transition_in"] for c in sess.calls] == [None, None]


def test_repeated_songs_are_distinguishable_by_position():
    """7/25/26 MSG Tweezerfest: Tweezer six times in Set 2. Without position
    these are byte-identical records and a picks board can't score them."""
    rows = [
        _row(1, "2", "Highway to Hell", transition=1, songid=1),
        _row(2, "2", "Down with Disease", transition=3, songid=2),
        _row(3, "2", "Tweezer", transition=3, songid=627),
        _row(4, "2", "Down with Disease", transition=2, songid=2),
        _row(5, "2", "Tweezer", transition=2, songid=627),
        _row(6, "2", "Sample in a Jar", transition=3, songid=449),
        _row(7, "2", "Tweezer", transition=0, songid=627),
    ]
    runner, truth, pp, sess = _runner_with_phishpicks(rows, set_recaps=False)
    runner.tick("2026-07-25")
    bodies = [c["json"] for c in sess.calls]

    tweezers = [b for b in bodies if b["song"] == "Tweezer"]
    assert len(tweezers) == 3
    assert [t["position"] for t in tweezers] == [3, 5, 7]           # all distinct
    assert len({json.dumps(t, sort_keys=True) for t in tweezers}) == 3  # no dupes
    # every record in the show is unique as a whole
    assert len({json.dumps(b, sort_keys=True) for b in bodies}) == len(bodies)


def test_runner_ingests_one_record_per_song_with_set_labels():
    rows = FIXTURE["data"]
    runner, truth, pp, sess = _runner_with_phishpicks(rows)
    runner.tick("2026-07-21")
    runner.post_show_recap("2026-07-21")  # recaps/lengths must NOT reach phishpicks

    songs = [(c["json"]["song"], c["json"]["set"]) for c in sess.calls]
    assert songs == [
        ("Free", "1"),
        ("Sample in a Jar", "1"),
        ("Slave to the Traffic Light", "1"),
        ("Tweezer", "2"),
        ("Ghost", "2"),
        ("Harry Hood", "2"),
        ("Icculus", "e"),
    ]
    # Truth (the sandbox) got far more than 7 posts (songs + recaps + lengths);
    # phishpicks got exactly the 7 song records and nothing else.
    assert len(sess.calls) == 7
    assert len(truth.sent) > 7


def test_runner_idempotent_per_platform_on_rerun():
    rows = FIXTURE["data"]
    runner, truth, pp, sess = _runner_with_phishpicks(rows)
    runner.tick("2026-07-21")
    assert len(sess.calls) == 7
    # A duplicate run (e.g. a late cron event) must not re-ingest.
    runner.tick("2026-07-21")
    assert len(sess.calls) == 7, "re-run double-sent to phishpicks"


def test_phishpicks_failure_does_not_block_truth():
    rows = FIXTURE["data"][:1]
    runner, truth, pp, sess = _runner_with_phishpicks(rows, set_recaps=False)
    sess.responses = [FakeResp(500, text="down"), FakeResp(500, text="down"), FakeResp(500, text="down")]
    runner.tick("2026-07-21")
    # Truth still got its song post despite phishpicks 500ing every retry.
    assert any("Free" in p for p in truth.sent)
    # phishpicks was attempted (and left unmarked so it retries next tick)
    assert len(sess.calls) >= 1
    assert not runner.state.already_posted(("2026-07-21", "1", 1), "phishpicks")
    assert runner.state.already_posted(("2026-07-21", "1", 1), "truthsocial-dry")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    ok = 0
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
        ok += 1
    print(f"\n{ok} tests passed")
