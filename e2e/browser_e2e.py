#!/usr/bin/env python3
"""
Real-browser end-to-end tests: a Chromium tab editing in the Overleaf editor
while the local file is edited at the same time, against a self-hosted
Overleaf (Community Edition).  Never point this at overleaf.com.

    OVERLEAF_URL=http://localhost:8080 OVERLEAF_EMAIL=you@example.org \
    OVERLEAF_PASSWORD_FILE=pw.txt  python3 -I e2e/browser_e2e.py [BR1 BR3 ...]

Needs: selenium, chromium + a matching chromedriver, the `overleaf-sync`
command on PATH (pip install -e .).  Every scenario ends by comparing three
views of the document: the browser's CodeMirror buffer, the local file, and
the server's copy (read through a second real-time connection).
"""
import html, json, os, random, re, shutil, subprocess, sys, tempfile, threading, time, traceback
from pathlib import Path

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

URL = os.environ.get("OVERLEAF_URL", "http://localhost:8080").rstrip("/")
EMAIL = os.environ["OVERLEAF_EMAIL"]
PASSWORD = Path(os.environ["OVERLEAF_PASSWORD_FILE"]).read_text().strip()
CHROMIUM = os.environ.get("CHROMIUM", "/usr/bin/chromium")
CHROMEDRIVER = os.environ.get("CHROMEDRIVER", "/usr/bin/chromedriver")
SYNC_CMD = os.environ.get("OVERLEAF_SYNC", "overleaf-sync")
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ["OVERLEAF_URL"] = URL
import overleaf_cli                                        # noqa: E402
from overleaf_sync import (TrackingSocketIOClient, collect_docs_from_project,  # noqa: E402
                           compute_ot_ops, ops_to_wire)

RUN = str(int(time.time()))
ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name, extra, flush=True)

def wait(cond, t=20, step=0.2):
    end = time.time() + t
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(step)
    try:
        return bool(cond())
    except Exception:
        return False

# --------------------------------------------------------------------- setup
def csrf_of(session, path):
    t = session.get(URL + path).text
    return html.unescape(re.search(r'name="ol-csrfToken"[^>]*content="([^"]*)"', t).group(1))

def create_project(name):
    s = requests.Session()
    tok = csrf_of(s, "/login")
    s.post(URL + "/login", json={"email": EMAIL, "password": PASSWORD, "_csrf": tok},
           headers={"X-Csrf-Token": tok}).raise_for_status()
    tok = csrf_of(s, "/project")
    return s.post(URL + "/project/new", json={"projectName": name, "template": "example"},
                  headers={"X-Csrf-Token": tok}).json()["project_id"]

class Server:
    """Ground truth: the server's copy of a doc, via a separate real-time connection."""
    def __init__(self, pid, cookie):
        sess = overleaf_cli.OverleafSession(cookie); sess.get_project_bootstrap(pid)
        self.ws = TrackingSocketIOClient(sess, pid)
        self.ws.on("otUpdateApplied", lambda a: None)
        self.ws.connect(); self.ws.run_forever()
        self.docs = {v: k for k, v in collect_docs_from_project(self.ws.join_project()).items()}
    def text(self, path="main.tex"):
        return "\n".join(self.ws.join_doc(self.docs[path])[0])
    def close(self):
        self.ws.disconnect()

