#!/usr/bin/env python3
"""
Overleaf Protocol Reverse Engineering CLI
==========================================

Demonstrates the Overleaf real-time editing protocol step by step.

Reverse-engineered from HAR capture of www.overleaf.com (2026-04-09).

STEP 1: Authentication  → overleaf_session2 cookie + CSRF token
STEP 2: Project info    → GET /project/{id} HTML with ol-* meta bootstrap data
STEP 3: File blobs      → GET /project/{id}/blob/{sha} (base64 content)
STEP 4: History/OT ops  → GET /project/{id}/latest/history (textOperation arrays)
STEP 5: Socket.io v0.9  → wss://www.overleaf.com/socket.io/1/websocket/{sid}
STEP 6: Live edit       → joinProject → joinDoc → applyOtUpdate (ShareJS ops)
STEP 7: Compile         → POST /project/{id}/compile

Usage:
  python3 overleaf_cli.py --cookie <overleaf_session2> info <project_id>
  python3 overleaf_cli.py --cookie <overleaf_session2> read <project_id> <file_path>
  python3 overleaf_cli.py --cookie <overleaf_session2> history <project_id>
  python3 overleaf_cli.py --cookie <overleaf_session2> connect <project_id>
  python3 overleaf_cli.py --cookie <overleaf_session2> edit <project_id> <doc_id> <position> <text>
  python3 overleaf_cli.py --cookie <overleaf_session2> compile <project_id>
  python3 overleaf_cli.py analyze-har <har_file>
"""

import argparse
import base64
import hashlib
import html
import json
import re
import sys
import time
import os
import threading
import urllib.parse
from typing import Optional

import requests
import websocket  # pip install websocket-client

# Point OVERLEAF_URL at a self-hosted instance (e.g. http://localhost:8080) to
# test against it; OVERLEAF_COOKIE_NAME is the session cookie there (CE uses
# "overleaf.sid").
BASE_URL = os.environ.get("OVERLEAF_URL", "https://www.overleaf.com").rstrip("/")
COOKIE_NAME = os.environ.get("OVERLEAF_COOKIE_NAME", "overleaf_session2")
_HOST = urllib.parse.urlparse(BASE_URL).hostname or "www.overleaf.com"
WS_BASE = ("wss" if BASE_URL.startswith("https") else "ws") + "://" + BASE_URL.split("://", 1)[1]


# ---------------------------------------------------------------------------
# STEP 1 & 2: Authentication + Project Bootstrap
# ---------------------------------------------------------------------------

class AuthError(RuntimeError):
    """The session cookie was rejected (expired, logged out, or wrong)."""


class OverleafSession:
    """Manages auth and extracts bootstrap data from Overleaf project page."""

    def __init__(self, session_cookie: str):
        self.session = requests.Session()
        # The only required cookie for auth is overleaf_session2
        self.session.cookies.set(COOKIE_NAME, session_cookie, domain=_HOST)
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; overleaf-cli/1.0)",
            "Origin": BASE_URL,
            "Referer": BASE_URL + "/project",
        })
        self._csrf_token: Optional[str] = None

    def get_project_bootstrap(self, project_id: str) -> dict:
        """
        STEP 2: Fetch project page and extract ol-* meta bootstrap data.

        Overleaf embeds all project config in <meta name="ol-*"> tags:
          - ol-csrfToken         → CSRF token for POST requests
          - ol-project_id        → project ID
          - ol-user_id           → user ID
          - ol-user              → full user object
          - ol-projectName       → project name
          - ol-imageNames        → available LaTeX compilers
          - ol-otMigrationStage  → 0 = old OT protocol, 1+ = new history protocol
        """
        resp = self.session.get(f"{BASE_URL}/project/{project_id}")
        if resp.status_code in (401, 403) or "/login" in resp.url:
            raise AuthError(
                "Overleaf rejected the session cookie (expired, logged out or wrong). "
                "Log in again in the browser and pass a fresh cookie."
            )
        if resp.status_code == 404:
            raise RuntimeError(f"project {project_id} not found (or no access)")
        resp.raise_for_status()

        bootstrap = {}
        # Parse all <meta name="ol-*" content="..."> tags
        for m in re.finditer(
            r'<meta\s+name="(ol-[^"]+)"[^>]+content="([^"]*)"',
            resp.text
        ):
            name = m.group(1)
            val = html.unescape(m.group(2))
            try:
                bootstrap[name] = json.loads(val)
            except json.JSONDecodeError:
                bootstrap[name] = val

        # Also handle tags with data-type but no content (booleans)
        for m in re.finditer(
            r'<meta\s+name="(ol-[^"]+)"\s+data-type="boolean"[^/]*/?>',
            resp.text
        ):
            name = m.group(1)
            if name not in bootstrap:
                bootstrap[name] = True

        self._csrf_token = bootstrap.get("ol-csrfToken")
        return bootstrap

    @property
    def csrf_token(self) -> Optional[str]:
        return self._csrf_token

    def _json_headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self._csrf_token:
            h["X-Csrf-Token"] = self._csrf_token
        return h


