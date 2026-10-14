# overleaf-sync

Bidirectional live sync between Overleaf and an AI coding agent — without going through Git.

## The problem

Overleaf's Git integration is painful when two beings are editing simultaneously (human-human, human-agent): every push risks a conflict, and pushing while someone is typing in the browser fails outright. The Git bridge was not designed for high-frequency, automated writes.

## The solution

This project uses the Overleaf real-time collaboration protocol and speaks it directly. Changes flow both ways in real time — the same way a second browser tab would — so there are no commits, no pushes, and no conflicts.

## Status

Early MVP. The core sync loop is fully working:

- reads the current document state from Overleaf
- applies agent edits via `applyOtUpdate` (insert / delete ops)
- polls for remote changes and merges them locally
- triggers LaTeX compile on demand

Files created, renamed, moved or deleted on disk are mirrored in the project (see below); binary files (images, PDFs) are never uploaded.

## Usage

```bash
pip install -r requirements.txt

python3 overleaf_sync.py <project_id>
```

Get `overleaf_session2` from your browser cookies while logged in to overleaf.com. Get `project_id` from the URL (`/project/<id>`).

### Install and run as a service

```bash
pipx install .            # or: pip install .   → provides the `overleaf-sync` command
overleaf-sync --check <project_id>
```

`contrib/overleaf-sync@.service` is a systemd *user* unit (one instance per project; the cookie goes in a `chmod 600` env file, see the comments in the file). Tests run on Python 3.11–3.14 in CI (`.github/workflows/test.yml`).

## Live concurrent editing

While a coauthor types, their ops are merged into the local file, never written over it (`test_concurrent_edits.py`):

- 🧾 State per doc: `content` = server doc + our in-flight op; the file = `content` + edits not pushed yet.
- 📥 A coauthor's op is transformed (ShareJS text OT, `transform_x`) past our in-flight op and past the unpushed local edits, then applied to the file.
- 📤 A local change is sent as one `applyOtUpdate`; it is acknowledged by its stripped `{v, doc}` echo, not by the callback (which only means "queued"). One in flight per doc.
- 🔁 If the push is rejected or never echoed: re-join the doc and 3-way merge the local file onto the server content.

## Safeguards against destroying the server document

