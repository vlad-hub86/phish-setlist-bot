"""PhishPicks ingest publisher.

Unlike Truth Social and X, this destination is NOT a social feed — it is a
structured data ingest for phishpicks.net. It does not want the composed post
text; it wants the structured song record, one HTTP POST per song:

    POST https://phishpicks.net/api/ingest/song
    Authorization: Bearer <PHISHPICKS_TOKEN>
    Content-Type: application/json
    {"song": "Tweezer", "set": "2", "position": 12,
     "showdate": "2026-07-25", "transition_in": "->", "songid": 627}

``position`` is the song's GLOBAL 1-based order within the whole show (not
within its set) and is the field that disambiguates repeats: the 7/25/26 MSG
show played Tweezer six times in Set 2, which without a position is six
byte-identical records.

``transition_in`` is the mark connecting the PREVIOUS song to this one —
",", ">", "->", or null. It is deliberately the INCOMING mark, not the
outgoing one: phish.net stores a transition as "how this song connects to the
NEXT one", so a song's outgoing mark does not exist yet at the moment it is
first posted live (its successor has not been entered). The incoming mark, by
contrast, is already known. Null means "not recorded yet" — never guessed.
It is also null at a set boundary, since nothing segues across a setbreak.

Because it consumes structured fields rather than rendered text, the runner
hands each publisher an optional ``meta`` dict alongside the text (see
publishers/base.py and runner._post_song). Text publishers ignore ``meta``;
this one ignores ``text`` and reads ``meta["song"]`` / ``meta["set"]``.

Scope: this platform ONLY accepts the ``song`` post kind (``kinds={"song"}``).
There is no meaningful ``{song, set}`` for a show-start, recap, milestone, or
lengths thread, so those kinds are never routed here. If ``post`` is ever
called without usable structured data (e.g. a code path that forgets ``meta``),
it logs and no-ops rather than shipping a garbage record — the endpoint is a
database, not a timeline.

Set values are phish.net's raw labels: "1", "2", "3", and "e" for the encore.
If phishpicks.net expects something else for the encore, remap it here (see
``_set_value``) — that is the one field whose encoding the endpoint owner has
to confirm.

Isolated like the other publishers: any exception here is caught by the runner
and retried on the next tick, so a phishpicks.net problem never blocks Truth
Social or X posting. Idempotency is handled by the runner's per-platform post
log (keyed by show/set/position + "phishpicks"), so a re-run never double-sends
a song that already landed — provided the state DB persists between runs.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests

from .base import Publisher

log = logging.getLogger(__name__)

DEFAULT_URL = "https://phishpicks.net/api/ingest/song"
UA = "phish-setlist-bot/0.1 (setlist ingest; contact: vlad@miajunefacialbar.com)"


class _Refused(RuntimeError):
    """A 409 from the receiver: an intentional refusal, not a transient fault.

    Raised (rather than returned) so the song stays unmarked and is offered
    again on the next poll — which is what heals the "show not featured yet"
    case. Distinct from RuntimeError so the reason is greppable in the log.
    """


class PhishPicksPublisher(Publisher):
    name = "phishpicks"
    # Structured song ingest only — see module docstring.
    kinds = {"song"}

    def __init__(
        self,
        token: str,
        url: str = DEFAULT_URL,
        max_retries: int = 3,
        timeout: int = 20,
        kinds: Optional[set] = None,
        session: Optional[requests.Session] = None,
    ):
        if not token:
            raise ValueError("PHISHPICKS_TOKEN is required")
        self.url = url or DEFAULT_URL
        self.max_retries = max_retries
        self.timeout = timeout
        if kinds is not None:
            self.kinds = kinds
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": UA,
        }
        self.http = session or requests.Session()

    @staticmethod
    def _set_value(raw) -> str:
        """phish.net set label -> the value phishpicks.net expects.

        Currently a pass-through of the raw label ("1"/"2"/"3"/"e"). The one
        place to remap if the endpoint owner wants, e.g., "encore" instead of
        "e".
        """
        return str(raw)

    def post(
        self,
        text: str,
        in_reply_to: Optional[str] = None,
        meta: Optional[dict] = None,
    ) -> Optional[str]:
        song = (meta or {}).get("song")
        raw_set = (meta or {}).get("set")
        if not song or raw_set in (None, ""):
            # No structured data to ingest — never ship the rendered text as if
            # it were a song record. This makes the publisher safe even if a
            # non-song code path (or a future caller) routes to it by mistake.
            log.warning(
                "phishpicks: missing structured song/set in meta; skipping "
                "(song=%r set=%r)", song, raw_set,
            )
            return None

        # Full record. Optional fields are always present, using null when
        # unknown, so the receiving schema is predictable rather than sparse.
        payload = {
            "song": song,
            "set": self._set_value(raw_set),
            "position": (meta or {}).get("position"),
            "showdate": (meta or {}).get("showdate"),
            "transition_in": (meta or {}).get("transition_in"),
            "songid": (meta or {}).get("songid"),
        }
        delay = 5
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.http.post(
                    self.url, headers=self.headers, json=payload, timeout=self.timeout
                )
            except requests.RequestException as e:
                # Network-level failure (timeout, connection reset): treat as
                # transient and let the runner retry next tick.
                if attempt < self.max_retries:
                    log.warning(
                        "phishpicks network error %s (attempt %d), retrying in %ds",
                        type(e).__name__, attempt, delay,
                    )
                    time.sleep(delay)
                    delay *= 3
                    continue
                raise RuntimeError(f"phishpicks ingest failed (network): {e}") from e

            if 200 <= resp.status_code < 300:
                log.info(
                    "ingested %s (set %s, pos %s) to phishpicks%s",
                    song, payload["set"], payload["position"], self._describe(resp),
                )
                return self._extract_id(resp)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                log.warning(
                    "phishpicks %s (attempt %d), retrying in %ds",
                    resp.status_code, attempt, delay,
                )
                time.sleep(delay)
                delay *= 3
                continue
            if resp.status_code == 400:
                # Malformed record (e.g. title over the receiver's length cap).
                # This can never succeed on a retry, so mark it handled rather
                # than re-sending the same bad payload every ~75s tick.
                log.error(
                    "phishpicks rejected %s (pos %s) as malformed — not retrying: %s",
                    song, payload["position"], resp.text[:200],
                )
                return None
            if resp.status_code == 409:
                # Deliberate refusal: either the show isn't featured yet, or the
                # setlist has been finalized (frozen ~30-60 min after the last
                # song). Logged plainly rather than as an error — but we do NOT
                # mark it handled, so the pre-featured case still heals on a
                # later tick. Post-finalize retries stop when the window closes.
                log.warning(
                    "phishpicks refused %s (pos %s): %s — will re-offer next tick",
                    song, payload["position"], resp.text[:200],
                )
                raise _Refused(f"phishpicks refused {song}: {resp.text[:200]}")
            # Other 4xx (401/403) — propagate loudly. A persistent 401 means the
            # bearer is wrong, and the repetition in the log is the signal.
            raise RuntimeError(
                f"phishpicks ingest failed: {resp.status_code} {resp.text[:300]}"
            )
        return None

    @staticmethod
    def _describe(resp) -> str:
        """Short tail for the success log, from the receiver's ack body.

        The receiver answers with {ok, song, set, position} plus optional
        duplicate / replaced / transitionWrittenTo flags. `replaced` in
        particular is worth surfacing: it means our record overwrote a
        different song at that position, i.e. real position drift.
        """
        try:
            body = resp.json()
        except ValueError:
            return ""
        if not isinstance(body, dict):
            return ""
        bits = []
        if body.get("duplicate"):
            bits.append("duplicate")
        if body.get("replaced"):
            bits.append(f"replaced {body['replaced']!r}")
        if body.get("transitionWrittenTo") is not None:
            bits.append(f"transition->pos {body['transitionWrittenTo']}")
        return (" [" + ", ".join(bits) + "]") if bits else ""

    @staticmethod
    def _extract_id(resp) -> Optional[str]:
        """Best-effort remote id for the post log; None is acceptable."""
        try:
            body = resp.json()
        except ValueError:
            return None
        if isinstance(body, dict):
            for k in ("id", "ingest_id", "song_id"):
                if body.get(k) is not None:
                    return str(body[k])
        return None