class Browser:
    def __init__(self, pid):
        o = Options(); o.binary_location = CHROMIUM
        for a in ("--headless=new", "--no-sandbox", "--disable-gpu", "--window-size=1500,1100"):
            o.add_argument(a)
        self.d = webdriver.Chrome(service=Service(CHROMEDRIVER), options=o)
        self.pid = pid
        self.d.get(URL + "/login")
        WebDriverWait(self.d, 30).until(EC.presence_of_element_located((By.NAME, "email")))
        self.d.find_element(By.NAME, "email").send_keys(EMAIL)
        self.d.find_element(By.NAME, "password").send_keys(PASSWORD)
        self.d.find_element(By.CSS_SELECTOR, "button[type=submit]").click()
        WebDriverWait(self.d, 30).until(lambda x: "/project" in x.current_url)
        self.open()
    def open(self):
        self.d.get(f"{URL}/project/{self.pid}")
        WebDriverWait(self.d, 90).until(EC.presence_of_element_located((By.CSS_SELECTOR, ".cm-content")))
        wait(lambda: self.text() != "", 30)
    def js(self, script, *args):
        return self.d.execute_script(
            "const view=document.querySelector('.cm-content').cmView.view;" + script, *args)
    def text(self):
        return self.js("return view.state.doc.toString()")
    def line_starts(self):
        return self.js("const s=[];for(let i=1;i<=view.state.doc.lines;i++)s.push(view.state.doc.line(i).from);return s")
    def type_at(self, pos, text):
        """Real keystrokes at document offset pos."""
        self.js("view.focus(); view.dispatch({selection:{anchor:arguments[0]},scrollIntoView:true})", pos)
        ActionChains(self.d).send_keys(text).perform()
    def insert_at(self, pos, text):
        """One atomic CodeMirror transaction at pos (what a paste does)."""
        self.js("view.dispatch({changes:{from:arguments[0],insert:arguments[1]}})", pos, text)
    def delete_range(self, a, b):
        self.js("view.dispatch({changes:{from:arguments[0],to:arguments[1]}})", a, b)
    def undo(self):
        self.js("view.focus()")
        ActionChains(self.d).key_down(Keys.CONTROL).send_keys("z").key_up(Keys.CONTROL).perform()
    def shot(self, name):
        self.d.save_screenshot(f"/tmp/browser_{name}.png")
    def quit(self):
        self.d.quit()

class Sync:
    def __init__(self, pid, outdir, logfile):
        self.log = open(logfile, "w")
        pwf = os.environ["OVERLEAF_PASSWORD_FILE"]
        self.p = subprocess.Popen(
            [SYNC_CMD, pid, "--url", URL, "--login-email", EMAIL, "--password-file", pwf,
             "--output-dir", str(outdir)], stdout=self.log, stderr=self.log, env=dict(os.environ, PYTHONUNBUFFERED="1"))
        self.logfile = logfile
        assert wait(lambda: "Bidirectional sync active" in Path(logfile).read_text(), 60), Path(logfile).read_text()[-500:]
        time.sleep(4)   # settle
    def text(self):
        return Path(self.logfile).read_text()
    def stop(self):
        self.p.terminate()
        try: self.p.wait(15)
        except subprocess.TimeoutExpired: self.p.kill()

# --------------------------------------------------------------- local edits
def local_insert_line(path, tok, r, style):
    """Insert tok on its own line at a random line start, the way an agent ('inplace') or an editor ('atomic') saves."""
    cur = path.read_text()
    starts = [0] + [i + 1 for i, c in enumerate(cur) if c == "\n"]
    pos = r.choice(starts)
    new = cur[:pos] + tok + "\n" + cur[pos:]
    if style == "inplace":
        with open(path, "w") as f:
            f.write(new)
    else:
        tmp = path.with_name(".swp-" + path.name); tmp.write_text(new); os.replace(tmp, path)

# ---------------------------------------------------------------- scenarios
class Ctx:
    pass

def converge(ctx, t=40):
    """browser == local == server."""
    def same():
        a, b, c = ctx.browser.text(), ctx.local.read_text(), ctx.server.text()
        return a == b == c
    good = wait(same, t)
    a, b, c = ctx.browser.text(), ctx.local.read_text(), ctx.server.text()
    return good, (len(a), len(b), len(c))

def all_once(text, toks):
    return [t for t in toks if text.count(t) != 1]

def BR1(ctx):
    """web -> file: tokens typed with real keystrokes show up in the local file and on the server."""
    toks = []
    for i in range(5):
        tok = f"<<BR1-{RUN}-{i}>>"; toks.append(tok)
        ctx.browser.type_at(ctx.browser.line_starts()[3 + 2 * i], tok + "\n"); time.sleep(0.4)
    check("BR1 typed text reaches the local file", wait(lambda: all(t in ctx.local.read_text() for t in toks), 20))
    good, n = converge(ctx); check("BR1 browser==file==server", good, n)
    check("BR1 each token exactly once", not all_once(ctx.local.read_text(), toks))

def BR2(ctx):
    """file -> web: agent-style and editor-style local writes appear in the browser."""
    toks = []
    for i, style in enumerate(["inplace", "atomic", "inplace", "atomic"]):
        tok = f"<<BR2-{RUN}-{i}-{style}>>"; toks.append(tok)
        local_insert_line(ctx.local, tok, random.Random(i), style); time.sleep(1.5)
    check("BR2 local writes appear in the browser", wait(lambda: all(t in ctx.browser.text() for t in toks), 30))
    good, n = converge(ctx); check("BR2 browser==file==server", good, n)
    check("BR2 each token exactly once", not all_once(ctx.server.text(), toks))

