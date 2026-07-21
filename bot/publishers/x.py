"""X publisher — stub for phase 2.

Plan (see design doc §2): X API v2 POST /2/tweets via tweepy, OAuth 1.0a
user context, pay-per-use billing (~$0.015/post; NEVER include links: $0.20).
"""
from __future__ import annotations

from typing import Optional

from .base import Publisher


class XPublisher(Publisher):
    name = "x"

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "X publisher lands in phase 2. Set PLATFORMS=truthsocial for now."
        )

    def post(self, text: str, in_reply_to: Optional[str] = None) -> Optional[str]:
        raise NotImplementedError
