"""PhishPicks ingest publisher.

Unlike Truth Social and X, this destination is NOT a social feed — it is a
structured data ingest for phishpicks.net. It does not want the composed post
text; it wants the raw song name and set number, one HTTP POST per song:

    POST https://phishpicks.net/api/ingest/song
    Authorization: Bearer <PHISHPICKS_TOKEN>
    Content-Type: application/json
    {"song": "Tweezer", "set": "2"}

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

        payload = {"song": song, "set": self._set_value(raw_set)}
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
                remote_id = self._extract_id(resp)
                log.info("ingested %s (set %s) to phishpicks: id=%s", song, payload["set"], remote_id)
                return remote_id
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                log.warning(
                    "phishpicks %s (attempt %d), retrying in %ds",
                    resp.status_code, attempt, delay,
                )
                time.sleep(delay)
                delay *= 3
                continue
            # 4xx (bad token, bad payload) — propagate so the runner logs it and
            # retries next tick; a persistent 401/403 means the bearer is wrong.
            raise RuntimeError(
                f"phishpicks ingest failed: {resp.status_code} {resp.text[:300]}"
            )
        return None

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