- 🕐 A local change is pushed only after the file has had the same content on two consecutive polls, so a half-written (truncated) save is never sent.
- 🛑 A change that removes more than half of a 200+ char doc is held until the file is stable for 5 s.
- 🗑️ A file that was **emptied** (or left with only whitespace) is never pushed on its own: the sync warns and keeps the doc intact in Overleaf. Restore the file to cancel, or confirm the wipe with `touch <dir>/.overleaf-sync-confirm-wipe` (used once, only after the warning; `--allow-wipe` / `allow_wipe = true` skips the question for scripted use).
- ❌ `joinDoc` timeouts/errors raise instead of looking like an empty doc (which would be merged as "server deleted everything").
- 🔀 A version gap in incoming updates, or an exception in the update handler, drops the socket; the reconnect path re-joins and 3-way merges instead of pushing from a stale copy.
- 💾 The last version both sides agreed on is kept per doc in `<dir>/.overleaf-sync-base/`. At startup, edits made to a file while the sync was stopped are 3-way merged with whatever changed in Overleaf meanwhile and then pushed; the pre-merge file is also copied to `<dir>/.overleaf-sync-backup/<timestamp>/`. With no saved base (first run, or another project) a local file that differs from Overleaf is backed up there and Overleaf's version wins.
- 😀 Overleaf stores every character outside the BMP (emoji, some math symbols) as two U+FFFD, even when typed in the browser. The sync mirrors that in the local file (and says so), so `😀` becomes `��` on disk.
- 📝 Local writes are atomic (temp file + rename) and the merge re-reads the file right before replacing it, so an editor save in between is not lost.
- 🌲 When a doc is added, renamed, moved or removed in Overleaf, the sync reconnects to rebuild the doc tree; docs removed upstream stop syncing (local copy untouched). Events used: `reciveNewDoc`, `reciveEntityRename`, `reciveEntityMove`, `removeEntity` (names as in Overleaf's realtime server; not yet verified against a live project).
- 📁 **Files on disk → project tree** (`--no-tree-sync` / `tree_sync = false` turns it off). A new text file (`.tex .bib .sty .cls .md …`) is created in Overleaf, with its folders, once it has been there for 3 s; a file renamed or moved with its text unchanged becomes a rename / move of the same doc (comments, history and the doc id are kept). Two docs with identical text are never guessed as a rename. **Deleting is never automatic**: the sync says so and waits for `touch <dir>/.overleaf-sync-confirm-wipe` (or `--allow-wipe`); before deleting it keeps the whole doc in `.overleaf-sync-backup/<timestamp>/deleted-docs/` and `deleted.jsonl`. Restoring the file, or restarting the sync, cancels a pending delete. A batch of more than 20 creates/moves at once (an unzip, a branch switch) needs the same confirmation. Folders are only created when needed, never deleted.
- ⚠️ Other local files (images, PDFs, odd names) are never uploaded; a warning is printed.
- 🧪 `--dry-run`: pulls from Overleaf but never sends; logs what would be pushed. Use it first on a real project.
- 🚫 If the server rejects an update (`otUpdateError`, e.g. over the 2 MB doc limit) the local file is backed up and kept, merged from the pre-update doc on reconnect, and that content is not sent again unless the file changes. Content over 2 MiB is not sent at all.
- 💽 Disk full / no permission: if an incoming update cannot be written, the in-memory state is rolled back and the sync reconnects, so the unwritten text is never mistaken for a local deletion. A change that deletes text is only sent after that text has been recorded in `.overleaf-sync-backup` (if that fails the whole change waits and a warning says why; pure additions still go). If a coauthor edits a doc while its emptied-file wipe is waiting for confirmation, the wipe is cancelled and the file gets the full text back.
- 🧾 Operations from the server are validated (position, types, and that a delete's text really is in the document); anything that does not fit means our copy diverged, so the sync reconnects instead of guessing.
- 🐢 HTTP 429 (rate limit): reconnects back off exponentially and honour `Retry-After`.
- 🧭 If the server announces an OT migration stage other than 0 (`ol-otMigrationStage`, Overleaf's move to a new history protocol), the sync refuses to run and says so — it was written and tested against stage 0 only. `--check` prints the stage; `--allow-unknown-protocol` overrides at your own risk.
- 🔒 One sync per directory (lock file); a second instance exits.
- 🔑 `--cookie-file` keeps the cookie out of `ps`/history; an expired or revoked session stops the sync with a clear error after 3 attempts (files untouched).
- 🪢 A doc renamed or moved in Overleaf takes its local file (and pending edits) along.
- 🧬 Concurrent edits: changes are diffed per hunk (patience diff) so a coauthor's text between two of your edits keeps its place; local files are swapped in with `renameat2(RENAME_EXCHANGE)` and the merge is redone if an editor saved meanwhile.
- Overleaf itself rejects a delete whose text does not match the document, and keeps full history (restore from Overleaf's History if needed).

## Known limit: whole-file saves that race a merge

If a tool reads the *whole* file, edits it and writes it back (many editors, some agent "write file" tools) and that write lands in the few milliseconds after the sync merged a coauthor's text into the same file, the write was made from a copy that predates the merge. The coauthor's text disappears from the file and the next push deletes it on Overleaf. A file watcher cannot tell this from a deliberate deletion (several time-based repair heuristics were tried and rejected: they duplicated text and broke the concurrent-edit tests).

Measured against a real Overleaf with a real browser typing (`e2e/browser_e2e.py`, BR9/BR10): with a coauthor typing continuously and the local tool saving 5–10 times per second, roughly 1 run in 6 overwrites 1–2 edits at 5–10 saves/s, and 10–20% under a deliberately adversarial writer that waits 40 ms between read and write. At human or agent pace (a save every few seconds) the window is a fraction of a millisecond per save.

It is never silent and never unrecoverable:
- every deleted string, with its whole line and position, is appended to `.overleaf-sync-backup/<start time>/deleted.jsonl` *before* the push;
- a push that deletes 2000+ characters also keeps a full copy of the document as Overleaf had it (`*.before-push`, last 20);
- Overleaf's History has the previous text as well.

Tools that re-read the file at the moment of writing (e.g. targeted `Edit`/`str_replace` operations) shrink the window to microseconds. Editors that warn "file changed on disk" avoid it entirely.

## Reconnect invariants (data-loss prevention)

These invariants are enforced by the test suite in `test_sync_reconnect.py` (103 tests covering every combination of edits during a network outage).

### Scenario A — No edits during disconnection (idle outage)
Both sides unchanged. **No-op.** Local file stays as-is.

### Scenario B — Only server edits (stale local)
Local file is **identical to the last-synced state** (the user did not edit locally during the outage).  
✅ **Invariant:** the local file is **overwritten** with the latest server content. The poll loop does **NOT** push anything to Overleaf.  
💣 This was the critical data-loss bug: the old code treated a stale file as a pending local edit and pushed old content back to the server, reverting all changes made during the outage.

### Scenario C — Only local edits (genuine pending edits)
Local file has **new content** relative to the pre-disconnect state; server is unchanged.  
✅ **Invariant:** local edits are preserved as-is. The poll loop computes OT ops from server content → local content and pushes them to Overleaf.

### Scenario D — Both sides edited different, non-overlapping regions
Both local and server have new content in distinct parts of the document.  
✅ **Invariant:** a 3-way merge is performed: local changes are computed as OT ops against the old state, then applied on top of the server state. Both sides' changes survive. The poll loop then pushes the merged result to Overleaf.

### Scenario E — Both sides edited overlapping regions
Both local and server modified the same span of text.  
✅ **Invariant:** OT merge is applied deterministically. No crash, no corrupted output, no data loss. Insertions from both sides survive where OT semantics allow.

### Poll-loop correctness (always)
For every reconnect, the in-memory state is set to the server content. The poll loop then diffs (in-memory state → local file) and pushes the result.  
✅ **Invariant:** `apply_sharejs_ops(new_memory, ops_to_push) == new_local` — the poll loop always faithfully reproduces the merged local content.
