#!/usr/bin/env python3
"""Push-inbox applier for the phish-setlist-bot repo.

Polls a publicly-viewable Google Drive folder for changeset files named
push-*.json, applies any that haven't been applied yet, commits them to the
repo, and records the result in docs/push-log.json.

Runs inside GitHub Actions (see .github/workflows/push-inbox.yml) on a
GitHub-hosted runner, so it never competes with the self-hosted show-night
runner. No Google credentials needed: the folder is listed via the public
embeddedfolderview endpoint and files are fetched via the public download URL.
If a DRIVE_API_KEY env var is present, the official Drive API is used for
listing instead (more robust, optional).

Changeset format (one JSON file per push, named push-<anything>.json):
{
  "message": "commit message here",
  "files": [
    {"path": "docs/projects/setlist/foo.html", "content_b64": "<base64>"},
    {"path": "docs/old-thing.html", "delete": true}
  ]
}

Safety rules enforced here:
  - paths must be relative, inside the repo, no "..", no absolute paths
  - paths may not touch .github/ (so a changeset can never alter workflows)
  - only files named push-*.json in the inbox are considered
"""

import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error

FOLDER_ID = os.environ.get("INBOX_FOLDER_ID", "1c_Kg5A_LMxVI55u6H2U1Z5mAg8NjwDaQ")
LOG_PATH = "docs/push-log.json"
UA = "phish-setlist-bot-inbox/1.0"
FORBIDDEN_PREFIXES = (".github/",)


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def list_inbox():
    """Return list of (file_id, name) for files in the inbox folder."""
    api_key = os.environ.get("DRIVE_API_KEY")
    if api_key:
        url = (
            "https://www.googleapis.com/drive/v3/files"
            f"?q='{FOLDER_ID}'+in+parents+and+trashed=false"
            f"&fields=files(id,name)&pageSize=1000&key={api_key}"
        )
        data = json.loads(http_get(url).decode("utf-8"))
        return [(f["id"], f["name"]) for f in data.get("files", [])]

    # Keyless fallback: public folder embedded view.
    html = http_get(
        f"https://drive.google.com/embeddedfolderview?id={FOLDER_ID}#list"
    ).decode("utf-8", "replace")
    entries = re.findall(
        r'id="entry-([-\w]{10,})".*?flip-entry-title">([^<]+)<', html, re.S
    )
    if not entries:
        # Secondary pattern in case markup shifts slightly.
        ids = re.findall(r'/file/d/([-\w]{10,})/', html)
        entries = [(i, "") for i in dict.fromkeys(ids)]
    return entries


def download(file_id):
    return http_get(f"https://drive.google.com/uc?export=download&id={file_id}")


def load_log():
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return {"applied": []}


def save_log(log):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as fh:
        json.dump(log, fh, indent=2)
        fh.write("\n")


def safe_path(path):
    if not isinstance(path, str) or not path:
        return None
    p = path.replace("\\", "/").lstrip("/")
    parts = [seg for seg in p.split("/") if seg not in ("", ".")]
    if any(seg == ".." for seg in parts):
        return None
    norm = "/".join(parts)
    for pref in FORBIDDEN_PREFIXES:
        if norm == pref.rstrip("/") or norm.startswith(pref):
            return None
    return norm


def run(*cmd, check=True):
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=check)


def apply_changeset(name, raw):
    """Apply one changeset. Returns (ok, message, detail)."""
    try:
        cs = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        return False, "(unparseable)", f"invalid JSON: {exc}"

    message = cs.get("message") or f"[inbox] apply {name}"
    files = cs.get("files")
    if not isinstance(files, list) or not files:
        return False, message, "changeset has no files[]"

    staged = []
    for entry in files:
        norm = safe_path(entry.get("path"))
        if norm is None:
            return False, message, f"rejected unsafe path: {entry.get('path')!r}"
        if entry.get("delete"):
            staged.append(("delete", norm, None))
        else:
            b64 = entry.get("content_b64")
            if b64 is None:
                return False, message, f"no content_b64 for {norm}"
            try:
                blob = base64.b64decode(b64, validate=True)
            except Exception as exc:
                return False, message, f"bad base64 for {norm}: {exc}"
            staged.append(("write", norm, blob))

    # All entries validated - now touch the working tree.
    for op, norm, blob in staged:
        if op == "delete":
            if os.path.exists(norm):
                run("git", "rm", "-q", "--", norm)
        else:
            d = os.path.dirname(norm)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(norm, "wb") as fh:
                fh.write(blob)
            run("git", "add", "--", norm)
    return True, message, None


def main():
    inbox = list_inbox()
    print(f"inbox listing: {len(inbox)} file(s)")
    log = load_log()
    seen = {e["file_id"] for e in log["applied"]}

    pending = [
        (fid, name)
        for fid, name in inbox
        if fid not in seen and (name == "" or re.fullmatch(r"push-.*\.json", name))
    ]
    if not pending:
        print("nothing to apply")
        return 0

    made_commits = False
    for fid, name in sorted(pending, key=lambda t: t[1]):
        label = name or fid
        print(f"applying {label} ({fid})")
        try:
            raw = download(fid)
        except Exception as exc:
            print(f"  download failed, will retry next run: {exc}")
            continue
        if name == "" and not raw.lstrip()[:1] == b"{":
            print("  not JSON, skipping")
            continue
        ok, message, detail = apply_changeset(label, raw)
        entry = {
            "file_id": fid,
            "name": label,
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "ok": ok,
            "message": message,
        }
        if not ok:
            entry["error"] = detail
            print(f"  REJECTED: {detail}")
            log["applied"].append(entry)
            save_log(log)
            run("git", "add", "--", LOG_PATH)
            run("git", "commit", "-q", "-m", f"[inbox] reject {label}: {detail}")
            made_commits = True
            continue

        log["applied"].append(entry)
        save_log(log)
        run("git", "add", "--", LOG_PATH)
        run("git", "commit", "-q", "-m", f"[inbox] {message}")
        made_commits = True
        print(f"  committed: {message}")

    if not made_commits:
        print("no commits made")
        return 0

    # Push with a rebase-retry loop in case show-night's site.py races us.
    for attempt in range(4):
        run("git", "pull", "--rebase", "origin", "main")
        result = subprocess.run(["git", "push", "origin", "main"])
        if result.returncode == 0:
            print("pushed")
            return 0
        print(f"push attempt {attempt + 1} failed, retrying")
        time.sleep(5)
    print("giving up on push", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
