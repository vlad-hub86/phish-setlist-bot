"""Publisher interface + dry-run implementation."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional

log = logging.getLogger(__name__)


class Publisher(ABC):
    name: str = "base"

    # Post kinds this platform accepts. None = accept everything.
    #
    # Truth Social is the sandbox and leaves this None, so every post kind —
    # including brand-new ones — lands there first. X carries an explicit
    # allowlist (X_POST_KINDS); a kind is promoted to X only after it has been
    # watched on Truth for a show. That is the whole staging pipeline.
    kinds: Optional[set] = None

    def accepts(self, kind: Optional[str]) -> bool:
        """Whether this platform should receive a post of this kind."""
        if self.kinds is None or kind is None:
            return True
        return kind in self.kinds

    @abstractmethod
    def post(self, text: str, in_reply_to: Optional[str] = None) -> Optional[str]:
        """Publish text. Returns the remote post ID, or None on accepted-but-unknown."""


class DryRunPublisher(Publisher):
    """Logs posts instead of sending them. Used for testing and shadow runs."""

    def __init__(self, name: str = "dry-run"):
        self.name = name
        self.sent: list[str] = []

    def post(self, text: str, in_reply_to: Optional[str] = None) -> Optional[str]:
        self.sent.append(text)
        banner = f"--- [{self.name}] would post ({len(text)} chars) ---"
        log.info("%s\n%s\n%s", banner, text, "-" * len(banner))
        return f"dry-{len(self.sent)}"
