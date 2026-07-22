# Push inbox — how to update mikeside.com from any device

This repo accepts commits through a **push inbox**: drop a changeset file into
the shared Google Drive folder `setlist-bot-push-inbox` and the `push-inbox`
GitHub Actions workflow (GitHub-hosted, every 5 minutes) validates and commits
it to `main`. Results are recorded in `docs/push-log.json`.

## Changeset format

Create a file named `push-<anything>.json` in the Drive folder:

```json
{
  "message": "commit message",
  "files": [
    {"path": "docs/projects/setlist/page.html", "content": "<file text>"},
    {"path": "docs/page2.html", "content_b64": "<base64, for binary>"},
    {"path": "docs/obsolete.html", "delete": true}
  ]
}
```

## Rules

- Only `push-*.json` files are processed, each exactly once (tracked by Drive file id in `docs/push-log.json`).
- Paths must be relative and inside the repo; `.github/` is off-limits, so a changeset can never modify workflows.
- Invalid changesets are rejected with the reason logged in `docs/push-log.json`.
- The workflow runs on GitHub-hosted runners, so it never queues behind show-night jobs on the self-hosted runner.

## Verifying a push

Read `docs/push-log.json` on `main` (or the commit list). Each entry has the
Drive file id, timestamp, ok/error, and the commit message used.

*This file was itself committed through the push inbox as its end-to-end test.*