# ---------------------------------------------------------------------------
# STEP 3: File Blob API  (content-addressed, Git-style SHA hashes)
# ---------------------------------------------------------------------------

class BlobAPI:
    """
    STEP 3: Read file content via the blob API.

    Overleaf stores files in a content-addressed store keyed by SHA-1 hash.
    The hash comes from the history API (see HistoryAPI below).

    GET /project/{projectId}/blob/{sha}
    → Response: application/octet-stream, body is the raw file bytes.
    HAR shows the response body is base64-encoded in the HAR JSON but the
    actual HTTP response is raw binary.
    """

    def __init__(self, session: OverleafSession):
        self.s = session

    def read(self, project_id: str, sha: str) -> bytes:
        resp = self.s.session.get(f"{BASE_URL}/project/{project_id}/blob/{sha}")
        resp.raise_for_status()
        return resp.content

    def read_text(self, project_id: str, sha: str) -> str:
        return self.read(project_id, sha).decode("utf-8")


# ---------------------------------------------------------------------------
# STEP 4: History API + TextOperation format
# ---------------------------------------------------------------------------

class HistoryAPI:
    """
    STEP 4: Fetch and reconstruct document history.

    GET /project/{projectId}/latest/history
    → { chunk: { history: { snapshot: { files: {} }, changes: [...] } } }

    Each change has:
      operations: [{ pathname, textOperation | file }]
      timestamp: ISO string
      v2DocVersions: { docId: { pathname, v: version } }

    TextOperation format (array):
      - Number > 0 : retain N characters
      - String     : insert string
      - Number < 0 : delete |N| characters

    Example: [11808, "\\ninserted text", 27856]
      = retain 11808, insert "\\ninserted text", retain 27856
    """

    def __init__(self, session: OverleafSession):
        self.s = session

    def get_latest(self, project_id: str) -> dict:
        resp = self.s.session.get(f"{BASE_URL}/project/{project_id}/latest/history")
        resp.raise_for_status()
        return resp.json()

    def reconstruct_file(self, project_id: str, pathname: str) -> str:
        """Replay all textOperations for a file to get current content."""
        data = self.get_latest(project_id)
        changes = data["chunk"]["history"]["changes"]

        content = ""
        # First, find if the file was created with a blob hash
        for ch in changes:
            for op in ch.get("operations", []):
                if op.get("pathname") != pathname:
                    continue
                if "file" in op:
                    # File was added from a blob
                    sha = op["file"].get("hash")
                    if sha:
                        content = BlobAPI(self.s).read_text(project_id, sha)
                elif "textOperation" in op:
                    content = apply_text_operation(content, op["textOperation"])
        return content

    @staticmethod
    def get_file_versions(history_data: dict) -> dict[str, int]:
        """Extract latest doc version numbers for socket.io joinDoc."""
        versions = {}
        for ch in history_data["chunk"]["history"]["changes"]:
            for doc_id, info in ch.get("v2DocVersions", {}).items():
                versions[doc_id] = info["v"]
        return versions

    @staticmethod
    def get_file_sha_map(history_data: dict) -> dict[str, str]:
        """Build pathname → sha map from history."""
        sha_map = {}
        for ch in history_data["chunk"]["history"]["changes"]:
            for op in ch.get("operations", []):
                pn = op.get("pathname")
                if pn and "file" in op:
                    sha = op["file"].get("hash")
                    if sha:
                        sha_map[pn] = sha
        return sha_map


