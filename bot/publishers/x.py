"""X (Twitter) publisher.

Posts via the X API v2 endpoint POST /2/tweets using tweepy's Client with
OAuth 1.0a user context. X moved new developers to pay-per-use billing in
2026: ~$0.015 per link-free post, but $0.20 for any post containing a link —
so this bot NEVER includes URLs (bot/composer.py enforces that); attribution
("Data: Phish.net") lives in the account bio instead.

Two X-only behaviours live here rather than in the composer, so that Truth
Social keeps receiving the platform-neutral text:

  * a lizard prefix on every post (skipped when the text already opens with
    one, so a post that includes it in its own copy is not doubled), and
  * a WEIGHTED length clamp. X counts emoji and CJK as two characters, while
    composer._clamp counts Python len(). A post at exactly composer's 280
    limit would be rejected by X once the prefix is added, and any emoji-heavy
    post could already exceed X's real limit. Clamping here is the backstop.

Credentials — create at https://developer.x.com :
  1. Create a Project + App for the bot account.
  2. Set the App's user-authentication settings to allow READ AND WRITE
     *before* generating the access token/secret. If the token is generated
     while the app is read-only, every post returns 403 Forbidden and the
     token must be regenerated after switching to Read+Write.
  3. Copy the four values into the environment:
       X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET

Isolated like the Truth publisher: any exception here is caught by the runner
and retried on the next tick, so an X problem never blocks Truth Social posting.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from .base import Publisher

log = logging.getLogger(__name__)

X_MAX_WEIGHTED = 280
ELLIPSIS = "…"


def weighted_len(text: str) -> int:
    """X's character count: code points above U+1100 (emoji, CJK) count as 2."""
    return sum(2 if ord(c) > 0x1100 else 1 for c in text)


def clamp_weighted(text: str, limit: int = X_MAX_WEIGHTED) -> str:
    """Truncate to X's weighted limit, reserving room for the ellipsis."""
    if weighted_len(text) <= limit:
        return text
    budget = limit - weighted_len(ELLIPSIS)
    out, total = [], 0
    for ch in text:
        cost = 2 if ord(ch) > 0x1100 else 1
        if total + cost > budget:
            break
        out.append(ch)
        total += cost
    return "".join(out).rstrip() + ELLIPSIS


class XPublisher(Publisher):
    name = "x"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        access_token: str,
        access_token_secret: str,
        max_retries: int = 3,
        kinds: Optional[set] = None,
        prefix: str = "",
    ):
        missing = [
            n
            for n, v in (
                ("X_API_KEY", api_key),
                ("X_API_SECRET", api_secret),
                ("X_ACCESS_TOKEN", access_token),
                ("X_ACCESS_TOKEN_SECRET", access_token_secret),
            )
            if not v
        ]
        if missing:
            raise ValueError("X publisher missing credentials: " + ", ".join(missing))

        # Imported lazily so a truthsocial-only deploy never needs tweepy
        # installed, and importing this module can't fail on the live runner.
        import tweepy  # type: ignore

        self._tweepy = tweepy
        self.max_retries = max_retries
        self.kinds = kinds          # None = accept every kind (see Publisher)
        self.prefix = prefix or ""
        self.client = tweepy.Client(
            consumer_key=api_key,
            consumer_secret=api_secret,
            access_token=access_token,
            access_token_secret=access_token_secret,
            wait_on_rate_limit=False,  # we do our own bounded backoff
        )

    def _render(self, text: str) -> str:
        body = text
        marker = self.prefix.strip()
        if marker and not body.lstrip().startswith(marker):
            body = f"{self.prefix}{body}"
        return clamp_weighted(body)

    def post(self, text: str, in_reply_to: Optional[str] = None) -> Optional[str]:
        kwargs = {"text": self._render(text)}
        if in_reply_to:
            kwargs["in_reply_to_tweet_id"] = in_reply_to

        delay = 5
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.client.create_tweet(**kwargs)
            except (self._tweepy.TooManyRequests, self._tweepy.TwitterServerError) as e:
                # 429 / 5xx are transient — bounded exponential backoff, then
                # give up and let the runner retry on the next poll tick.
                if attempt < self.max_retries:
                    log.warning(
                        "X transient error %s (attempt %d), retrying in %ds",
                        type(e).__name__, attempt, delay,
                    )
                    time.sleep(delay)
                    delay *= 3
                    continue
                raise
            # 4xx (Forbidden/Unauthorized/BadRequest) propagate to the runner,
            # which logs and retries next tick — a persistent 403 usually means
            # the token was minted before Read+Write was enabled.
            data = getattr(resp, "data", None) or {}
            tweet_id = data.get("id")
            log.info("posted to X: tweet %s", tweet_id)
            return str(tweet_id) if tweet_id is not None else None
        return None
