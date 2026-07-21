"""Truth Social publisher.

Truth Social has no official posting API; this uses the same internal
Mastodon-derived endpoint the web app uses (POST /api/v1/statuses) with a
bearer token from the bot account. Endpoint shape borrowed from the
TruthAutonomy project (MIT).

Getting a bearer token:
  1. Log into truthsocial.com in a browser as the bot account.
  2. Open DevTools -> Network, refresh, click any /api/ request.
  3. Copy the `Authorization: Bearer ...` header value (without "Bearer ").
  4. Put it in .env as TRUTH_BEARER_TOKEN.

Cloudflare fronts truthsocial.com, so we use `cloudscraper` when available
(install on the VPS: pip install cloudscraper). Falls back to plain requests.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from .base import Publisher

log = logging.getLogger(__name__)

BASE = "https://truthsocial.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class TruthPublisher(Publisher):
    name = "truthsocial"

    def __init__(self, bearer_token: str, max_retries: int = 3):
        if not bearer_token:
            raise ValueError("TRUTH_BEARER_TOKEN is required")
        self.max_retries = max_retries
        self.headers = {
            "Accept": "*/*",
            "User-Agent": UA,
            "Authorization": f"Bearer {bearer_token}",
            "Origin": BASE,
            "Referer": BASE,
            "Content-Type": "application/json",
        }
        try:
            import cloudscraper  # type: ignore

            self.http = cloudscraper.create_scraper()
        except ImportError:
            import requests

            log.warning("cloudscraper not installed; using plain requests (may be blocked by Cloudflare)")
            self.http = requests.Session()

    def post(self, text: str, in_reply_to: Optional[str] = None) -> Optional[str]:
        payload = {
            "status": text,
            "media_ids": [],
            "visibility": "public",
            "content_type": "text/plain",
            "in_reply_to_id": in_reply_to,
            "quote_id": None,
            "poll": None,
            "group_timeline_visible": False,
        }
        delay = 5
        for attempt in range(1, self.max_retries + 1):
            resp = self.http.post(f"{BASE}/api/v1/statuses", headers=self.headers, json=payload)
            if resp.status_code == 200:
                body = resp.json()
                self._respect_ratelimit(resp)
                return str(body.get("id"))
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                log.warning("truthsocial %s (attempt %d), retrying in %ds", resp.status_code, attempt, delay)
                time.sleep(delay)
                delay *= 3
                continue
            raise RuntimeError(f"Truth Social post failed: {resp.status_code} {resp.text[:300]}")
        return None

    @staticmethod
    def _respect_ratelimit(resp):
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining is not None and int(remaining) <= 1:
            reset = int(resp.headers.get("X-RateLimit-Reset", 0))
            wait = max(0, reset - int(time.time()))
            if wait:
                log.warning("Truth Social rate limit nearly hit; sleeping %ds", wait)
                time.sleep(min(wait, 300))
