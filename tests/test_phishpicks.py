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
    rid = pub.post("ignored composed text", meta={"song": "Tweezer", "set": "2"})
    assert rid == "abc123"
    assert len(sess.calls) == 1
    call = sess.calls[0]
    assert call["url"] == "https://phishpicks.net/api/ingest/song"
    assert call["headers"]["Authorization"] == "Bearer SEKRIT"
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["json"] == {"song": "Tweezer", "set": "2"}


def test_encore_set_passthrough():
    sess = FakeSession()
    pub = PhishPicksPublisher(token="t", session=sess)
    pub.post("x", meta={"song": "Icculus", "set": "e"})
    assert sess.calls[0]["json"] == {"song": "Icculus", "set": "e"}


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
    pub.post("SET TWO: Tweezer [9:47 PM ET]\nGap: 3 shows", meta={"song": "Tweezer", "set": "2"})
    assert sess.calls[0]["json"] == {"song": "Tweezer", "set": "2"}


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