def apply_text_operation(content: str, operation: list) -> str:
    """
    Apply an Overleaf textOperation to a string.

    textOperation is an array of:
      int > 0 → retain N chars (advance cursor by N)
      str     → insert string at cursor
      int < 0 → delete |N| chars at cursor
    """
    result = []
    cursor = 0
    for component in operation:
        if isinstance(component, int):
            if component > 0:
                # retain
                result.append(content[cursor:cursor + component])
                cursor += component
            else:
                # delete
                cursor += abs(component)
        elif isinstance(component, str):
            # insert
            result.append(component)
    # Append remaining content if any
    result.append(content[cursor:])
    return "".join(result)


# ---------------------------------------------------------------------------
# STEP 5 & 6: Socket.io v0.9 real-time editing
# ---------------------------------------------------------------------------

class SocketIOClient:
    """
    STEP 5 & 6: Socket.io v0.9 (not 1.x!) real-time collaboration.

    Protocol overview:
    ==================
    Overleaf uses Socket.io v0.9 with WebSocket transport.

    HANDSHAKE (HTTP):
      GET /socket.io/1/?projectId={id}&esh=1&ssp=1&t={timestamp}
      → "{sessionId}:60:60:websocket,xhr-polling"

    WEBSOCKET URL:
      wss://www.overleaf.com/socket.io/1/websocket/{sessionId}
             ?projectId={id}&esh=1&ssp=1

    MESSAGE FORMAT: "{type}:{id}:{endpoint}:{data}"
      Type 0 = disconnect
      Type 1 = connect
      Type 2 = heartbeat
      Type 5 = event  (5:::{json})
      Type 6 = ack    (6:::{id}+{json})

    FLOW:
      1. Server sends:  "1::"  (connected)
      2. Client sends:  "5:::{\"name\":\"joinProject\",\"args\":[{\"project_id\":\"...\"}]}"
      3. Server sends:  "6:::1+[null, {project data}]"
      4. Client sends:  "5:::{\"name\":\"joinDoc\",\"args\":[\"<docId>\",{\"encodeRanges\":true}]}"
      5. Server sends:  "6:::2+[null, [\"line1\",\"line2\",...], version, [], {}]"
      6. Client sends heartbeat every 25s: "2::"
      7. To edit:       "5:::{\"name\":\"applyOtUpdate\",\"args\":[\"<docId>\",{...}]}"

    APPLYOTUPDATE format:
      {
        "doc": "<docId>",
        "op": [{"p": <position>, "i": "<text>"}],  // insert
              [{"p": <position>, "d": "<text>"}],  // delete
        "v": <current_version>,
        "hash": "<sha1_of_new_content>"  // optional, for integrity
      }
    """

    def __init__(self, session: OverleafSession, project_id: str):
        self.session = session
        self.project_id = project_id
        self.ws = None
        self._msg_id = 0
        self._callbacks = {}
        self._connected = threading.Event()
        self._session_id = None
        self._heartbeat_thread = None
        self._running = False

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    def connect(self) -> str:
        """Perform Socket.io handshake and open WebSocket."""
        # Step 1: HTTP handshake to get session ID
        t = int(time.time() * 1000)
        params = f"projectId={self.project_id}&esh=1&ssp=1&t={t}"
        resp = self.session.session.get(f"{BASE_URL}/socket.io/1/?{params}")
        resp.raise_for_status()

        # Response format: "{sessionId}:60:60:websocket,xhr-polling"
        self._session_id = resp.text.split(":")[0]
        print(f"[socket.io] Session ID: {self._session_id}")

        # Step 2: Build WebSocket URL with session cookie
        cookie_str = "; ".join(
            f"{c.name}={c.value}"
            for c in self.session.session.cookies
        )
        ws_url = (
            f"{WS_BASE}/socket.io/1/websocket/{self._session_id}"
            f"?projectId={self.project_id}&esh=1&ssp=1"
        )

        self._running = True
        self.ws = websocket.WebSocketApp(
            ws_url,
            header={"Cookie": cookie_str},
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        return self._session_id

    def run_forever(self):
        """Run WebSocket in a background thread."""
        t = threading.Thread(target=self.ws.run_forever, daemon=True)
        t.start()
        self._connected.wait(timeout=15)
        return t

    def _on_open(self, ws):
        print("[socket.io] WebSocket opened")

    def _on_message(self, ws, raw: str):
        msg_type, *rest = raw.split(":", 3)
        msg_type = int(msg_type)
        data = rest[2] if len(rest) >= 3 else ""

        if msg_type == 1:  # connect
            print("[socket.io] Connected to namespace")
            self._connected.set()
            self._start_heartbeat()

        elif msg_type == 2:  # heartbeat
            ws.send("2::")  # echo back

        elif msg_type == 5:  # event
            try:
                payload = json.loads(data)
                self._handle_event(payload.get("name"), payload.get("args", []))
            except Exception as e:
                print(f"[socket.io] Event parse error: {e} — raw: {data[:200]}")

        elif msg_type == 6:  # ack
            ack_raw = data
            if "+" in ack_raw:
                plus_pos = ack_raw.index("+")
                ack_id = int(ack_raw[:plus_pos])
                tail = ack_raw[plus_pos + 1:]
                ack_data = json.loads(tail) if tail else [None]
            else:
                # No data payload — server sends bare ack ID (e.g. applyOtUpdate)
                ack_id = int(ack_raw)
                ack_data = [None]
            if ack_id in self._callbacks:
                self._callbacks.pop(ack_id)(ack_data)

    def _on_error(self, ws, error):
        print(f"[socket.io] Error: {error}")

    def _on_close(self, ws, code, msg):
        self._running = False
        print(f"[socket.io] Closed: {code} {msg}")

    def _handle_event(self, name: str, args: list):
        print(f"[socket.io] Event: {name} args={json.dumps(args)[:200]}")

    def _start_heartbeat(self):
        def beat():
            while self._running and self.ws:
                time.sleep(25)
                if self._running:
                    try:
                        self.ws.send("2::")
                    except Exception:
                        break
        self._heartbeat_thread = threading.Thread(target=beat, daemon=True)
        self._heartbeat_thread.start()

    def send_event(self, name: str, args: list, callback=None) -> int:
        """Send a Socket.io event (type 5), optionally with ack callback."""
        msg_id = self._next_id()
        payload = json.dumps({"name": name, "args": args})
        if callback:
            self._callbacks[msg_id] = callback
            self.ws.send(f"5:{msg_id}+::{payload}")
        else:
            self.ws.send(f"5:::{payload}")
        return msg_id

    def join_project(self) -> dict:
        """Send joinProject and wait for response."""
        result = {}
        done = threading.Event()

        def on_ack(data):
            # data = [null, {project data}]
            if data and len(data) > 1:
                result.update(data[1] or {})
            done.set()

        self._connected.wait(timeout=10)
        self.send_event("joinProject", [{"project_id": self.project_id}], callback=on_ack)
        done.wait(timeout=10)
        return result

    def join_doc(self, doc_id: str) -> tuple[list[str], int]:
        """
        Send joinDoc and get document lines + version.
        Returns (lines, version).  Raises RuntimeError on timeout or error
        ack: an unanswered join must never look like an empty document.
        """
        lines = []
        version = 0
        done = threading.Event()
        ok = [False]
        err: list = [None]

        def on_ack(data):
            nonlocal version
            # data = [null, [lines...], version, ranges, ...]
            if data and data[0] is None and len(data) >= 3 and data[1] is not None:
                lines.extend(data[1])
                version = data[2] or 0
                ok[0] = True
            else:
                err[0] = data[0] if data and data[0] is not None else data
            done.set()

        self.send_event(
            "joinDoc",
            [doc_id, {"encodeRanges": True}],
            callback=on_ack
        )
        if not done.wait(timeout=10):
            raise RuntimeError(f"joinDoc {doc_id}: no answer from server")
        if not ok[0]:
            raise RuntimeError(f"joinDoc {doc_id} failed: {err[0]}")
        return lines, version

    def apply_insert(self, doc_id: str, position: int, text: str, version: int) -> bool:
        """
        STEP 6: Apply an insert operation via Socket.io.

        This is the core real-time editing operation.
        op format: [{"p": position, "i": "inserted text"}]
        """
        done = threading.Event()
        success = [False]

        def on_ack(data):
            # data = [null] on success, [error_msg] on failure
            if data and data[0] is None:
                success[0] = True
                print(f"[socket.io] Insert applied at pos {position}: {repr(text[:50])}")
            else:
                print(f"[socket.io] Insert failed: {data}")
            done.set()

        update = {
            "doc": doc_id,
            "op": [{"p": position, "i": text}],
            "v": version,
            "dupIfSource": [],
        }
        self.send_event("applyOtUpdate", [doc_id, update], callback=on_ack)
        done.wait(timeout=10)
        return success[0]

    def apply_delete(self, doc_id: str, position: int, text: str, version: int) -> bool:
        """Apply a delete operation: delete 'text' at 'position'."""
        done = threading.Event()
        success = [False]

        def on_ack(data):
            if data and data[0] is None:
                success[0] = True
                print(f"[socket.io] Delete applied at pos {position}: {repr(text[:50])}")
            else:
                print(f"[socket.io] Delete failed: {data}")
            done.set()

        update = {
            "doc": doc_id,
            "op": [{"p": position, "d": text}],
            "v": version,
            "dupIfSource": [],
        }
        self.send_event("applyOtUpdate", [doc_id, update], callback=on_ack)
        done.wait(timeout=10)
        return success[0]

    def leave_doc(self, doc_id: str):
        self.send_event("leaveDoc", [doc_id])

    def disconnect(self):
        self._running = False
        if self.ws:
            self.ws.close()


# ---------------------------------------------------------------------------
# STEP 7: Compile API
# ---------------------------------------------------------------------------

class CompileAPI:
    """
    STEP 7: Trigger a LaTeX compile.

    POST /project/{projectId}/compile?auto_compile=true&enable_pdf_caching=true
    Body: {
      "rootDoc_id": "<docId>",
      "rootResourcePath": "main.tex",
      "draft": false,
      "check": "silent",
      "incrementalCompilesEnabled": true,
      "stopOnFirstError": false,
      "editorId": "<uuid>"
    }
    Response: { status: "success"|"failure"|"clsi-maintenance",
                outputFiles: [...], stats: {...} }
    """

    def __init__(self, session: OverleafSession):
        self.s = session

    def compile(self, project_id: str, root_doc_id: str,
                root_resource_path: str = "main.tex") -> dict:
        import uuid
        body = {
            "rootDoc_id": root_doc_id,
            "rootResourcePath": root_resource_path,
            "draft": False,
            "check": "silent",
            "incrementalCompilesEnabled": True,
            "stopOnFirstError": False,
            "editorId": str(uuid.uuid4()),
        }
        resp = self.s.session.post(
            f"{BASE_URL}/project/{project_id}/compile"
            "?auto_compile=true&enable_pdf_caching=true",
            json=body,
            headers=self.s._json_headers(),
        )
        resp.raise_for_status()
        return resp.json()

    def flush(self, project_id: str):
        """Flush pending document changes before compiling."""
        resp = self.s.session.post(
            f"{BASE_URL}/project/{project_id}/flush",
            headers=self.s._json_headers(),
        )
        resp.raise_for_status()


# ---------------------------------------------------------------------------
# HAR Analysis utility
# ---------------------------------------------------------------------------

def analyze_har(har_path: str):
    """Parse a HAR file and summarize the Overleaf protocol interactions."""
    print(f"Analyzing HAR: {har_path}\n")
    with open(har_path) as f:
        har = json.load(f)

    entries = har["log"]["entries"]
    print(f"Total entries: {len(entries)}\n")

    # Categorize
    websockets = [e for e in entries if e["request"]["url"].startswith("wss://")]
    api_calls = [e for e in entries if "overleaf.com" in e["request"]["url"]
                 and not e["request"]["url"].startswith("wss://")
                 and "cdn.overleaf.com" not in e["request"]["url"]]

    print(f"WebSocket connections: {len(websockets)}")
    for ws in websockets:
        print(f"  {ws['request']['url']}")
        print(f"  → {ws['response']['status']} {ws['response']['statusText']}")

    print(f"\nKey API calls ({len(api_calls)} total):")
    seen = set()
    for e in api_calls:
        url = e["request"]["url"]
        method = e["request"]["method"]
        status = e["response"]["status"]
        # Normalize URL by replacing project/user IDs
        key = re.sub(r'/[0-9a-f]{24}', '/<id>', url)
        key = re.sub(r'\?.*', '', key)
        if key not in seen:
            seen.add(key)
            print(f"  {method:6} {status} {key}")

    # Extract Socket.io session handshake
    print("\nSocket.io handshakes:")
    for e in entries:
        if "socket.io/1/?" in e["request"]["url"]:
            print(f"  Response: {e['response']['content'].get('text', '')}")

    # Show CSRF and session cookie
    print("\nAuthentication artifacts:")
    for e in entries:
        if "overleaf.com/project" in e["request"]["url"]:
            for c in e["request"]["cookies"]:
                if c["name"] == "overleaf_session2":
                    print(f"  overleaf_session2: {c['value'][:40]}...")
                    break
            break

    # Show textOperation examples from history
    print("\nTextOperation format examples (from /latest/history):")
    for e in entries:
        if "/latest/history" in e["request"]["url"]:
            try:
                data = json.loads(e["response"]["content"]["text"])
                changes = data["chunk"]["history"]["changes"]
                print(f"  Total changes in history: {len(changes)}")
                for ch in changes[-3:]:
                    for op in ch.get("operations", []):
                        if "textOperation" in op:
                            top = op["textOperation"]
                            print(f"  textOp on '{op['pathname']}': {str(top)[:100]}")
            except Exception:
                pass
            break


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def cmd_analyze_har(args):
    analyze_har(args.har_file)


def cmd_info(args):
    """STEP 1+2: Authenticate and show project info."""
    s = OverleafSession(args.cookie)
    bootstrap = s.get_project_bootstrap(args.project_id)

    print(f"Project: {bootstrap.get('ol-projectName')}")
    print(f"Project ID: {bootstrap.get('ol-project_id')}")
    print(f"User: {bootstrap.get('ol-usersEmail')} ({bootstrap.get('ol-user_id')})")
    print(f"CSRF Token: {bootstrap.get('ol-csrfToken')}")
    print(f"OT Migration Stage: {bootstrap.get('ol-otMigrationStage')} (0=old socket.io OT)")
    print(f"Max doc length: {bootstrap.get('ol-maxDocLength')} bytes")

    user = bootstrap.get("ol-user", {})
    features = user.get("features", {})
    print(f"\nFeatures:")
    for k, v in features.items():
        print(f"  {k}: {v}")


def cmd_history(args):
    """STEP 4: Show project history and file structure."""
    s = OverleafSession(args.cookie)
    s.get_project_bootstrap(args.project_id)

    hist_api = HistoryAPI(s)
    data = hist_api.get_latest(args.project_id)
    changes = data["chunk"]["history"]["changes"]

    print(f"Total changes: {len(changes)}")

    sha_map = HistoryAPI.get_file_sha_map(data)
    versions = HistoryAPI.get_file_versions(data)

    print("\nFiles (from blob operations):")
    for path, sha in sorted(sha_map.items()):
        print(f"  {path} → blob:{sha}")

    print("\nLatest doc versions (for joinDoc):")
    for doc_id, v in versions.items():
        print(f"  {doc_id}: v{v}")

    print(f"\nLast 5 changes:")
    for ch in changes[-5:]:
        ts = ch["timestamp"]
        for op in ch.get("operations", []):
            pn = op.get("pathname", "?")
            if "textOperation" in op:
                top = op["textOperation"]
                inserts = sum(len(x) for x in top if isinstance(x, str))
                deletes = sum(abs(x) for x in top if isinstance(x, int) and x < 0)
                print(f"  {ts}  {pn}  +{inserts}/-{deletes} chars")
            elif "file" in op:
                print(f"  {ts}  {pn}  blob:{op['file'].get('hash','?')[:12]}...")


def cmd_read(args):
    """STEP 3+4: Read file content via blob API or history reconstruction."""
    s = OverleafSession(args.cookie)
    s.get_project_bootstrap(args.project_id)

    hist_api = HistoryAPI(s)
    data = hist_api.get_latest(args.project_id)
    sha_map = HistoryAPI.get_file_sha_map(data)

    # Find sha for the requested path
    target_path = args.file_path
    if target_path not in sha_map:
        # Try prefix match
        matches = [p for p in sha_map if p.endswith(target_path)]
        if matches:
            target_path = matches[0]
        else:
            print(f"File '{target_path}' not found. Available files:")
            for p in sorted(sha_map.keys()):
                print(f"  {p}")
            sys.exit(1)

    sha = sha_map[target_path]
    print(f"Reading {target_path} (blob:{sha[:12]}...)", file=sys.stderr)

    blob_api = BlobAPI(s)
    content = blob_api.read_text(args.project_id, sha)
    print(content)


def cmd_connect(args):
    """STEP 5: Connect via Socket.io and join the project."""
    s = OverleafSession(args.cookie)
    s.get_project_bootstrap(args.project_id)

    client = SocketIOClient(s, args.project_id)
    print(f"Connecting to project {args.project_id}...")
    client.connect()
    client.run_forever()

    print("Joining project...")
    project_data = client.join_project()
    print(f"Project data: {json.dumps(project_data, indent=2)[:1000]}")

    if args.doc_id:
        print(f"\nJoining doc {args.doc_id}...")
        lines, version = client.join_doc(args.doc_id)
        content = "\n".join(lines)
        print(f"Doc version: {version}")
        print(f"Content preview ({len(content)} chars):")
        print(content[:500])

    input("\nPress Enter to disconnect...")
    client.disconnect()


def cmd_edit(args):
    """STEP 6: Insert text into a document via Socket.io."""
    s = OverleafSession(args.cookie)
    s.get_project_bootstrap(args.project_id)

    client = SocketIOClient(s, args.project_id)
    client.connect()
    client.run_forever()

    # Join project first
    project_data = client.join_project()

    # Join the document to get current version
    lines, version = client.join_doc(args.doc_id)
    print(f"Doc joined at version {version}")

    # Apply insert
    success = client.apply_insert(args.doc_id, args.position, args.text, version)
    if success:
        print(f"Successfully inserted at position {args.position}")
    else:
        print("Insert failed")

    client.leave_doc(args.doc_id)
    client.disconnect()


def cmd_compile(args):
    """STEP 7: Trigger a LaTeX compile."""
    s = OverleafSession(args.cookie)
    s.get_project_bootstrap(args.project_id)

    # Need root doc id - get from history
    hist_api = HistoryAPI(s)
    data = hist_api.get_latest(args.project_id)
    versions = HistoryAPI.get_file_versions(data)

    # Find main.tex doc id
    root_doc_id = args.doc_id
    if not root_doc_id:
        changes = data["chunk"]["history"]["changes"]
        for ch in reversed(changes):
            for doc_id, info in ch.get("v2DocVersions", {}).items():
                if "main.tex" in info.get("pathname", ""):
                    root_doc_id = doc_id
                    break
            if root_doc_id:
                break

    if not root_doc_id:
        print("Could not determine root doc ID. Use --doc-id to specify.")
        sys.exit(1)

    print(f"Flushing pending changes...")
    compile_api = CompileAPI(s)
    compile_api.flush(args.project_id)

    print(f"Compiling (root doc: {root_doc_id})...")
    result = compile_api.compile(args.project_id, root_doc_id)

    status = result.get("status")
    print(f"Compile status: {status}")
    if status == "success":
        pdf = next((f for f in result.get("outputFiles", []) if f["type"] == "pdf"), None)
        if pdf:
            print(f"PDF: https://compiles.overleafusercontent.com{pdf['url']}")
    elif status in ("failure", "clsi-maintenance"):
        print("Compile failed. Check logs.")
        log = next((f for f in result.get("outputFiles", []) if f["type"] == "log"), None)
        if log:
            print(f"Log: https://compiles.overleafusercontent.com{log['url']}")


def main():
    parser = argparse.ArgumentParser(
        description="Overleaf Protocol CLI — reverse-engineered from HAR",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--cookie",
        help="overleaf_session2 cookie value (required for most commands)",
        default="",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # analyze-har
    p = sub.add_parser("analyze-har", help="Analyze a HAR file")
    p.add_argument("har_file")
    p.set_defaults(func=cmd_analyze_har)

    # info
    p = sub.add_parser("info", help="STEP 1+2: Show project bootstrap info")
    p.add_argument("project_id")
    p.set_defaults(func=cmd_info)

    # history
    p = sub.add_parser("history", help="STEP 4: Show project history + file list")
    p.add_argument("project_id")
    p.set_defaults(func=cmd_history)

    # read
    p = sub.add_parser("read", help="STEP 3: Read a file via blob API")
    p.add_argument("project_id")
    p.add_argument("file_path", help="e.g. main.tex or references.bib")
    p.set_defaults(func=cmd_read)

    # connect
    p = sub.add_parser("connect", help="STEP 5: Connect via Socket.io")
    p.add_argument("project_id")
    p.add_argument("--doc-id", help="Doc ID to join after connecting")
    p.set_defaults(func=cmd_connect)

    # edit
    p = sub.add_parser("edit", help="STEP 6: Insert text via Socket.io")
    p.add_argument("project_id")
    p.add_argument("doc_id", help="Document ID (from history command)")
    p.add_argument("position", type=int, help="Character position to insert at")
    p.add_argument("text", help="Text to insert")
    p.set_defaults(func=cmd_edit)

    # compile
    p = sub.add_parser("compile", help="STEP 7: Trigger LaTeX compile")
    p.add_argument("project_id")
    p.add_argument("--doc-id", help="Root doc ID (auto-detected if omitted)")
    p.set_defaults(func=cmd_compile)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
