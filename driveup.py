#!/usr/bin/env python3
"""driveup: Google Drive uploads that survive sleep, lid-close, wifi drops and restarts.

  driveup add <files or folders...> [--to "Drive/Folder"]   queue and start uploading
  driveup run                                              continue anything unfinished
  driveup status                                           what's done, partial, not started
  driveup clear                                            forget finished uploads

Progress is saved after every chunk in ~/.driveup/state.json, using Drive's
resumable upload protocol, so an interrupted file continues from the last chunk
Drive confirmed instead of starting over. Every finished file is checked
against Drive's MD5 checksum.
"""
import argparse
import fcntl
import hashlib
import json
import mimetypes
import os
import subprocess
import sys
import time
from pathlib import Path

import requests
from google.auth.exceptions import RefreshError, TransportError

HOME = Path.home() / ".driveup"
STATE = HOME / "state.json"
TOKEN = HOME / "token.json"
CLIENT = HOME / "credentials.json"
LOCK = HOME / "lock"

# drive.file only lets driveup see files and folders it created itself.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
API = "https://www.googleapis.com/drive/v3/files"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER = "application/vnd.google-apps.folder"
CHUNK = 16 * 1024 * 1024  # Drive requires multiples of 256 KB
SKIP = {".DS_Store", "Thumbs.db", "desktop.ini"}
FINISHED = {"verified", "mismatch"}
NET_ERRORS = (requests.ConnectionError, requests.Timeout, TransportError)

# driveup's own Google sign-in, shared by everyone who installs it. Google treats
# a desktop app's client secret as not confidential: it can't be kept secret in
# software people run on their own computers, and it only identifies the app.
# A credentials.json in ~/.driveup overrides this.
CLIENT_CONFIG = {
    "installed": {
        "client_id": "",
        "client_secret": "",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}

SETUP_HELP = f"""\
driveup needs a Google OAuth client before its first upload (one-time, ~5 minutes):

  1. Open https://console.cloud.google.com/ and create a project (any name).
  2. APIs & Services > Library > search "Google Drive API" > Enable.
  3. APIs & Services > OAuth consent screen > External, fill in app name + your
     email, and add yourself under "Test users".
  4. APIs & Services > Credentials > Create credentials > OAuth client ID >
     Application type "Desktop app" > Create > Download JSON.
  5. Move the downloaded file to:
       {CLIENT}

Then run `driveup login` (or just `driveup add ...`)."""


class UploadError(Exception):
    pass


class Retryable(Exception):
    pass


# ---------- state ----------

def load():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"folders": {}, "files": {}}


def save(state):
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    os.replace(tmp, STATE)


# ---------- output ----------

def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def line(text):
    sys.stdout.write("\r\033[K" + text)
    sys.stdout.flush()


def eta(seconds):
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


# ---------- auth ----------

def session():
    from google.auth.transport.requests import AuthorizedSession, Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES) if TOKEN.exists() else None
    if creds and creds.expired and creds.refresh_token:
        while True:
            try:
                creds.refresh(Request())
                break
            except RefreshError:
                creds = None
                break
            except TransportError:
                line("No internet connection, retrying in 10s")
                time.sleep(10)
    if not creds or not creds.valid:
        from google_auth_oauthlib.flow import InstalledAppFlow
        if CLIENT.exists():
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT), SCOPES)
        elif CLIENT_CONFIG["installed"]["client_id"]:
            flow = InstalledAppFlow.from_client_config(CLIENT_CONFIG, SCOPES)
        else:
            sys.exit(SETUP_HELP)
        print("Opening your browser to sign in to Google...")
        creds = flow.run_local_server(port=0)
    HOME.mkdir(parents=True, exist_ok=True)
    TOKEN.write_text(creds.to_json())
    os.chmod(TOKEN, 0o600)
    return AuthorizedSession(creds)


# ---------- drive calls ----------

def call(sess, method, url, **kw):
    """Send a small request, waiting out network drops and server hiccups."""
    delay = 2
    while True:
        try:
            r = sess.request(method, url, timeout=60, **kw)
            if r.status_code in (429, 500, 502, 503, 504) or (r.status_code == 403 and "ateLimit" in r.text):
                raise Retryable(f"Drive returned {r.status_code}")
            return r
        except (*NET_ERRORS, Retryable) as err:
            reason = err if isinstance(err, Retryable) else "connection lost"
            line(f"  {reason}, retrying in {delay}s...")
            time.sleep(delay)
            delay = min(delay * 2, 60)


