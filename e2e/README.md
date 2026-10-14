# End-to-end tests against a real Overleaf (Community Edition)

These run the sync against a throwaway self-hosted Overleaf, never overleaf.com.
Last run: all scenarios pass (local edit, remote edit, concurrent edits,
truncate/rewrite, deliberate wipe, offline edit + backup, new/renamed/deleted
doc, connection drop with edits on both sides).

1. `up_local_overleaf.sh` starts MongoDB 8 (replica set), Redis and
   `sharelatex/sharelatex` with podman on a host with ~10 GB free; it listens on
   127.0.0.1:8080 (tunnel it with `ssh -L 18080:127.0.0.1:8080 <host>`).
2. Create a user with `node modules/server-ce-scripts/scripts/create-user.mjs --admin --email=...`
   inside the container, put the activation token/user id in `setup_local_overleaf.py`,
   run it: it writes `/tmp/olt/state.json` (project id + `overleaf.sid`).
3. `pip install requests websocket-client`, then run `e2e_sync.py` and
   `e2e_tree_and_outage.py` (paths are `/tmp/olt` and `/tmp/olt/venv`).

The sync reads `OVERLEAF_URL` and `OVERLEAF_COOKIE_NAME` (`overleaf.sid` on CE).

## Real overleaf.com with a real Chrome

`real_browser_ctl.py` is the local half of scenarios where a real Chrome edits the web editor (driven by Claude in Chrome, timed `view.dispatch` calls) while files change locally: typing, same-line edits, deletion, whole-file reformat, Unicode, second document, burst, undo, offline restart, full wipe, over-2-MiB file. It verifies browser == server == local and that nothing was lost or duplicated. Use a throw-away project only; `restore` rewrites it.
