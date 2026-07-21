"""Live setlist page for mikeside.com (GitHub Pages).

During a show the bot rewrites docs/setlist.json and commits it; GitHub Pages
redeploys, and the static docs/index.html renders it (auto-refreshing).
"""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .phishnet import SetlistEntry

log = logging.getLogger(__name__)


def build_payload(
    entries: list[SetlistEntry],
    durations: dict[tuple, Optional[int]],
    complete: bool = False,
) -> dict:
    sets: list[dict] = []
    for e in entries:
        if not sets or sets[-1]["label"] != e.set_label:
            sets.append({"label": e.set_label, "display": e.set_display, "songs": []})
        sets[-1]["songs"].append(
            {
                "title": e.song,
                "transition": e.transition.strip(),
                "length_secs": durations.get(e.key),
                "footnote": e.footnote or None,
            }
        )
    first = entries[0] if entries else None
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "showdate": first.showdate if first else None,
        "venue": first.venue if first else None,
        "city": first.city if first else None,
        "state": first.state if first else None,
        "complete": complete,
        "sets": sets,
    }


def write_if_changed(payload: dict, site_dir: str | Path) -> bool:
    """Write setlist.json if content (minus timestamp) changed. Returns True if written."""
    path = Path(site_dir) / "setlist.json"
    new_body = {k: v for k, v in payload.items() if k != "updated_at"}
    if path.exists():
        try:
            old = json.loads(path.read_text())
            if {k: v for k, v in old.items() if k != "updated_at"} == new_body:
                return False
        except (json.JSONDecodeError, OSError):
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1))
    return True


def git_push(site_dir: str | Path, message: str) -> bool:
    """Commit and push the site dir. Quietly no-ops outside a git checkout."""
    root = Path(site_dir).resolve().parent
    try:
        subprocess.run(["git", "add", str(Path(site_dir))], cwd=root, check=True, capture_output=True)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=root, capture_output=True)
        if diff.returncode == 0:
            return False  # nothing staged
        subprocess.run(["git", "commit", "-m", message], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "push"], cwd=root, check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        out = getattr(e, "stderr", b"") or b""
        log.warning("site git push skipped/failed: %s %s", e, out.decode(errors="replace")[:200])
        return False


def update_site(
    entries: list[SetlistEntry],
    durations: dict[tuple, Optional[int]],
    site_dir: str | Path,
    push: bool = False,
    complete: bool = False,
) -> bool:
    if not entries:
        return False
    payload = build_payload(entries, durations, complete=complete)
    if not write_if_changed(payload, site_dir):
        return False
    log.info("site: setlist.json updated (%d songs)", sum(len(s["songs"]) for s in payload["sets"]))
    if push:
        git_push(site_dir, f"setlist update {payload['showdate']}")
    return True