def check(r):
    if not r.ok:
        raise UploadError(f"Drive returned {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else {}  # starting an upload replies with headers only


def esc(name):
    return name.replace("\\", "\\\\").replace("'", "\\'")


def folder_id(sess, state, remote):
    """Find or create each folder along a Drive path like 'Backups/Trip'."""
    parent, path = "root", ""
    for part in [p for p in remote.split("/") if p]:
        path = f"{path}/{part}" if path else part
        if path not in state["folders"]:
            q = f"name = '{esc(part)}' and mimeType = '{FOLDER}' and '{parent}' in parents and trashed = false"
            found = check(call(sess, "GET", API, params={"q": q, "fields": "files(id)"}))["files"]
            if found:
                state["folders"][path] = found[0]["id"]
            else:
                meta = {"name": part, "mimeType": FOLDER, "parents": [parent]}
                state["folders"][path] = check(call(sess, "POST", API, params={"fields": "id"}, json=meta))["id"]
            save(state)
        parent = state["folders"][path]
    return parent


def start_session(sess, state, e):
    mime = mimetypes.guess_type(e["name"])[0] or "application/octet-stream"
    for attempt in range(2):
        meta = {"name": e["name"], "parents": [folder_id(sess, state, e["remote_dir"])]}
        r = call(sess, "POST", UPLOAD, json=meta,
                 params={"uploadType": "resumable", "fields": "id,md5Checksum"},
                 headers={"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(e["size"])})
        if r.status_code == 404 and attempt == 0:
            state["folders"].clear()  # a cached folder was deleted in Drive; rebuild the path
            continue
        check(r)
        e["session"] = r.headers["Location"]
        e["sent"] = 0
        save(state)
        return


def probe(sess, e):
    """Ask Drive how much of this upload it has. Returns (offset, finished_json); offset None = expired."""
    r = call(sess, "PUT", e["session"], headers={"Content-Range": f"bytes */{e['size']}"})
    if r.status_code in (200, 201):
        return e["size"], r.json()
    if r.status_code == 308:
        return received(r), None
    if r.status_code in (404, 410):
        return None, None
    raise UploadError(f"Drive returned {r.status_code}: {r.text[:300]}")


def received(r):
    rng = r.headers.get("Range")  # "bytes=0-12345"
    return int(rng.rsplit("-", 1)[1]) + 1 if rng else 0


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while block := f.read(CHUNK):
            h.update(block)
    return h.hexdigest()


# ---------- uploading ----------

def upload_file(sess, state, e):
    path = Path(e["local"])
    try:
        st = path.stat()
    except FileNotFoundError:
        e["status"] = "missing"
        return
    if (st.st_size, int(st.st_mtime)) != (e["size"], e["mtime"]):
        # Changed on disk since it was queued: start this file over.
        e.update(size=st.st_size, mtime=int(st.st_mtime), session=None, sent=0)
    size = e["size"]
    e["status"] = "uploading"

    if size == 0:
        meta = {"name": e["name"], "parents": [folder_id(sess, state, e["remote_dir"])]}
        e["file_id"] = check(call(sess, "POST", API, params={"fields": "id"}, json=meta))["id"]
        e.update(status="verified", sent=0, session=None)
        return

    def resume_point():
        if e.get("session"):
            offset, done = probe(sess, e)
            if offset is not None:
                return offset, done
            line("  Drive expired the old upload session (they last ~1 week), starting this file over")
            print()
        start_session(sess, state, e)
        return 0, None

    offset, done = resume_point()
    if offset:
        print(f"  resuming at {human(offset)} of {human(size)}")
    began, began_at = time.time(), offset

    with path.open("rb") as f:
        while done is None:
            e["sent"] = offset
            save(state)
            elapsed = time.time() - began
            rate = (offset - began_at) / elapsed if elapsed > 1 else 0
            left = f", {eta((size - offset) / rate)} left" if rate else ""
            line(f"  {offset * 100 / size:5.1f}%  {human(offset)} of {human(size)}"
                 + (f"  {human(rate)}/s{left}" if rate else ""))

            f.seek(offset)
            chunk = f.read(CHUNK)
            end = offset + len(chunk) - 1
            try:
                r = sess.put(e["session"], data=chunk, timeout=300,
                             headers={"Content-Range": f"bytes {offset}-{end}/{size}"})
            except NET_ERRORS:
                line("  connection lost, waiting for it to come back...")
                time.sleep(3)
                offset, done = resume_point()
                continue
            if r.status_code in (200, 201):
                done = r.json()
            elif r.status_code == 308:
                offset = received(r)
            elif r.status_code in (404, 410):
                e["session"] = None
                offset, done = resume_point()
            elif r.status_code in (429, 500, 502, 503, 504) or (r.status_code == 403 and "ateLimit" in r.text):
                line(f"  Drive returned {r.status_code}, retrying...")
                time.sleep(5)
                offset, done = resume_point()
            else:
                raise UploadError(f"Drive returned {r.status_code}: {r.text[:300]}")

    e.update(file_id=done["id"], sent=size, session=None)
    save(state)
    remote_md5 = done.get("md5Checksum") or check(
        call(sess, "GET", f"{API}/{done['id']}", params={"fields": "md5Checksum"})).get("md5Checksum")
    line("  checking the upload against your file...")
    e["status"] = "verified" if remote_md5 == md5(path) else "mismatch"


# ---------- commands ----------

def cmd_add(args):
    state = load()
    queued, skipped, total = 0, 0, 0
    remote_root = args.to.strip("/")
    for raw in args.paths:
        p = Path(raw).expanduser().resolve()
        if p.is_dir():
            items = [(f, "/".join(filter(None, [remote_root, p.name, *f.relative_to(p).parent.parts])))
                     for f in sorted(p.rglob("*")) if f.is_file() and f.name not in SKIP]
        elif p.is_file():
            items = [(p, remote_root)]
        else:
            print(f"Not found, skipping: {raw}")
            continue
        for f, remote_dir in items:
            st = f.stat()
            key = f"{remote_dir}\x00{f}"
            old = state["files"].get(key)
            if old and old["status"] in FINISHED and (old["size"], old["mtime"]) == (st.st_size, int(st.st_mtime)):
                skipped += 1
                continue
            if old and (old["size"], old["mtime"]) == (st.st_size, int(st.st_mtime)):
                old["status"] = "pending" if old["status"] != "uploading" else old["status"]
            else:
                state["files"][key] = {"local": str(f), "name": f.name, "remote_dir": remote_dir,
                                       "size": st.st_size, "mtime": int(st.st_mtime),
                                       "status": "pending", "sent": 0, "session": None, "queued": time.time()}
            queued += 1
            total += st.st_size
    save(state)
    print(f"Queued {queued} file(s), {human(total)}, into Drive folder \"{remote_root or 'My Drive'}\".")
    if skipped:
        print(f"Skipped {skipped} already uploaded and unchanged.")
    if queued and not args.no_start:
        cmd_run(args)


def cmd_run(args):
    HOME.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("driveup is already uploading in another window. Check it with: driveup status")
        # Keep the Mac from idle-sleeping while this runs (closing the lid still sleeps it).
        caffeinate = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        try:
            if not upload_all(args):
                return
        finally:
            caffeinate.terminate()
    print()
    cmd_status(args)


def upload_all(args):
    state = load()
    todo = [e for e in state["files"].values() if e["status"] not in FINISHED]
    if not todo:
        print("Nothing left to upload.")
        return False
    sess = session()
    print(f"Uploading {len(todo)} file(s), {human(sum(e['size'] - e.get('sent', 0) for e in todo))} to go. "
          "Safe to close the lid or press Ctrl+C at any point.\n")
    for i, e in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {e['remote_dir']}/{e['name']}")
        try:
            upload_file(sess, state, e)
        except UploadError as err:
            e.update(status="failed", error=str(err))
        save(state)
        line({"verified": "  ✓ uploaded and verified",
              "mismatch": "  ✗ uploaded, but Drive's copy does not match your file. Upload it again by hand.",
              "missing": "  ✗ file no longer exists on your Mac, skipped",
              "failed": f"  ✗ failed: {e.get('error')}"}.get(e["status"], f"  {e['status']}"))
        print()
    return True


def cmd_status(args):
    state = load()
    files = list(state["files"].values())
    if not files:
        print("No uploads queued. Start one with: driveup add <file or folder> --to \"Folder\"")
        return
    try:
        with open(LOCK, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        running = False
    except BlockingIOError:
        running = True

    groups = {"done": [], "partial": [], "waiting": [], "problem": []}
    for e in files:
        if e["status"] == "verified":
            groups["done"].append(e)
        elif e["status"] in ("mismatch", "failed", "missing"):
            groups["problem"].append(e)
        elif e.get("sent"):
            groups["partial"].append(e)
        else:
            groups["waiting"].append(e)

    size = lambda es: human(sum(e["size"] for e in es))
    print(f"{len(files)} file(s), {size(files)}" + ("  (uploading right now)" if running else ""))
    print(f"  ✓ {len(groups['done'])} uploaded and verified ({size(groups['done'])})")
    if groups["partial"]:
        print(f"  ◐ {len(groups['partial'])} partly uploaded")
    if groups["waiting"]:
        print(f"  · {len(groups['waiting'])} not started ({size(groups['waiting'])})")
    if groups["problem"]:
        print(f"  ✗ {len(groups['problem'])} need attention")

    shown = groups["problem"] + groups["partial"] + groups["waiting"]
    if args.all:
        shown = groups["done"] + shown
    if shown:
        print()
    for e in shown[:200]:
        where = f"{e['remote_dir']}/{e['name']}"
        if e["status"] == "verified":
            print(f"  ✓ done   {where}")
        elif e["status"] in ("mismatch", "failed", "missing"):
            print(f"  ✗ {e['status']:6} {where}" + (f"\n             {e['error']}" if e.get("error") else ""))
        else:
            pct = e.get("sent", 0) * 100 / e["size"] if e["size"] else 0
            print(f"  {pct:4.0f}%   {where}  ({human(e.get('sent', 0))} of {human(e['size'])})")
    if len(shown) > 200:
        print(f"  ... and {len(shown) - 200} more")
    if (groups["partial"] or groups["waiting"] or any(e["status"] == "failed" for e in groups["problem"])) and not running:
        print("\nContinue with: driveup run")


def cmd_clear(args):
    state = load()
    before = len(state["files"])
    state["files"] = {k: e for k, e in state["files"].items() if e["status"] != "verified"}
    save(state)
    print(f"Forgot {before - len(state['files'])} finished upload(s). Files in Drive are untouched.")


# The standard macOS chooser (sidebar, Desktop, iCloud...), run through osascript.
# It picks files or folders, not both at once, so it asks which first.
PICKER = """
set choice to button returned of (display dialog "Upload files or folders to Google Drive?" ¬
  buttons {"Cancel", "Folders", "Files"} default button "Files" cancel button "Cancel" with title "driveup")
if choice is "Files" then
  set picked to choose file with prompt "Choose files to upload" default location (path to desktop) ¬
    with multiple selections allowed
else
  set picked to choose folder with prompt "Choose folders to upload" default location (path to desktop) ¬
    with multiple selections allowed
end if
set out to ""
repeat with f in picked
  set out to out & POSIX path of f & linefeed
end repeat
return out
"""


def cmd_pick(args):
    if sys.platform != "darwin":
        sys.exit('Picking files needs a Mac. Instead run: driveup add <file or folder> --to "Folder"')
    print("Choose what to upload in the window that just opened (check the Dock if you don't see it)...")
    out = subprocess.run(["osascript", "-e", PICKER], capture_output=True, text=True)
    if out.returncode and "-128" not in out.stderr:  # -128 is the Cancel button
        sys.exit(f"The file chooser didn't open: {out.stderr.strip()}")
    args.paths = [p for p in out.stdout.splitlines() if p]
    if not args.paths:
        sys.exit("Nothing chosen.")
    args.no_start = False
    cmd_add(args)


def cmd_login(args):
    session()
    print("Signed in. driveup can upload now.")


def main():
    ap = argparse.ArgumentParser(prog="driveup", description="Resumable Google Drive uploads.")
    sub = ap.add_subparsers(dest="cmd")
    k = sub.add_parser("pick", help="choose files/folders in a window and upload them (same as plain `driveup`)")
    k.add_argument("--to", default="driveup", help='Drive folder path (default: driveup)')
    k.add_argument("--all", action="store_true", help=argparse.SUPPRESS)
    a = sub.add_parser("add", help="queue files/folders and start uploading")
    a.add_argument("paths", nargs="+")
    a.add_argument("--to", default="driveup", help='Drive folder path, e.g. "Backups/2026" (default: driveup)')
    a.add_argument("--no-start", action="store_true", help="only queue, don't upload yet")
    a.add_argument("--all", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("run", help="continue unfinished uploads").add_argument("--all", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("status", help="show what's uploaded").add_argument("--all", action="store_true", help="list finished files too")
    sub.add_parser("clear", help="forget finished uploads")
    sub.add_parser("login", help="sign in to Google")
    args = ap.parse_args()
    if not args.cmd:  # plain `driveup` opens the file picker
        args = ap.parse_args(["pick"])
    try:
        {"pick": cmd_pick, "add": cmd_add, "run": cmd_run, "status": cmd_status, "clear": cmd_clear, "login": cmd_login}[args.cmd](args)
    except KeyboardInterrupt:
        print("\n\nPaused. Progress is saved. Continue anytime with: driveup run")


if __name__ == "__main__":
    main()
