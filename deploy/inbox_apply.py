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
    {"path": "docs/projects/setlist/foo.html", "content": "<plain text>"},
    {"path": "docs/img/logo.png", "content_b64": "<base64, for binary>"},
    {"path": "docs/old-thing.html", "delete": true}
  ]
}

Safety rules enforced here:
  - paths must be relative, inside the repo, no "..", no absolute paths
  - paths may not touch .github/ (so a changeset can never alter workflows)
  - only files named push-*.json in the inbox are considered

Job requests: if docs/job-request.json exists in the repo and names a job
from ALLOWED_JOBS, that script is run here (the GitHub-hosted runner has
the network access the sandboxed Claude sessions lack), its output under
docs/ is committed, and the request file is removed so a bad request can
never loop. Only the hard-coded ALLOWED_JOBS commands can run.
"""

import base64
import calendar
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

JOB_REQUEST = "docs/job-request.json"
ALLOWED_JOBS = {
    "backfill-2026": [sys.executable or "python3", "deploy/backfill_2026.py"],
}

# The backfill also runs on a cadence, not only when something asks for it.
# Reason: phish.in posts a show's recording a day or two AFTER the night, so a
# show captured live by show-night has no audio until a later pass picks it up.
# A one-shot request fired the morning after would run too early and be spent.
# The workflow file is the only real clock here and it can't be edited by the
# tooling that maintains this repo, so the cadence is tracked in the push log.
BACKFILL_JOB = "backfill-2026"
BACKFILL_EVERY_HOURS = 6


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
            text = entry.get("content")
            if b64 is not None:
                try:
                    blob = base64.b64decode(b64, validate=True)
                except Exception as exc:
                    return False, message, f"bad base64 for {norm}: {exc}"
            elif text is not None:
                if not isinstance(text, str):
                    return False, message, f"content must be a string for {norm}"
                blob = text.encode("utf-8")
            else:
                return False, message, f"no content or content_b64 for {norm}"
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


def run_job(job, trigger):
    """Run one whitelisted job and commit whatever it wrote under docs/.

    Returns True if a commit was made. Shared by the on-request path and the
    scheduled cadence so both log and commit identically.
    """
    cmd = ALLOWED_JOBS.get(job)
    ok, detail = False, None
    if cmd is None:
        detail = f"unknown job {job!r}"
        print(f"job ({trigger}): {detail}")
    else:
        print(f"job ({trigger}): running {job}: {' '.join(cmd)}", flush=True)
        try:
            result = subprocess.run(cmd, timeout=480)
            ok = result.returncode == 0
            if not ok:
                detail = f"exit code {result.returncode}"
        except subprocess.TimeoutExpired:
            detail = "timed out after 480s"
        print(f"job ({trigger}): {job} {'succeeded' if ok else 'FAILED: ' + str(detail)}")

    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    log = load_log()
    log.setdefault("jobs", []).append({
        "job": job,
        "trigger": trigger,
        "time": stamp,
        "ok": ok,
        **({"error": detail} if detail else {}),
    })
    # Stamp the attempt, not just the success: a job that keeps failing must
    # not re-run every five minutes forever.
    if job == BACKFILL_JOB:
        log["backfill_last"] = stamp
    log["jobs"] = log["jobs"][-50:]
    save_log(log)

    run("git", "add", "-A", "--", "docs")
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if diff.returncode == 0:
        return False
    status = "ok" if ok else f"failed ({detail})"
    run("git", "commit", "-q", "-m", f"[job] {job} ({trigger}): {status}")
    return True


def run_job_if_requested():
    """Run a whitelisted job named by docs/job-request.json. Returns True if
    a commit was made. The request file is always consumed, success or not."""
    if not os.path.exists(JOB_REQUEST):
        return False
    try:
        with open(JOB_REQUEST, "r", encoding="utf-8") as fh:
            req = json.load(fh)
    except Exception:
        req = {}
    job = req.get("job") if isinstance(req, dict) else None
    # Consume the request first so a job that crashes can never loop on it.
    run("git", "rm", "-q", "--ignore-unmatch", "--", JOB_REQUEST)
    return run_job(job, "requested")


def run_backfill_on_cadence():
    """Re-run the backfill every few hours so late-arriving data lands.

    The backfill is incremental and idempotent: it skips shows that are
    already complete, and only tops up the ones still missing audio or gaps.
    A quiet run costs a couple of API calls and commits nothing.
    """
    log = load_log()
    last = log.get("backfill_last")
    if last:
        try:
            age = time.time() - calendar.timegm(time.strptime(last, "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            age = None
        if age is not None and age < BACKFILL_EVERY_HOURS * 3600:
            print(f"backfill: last run {int(age / 60)} min ago - not due")
            return False
    return run_job(BACKFILL_JOB, "cadence")


def main():
    inbox = []
    try:
        inbox = list_inbox()
    except Exception as exc:
        print(f"inbox listing failed (continuing to job check): {exc}")
    print(f"inbox listing: {len(inbox)} file(s)")
    log = load_log()
    seen = {e["file_id"] for e in log["applied"]}

    pending = [
        (fid, name)
        for fid, name in inbox
        if fid not in seen and (name == "" or re.fullmatch(r"push-.*\.json", name))
    ]

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

    if run_job_if_requested():
        made_commits = True
    elif run_backfill_on_cadence():
        made_commits = True

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