def BR3(ctx):
    """Simultaneous edits for 45 s: real typing in the browser + local writes (both save styles)."""
    r_loc, r_br = random.Random(3), random.Random(4)
    btoks, ltoks = [], []; end = time.time() + 45; err = []
    def browser_side():
        n = 0
        while time.time() < end:
            n += 1; tok = f"<<BR3b-{RUN}-{n}>>"
            try:
                starts = ctx.browser.line_starts()
                ctx.browser.type_at(r_br.choice(starts[: len(starts) // 2]), tok + "\n"); btoks.append(tok)
            except Exception as e:
                err.append(repr(e)[:120])
            time.sleep(r_br.uniform(0.2, 1.2))
    def local_side():
        n = 0
        while time.time() < end:
            n += 1; tok = f"<<BR3l-{RUN}-{n}>>"
            local_insert_line(ctx.local, tok, r_loc, r_loc.choice(["inplace", "atomic"])); ltoks.append(tok)
            time.sleep(r_loc.uniform(0.3, 2.0))
    ts = [threading.Thread(target=browser_side), threading.Thread(target=local_side)]
    [t.start() for t in ts]; [t.join() for t in ts]
    good, n = converge(ctx, 60); check("BR3 browser==file==server after simultaneous editing", good, n)
    final = ctx.server.text()
    missing = [t for t in btoks + ltoks if t not in final]
    dup = [t for t in btoks + ltoks if final.count(t) > 1]
    check("BR3 no edit lost", not missing, f"{len(btoks)} typed in browser, {len(ltoks)} written locally, missing={missing[:4]} {err[:2]}")
    check("BR3 no edit duplicated", not dup, str(dup[:4]))

def BR4(ctx):
    """Both sides insert at the very same position, repeatedly (atomic browser transactions)."""
    toks = []
    for i in range(12):
        pos = ctx.browser.line_starts()[10]
        btok = f"<<BR4b-{RUN}-{i}>>\n"; ltok = f"<<BR4l-{RUN}-{i}>>"
        cur = ctx.local.read_text(); lp = cur.split("\n")
        starts = [0] + [k + 1 for k, c in enumerate(cur) if c == "\n"]
        t1 = threading.Thread(target=lambda: ctx.browser.insert_at(pos, btok))
        t2 = threading.Thread(target=lambda: ctx.local.write_text(cur[:starts[10]] + ltok + "\n" + cur[starts[10]:]) )
        t1.start(); t2.start(); t1.join(); t2.join(); toks += [btok.strip(), ltok]
        time.sleep(2.5)
        wait(lambda: ctx.browser.text() == ctx.local.read_text() == ctx.server.text(), 25)
    good, n = converge(ctx, 40); check("BR4 same-position inserts converge", good, n)
    check("BR4 each token exactly once and intact", not all_once(ctx.server.text(), toks))

def BR5(ctx):
    """A 200 KB paste in the browser while the local file is edited."""
    big = "".join(f"%% BR5 paste line {i} " + "y" * 60 + "\n" for i in range(2500))
    ltok = f"<<BR5l-{RUN}>>"
    t = threading.Thread(target=lambda: local_insert_line(ctx.local, ltok, random.Random(5), "atomic"))
    t.start(); ctx.browser.insert_at(ctx.browser.line_starts()[20], big); t.join()
    good, n = converge(ctx, 90); check("BR5 paste + local edit converge", good, n)
    f = ctx.server.text()
    check("BR5 paste intact and local edit kept", f.count("BR5 paste line") == 2500 and f.count(ltok) == 1)
    # and remove it again
    s = ctx.browser.text(); a = s.index("%% BR5 paste line 0 "); b = s.index("%% BR5 paste line 2499 ") + 80
    ctx.browser.delete_range(a, b)
    converge(ctx, 60)

def BR6(ctx):
    """Deletions on both sides at different places, plus a replace."""
    pre = [f"<<BR6-{RUN}-{i}>>" for i in range(6)]
    for i, tok in enumerate(pre):
        ctx.browser.insert_at(ctx.browser.line_starts()[5 + i], tok + "\n")
    converge(ctx, 30)
    # delete tokens 0,1 in the browser and 4,5 in the local file, replace 2 in the browser meanwhile
    b = ctx.browser.text()
    def browser_ops():
        for tok in pre[:2]:
            s = ctx.browser.text(); i = s.index(tok); ctx.browser.delete_range(i, i + len(tok) + 1); time.sleep(0.5)
        s = ctx.browser.text(); i = s.index(pre[2]); ctx.browser.delete_range(i, i + len(pre[2])); ctx.browser.insert_at(i, f"<<BR6-{RUN}-replaced>>")
    def local_ops():
        time.sleep(0.3); cur = ctx.local.read_text()
        for tok in pre[4:]:
            cur = cur.replace(tok + "\n", "")
        tmp = ctx.local.with_name(".swp"); tmp.write_text(cur); os.replace(tmp, ctx.local)
    ts = [threading.Thread(target=browser_ops), threading.Thread(target=local_ops)]
    [t.start() for t in ts]; [t.join() for t in ts]
    good, n = converge(ctx, 60); check("BR6 deletions on both sides converge", good, n)
    f = ctx.server.text()
    check("BR6 deleted tokens are gone, kept ones remain",
          all(t not in f for t in (pre[0], pre[1], pre[2], pre[4], pre[5])) and pre[3] in f and f"<<BR6-{RUN}-replaced>>" in f)

def BR7(ctx):
    """Browser reload in the middle of local edits: it reloads the same text."""
    ltoks = []
    def local_side():
        for i in range(8):
            tok = f"<<BR7-{RUN}-{i}>>"; local_insert_line(ctx.local, tok, random.Random(70 + i), "inplace"); ltoks.append(tok); time.sleep(0.8)
    t = threading.Thread(target=local_side); t.start(); time.sleep(2.5)
    ctx.browser.open()                       # reload the tab mid-way
    t.join()
    good, n = converge(ctx, 60); check("BR7 reload mid-edit: browser==file==server", good, n)
    check("BR7 all local edits present", not [x for x in ltoks if x not in ctx.browser.text()])

def BR8(ctx):
    """Undo in the browser after remote activity must not resurrect or delete other people's text."""
    tok = f"<<BR8b-{RUN}>>"
    ctx.browser.type_at(ctx.browser.line_starts()[8], tok + "\n"); time.sleep(1)
    ltok = f"<<BR8l-{RUN}>>"; local_insert_line(ctx.local, ltok, random.Random(8), "atomic")
    wait(lambda: ltok in ctx.browser.text(), 20)
    ctx.browser.undo(); time.sleep(2)
    good, n = converge(ctx, 40); check("BR8 after browser undo all three views agree", good, n)
    f = ctx.server.text()
    check("BR8 undo removed only the browser's own typing", f.count(ltok) == 1)

def BR9(ctx):
    """High rate for 30 s: a keystroke or transaction every ~100 ms in the browser and a local save every ~150 ms."""
    r_loc, r_br = random.Random(9), random.Random(10)
    btoks, ltoks = [], []; end = time.time() + 30
    def browser_side():
        n = 0
        while time.time() < end:
            n += 1; tok = f"<<BR9b-{RUN}-{n}>>"
            starts = ctx.browser.line_starts()
            if n % 2:
                ctx.browser.type_at(r_br.choice(starts[: len(starts) // 2]), tok + "\n")
            else:
                ctx.browser.insert_at(r_br.choice(starts[: len(starts) // 2]), tok + "\n")
            btoks.append(tok); time.sleep(r_br.uniform(0.02, 0.15))
    def local_side():
        n = 0
        while time.time() < end:
            n += 1; tok = f"<<BR9l-{RUN}-{n}>>"
            local_insert_line(ctx.local, tok, r_loc, r_loc.choice(["inplace", "atomic"])); ltoks.append(tok)
            time.sleep(r_loc.uniform(0.05, 0.25))
    ts = [threading.Thread(target=browser_side), threading.Thread(target=local_side)]
    [t.start() for t in ts]; [t.join() for t in ts]
    good, n = converge(ctx, 90); check("BR9 browser==file==server after high-rate editing", good, n)
    final = ctx.server.text()
    lost = [t for t in btoks + ltoks if t not in final]
    dup = [t for t in btoks + ltoks if final.count(t) > 1]
    record = "".join(p.read_text() for p in (ctx.local.parent / ".overleaf-sync-backup").rglob("deleted.jsonl"))
    check("BR9 no edit duplicated", not dup)
    # A whole-file read-modify-write that overlaps a merge can overwrite a coauthor's fresh text (KNOWN LIMIT,
    # see README).  The contract is that this is never silent: the overwritten text is in the deletion record.
    check("BR9 every overwritten edit is recoverable", all(t in record for t in lost),
          f"{len(btoks)} browser + {len(ltoks)} local edits, overwritten={len(lost)}")

def BR10(ctx):
    """KNOWN LIMIT, measured: a tool that reads the whole file, 'thinks' 40 ms and writes it back can overwrite
    text the sync merged in that instant.  Checked: views converge, nothing is duplicated, and every lost edit is
    recoverable from the deletion record.  The loss rate is reported, not gated."""
    r_loc, r_br = random.Random(11), random.Random(12)
    btoks, ltoks = [], []; end = time.time() + 30
    def stale_writer():
        n = 0
        while time.time() < end:
            n += 1; tok = f"<<BR10l-{RUN}-{n}>>"
            cur = ctx.local.read_text(); time.sleep(0.04)          # the window in which a merge can land
            starts = [0] + [i + 1 for i, c in enumerate(cur) if c == "\n"]
            pos = r_loc.choice(starts); new = cur[:pos] + tok + "\n" + cur[pos:]
            if n % 2:
                with open(ctx.local, "w") as f: f.write(new)
            else:
                tmp = ctx.local.with_name(".swp"); tmp.write_text(new); os.replace(tmp, ctx.local)
            ltoks.append(tok); time.sleep(r_loc.uniform(0.05, 0.2))
    def browser_side():
        n = 0
        while time.time() < end:
            n += 1; tok = f"<<BR10b-{RUN}-{n}>>"
            starts = ctx.browser.line_starts()
            ctx.browser.insert_at(r_br.choice(starts[: len(starts) // 2]), tok + "\n"); btoks.append(tok)
            time.sleep(r_br.uniform(0.02, 0.1))
    ts = [threading.Thread(target=stale_writer), threading.Thread(target=browser_side)]
    [t.start() for t in ts]; [t.join() for t in ts]
    good, n = converge(ctx, 90); check("BR10 browser==file==server", good, n)
    final = ctx.server.text()
    lost = [t for t in btoks + ltoks if t not in final]
    dups = [t for t in btoks + ltoks if final.count(t) > 1]
    check("BR10 no edit duplicated", not dups, str(dups[:3]))
    snaps = "".join(p.read_text() for p in (ctx.local.parent / ".overleaf-sync-backup").rglob("deleted.jsonl"))
    unrecoverable = [t for t in lost if t not in snaps]
    print("   unrecoverable:", [t.split("-", 2)[2] for t in unrecoverable][:12], "work dir:", ctx.local.parent.parent)
    check("BR10 every lost edit is recoverable from the deletion record", not unrecoverable,
          f"{len(lost)} of {len(btoks) + len(ltoks)} edits overwritten by stale saves; unrecoverable={len(unrecoverable)}")

SCENARIOS = {f.__name__: f for f in (BR1, BR2, BR3, BR4, BR5, BR6, BR7, BR8, BR9, BR10)}

def main():
    names = sys.argv[1:] or list(SCENARIOS)
    work = Path(tempfile.mkdtemp(prefix="olsync-browser-"))
    pid = create_project(f"browser-e2e-{RUN}")
    print("project", pid, "work dir", work, flush=True)
    outdir = work / "out"; ctx = Ctx()
    cookie = overleaf_cli.login_with_password(EMAIL, PASSWORD)
    ctx.sync = Sync(pid, outdir, work / "sync.log")
    ctx.server = Server(pid, cookie)
    ctx.browser = Browser(pid)
    ctx.local = outdir / "main.tex"
    try:
        assert wait(lambda: ctx.browser.text() == ctx.local.read_text() == ctx.server.text(), 30), "initial views differ"
        for n in names:
            print(f"=== {n}: {SCENARIOS[n].__doc__.strip()}", flush=True)
            try:
                SCENARIOS[n](ctx)
            except Exception:
                global ok_all; ok_all = False
                print("FAIL (exception)"); traceback.print_exc()
                try: ctx.browser.shot(n)
                except Exception: pass
        check("sync process still alive", ctx.sync.p.poll() is None)
    finally:
        ctx.browser.quit(); ctx.server.close(); ctx.sync.stop()
        print("---- sync log tail ----"); print(ctx.sync.text()[-1500:])
    print("ALL OK" if ok_all else "SOME FAILED")
    sys.exit(0 if ok_all else 1)

if __name__ == "__main__":
    main()
