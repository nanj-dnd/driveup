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
import plistlib
import random
import shutil
import subprocess
import sys
import threading
import time
import uuid
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# macOS's own Python (what `xcode-select --install` gives you) is 3.9 built against LibreSSL.
# urllib3 and google-auth each print a warning about that on every import; neither is something
# a driveup user can act on, and uploads work fine, so keep them off the screen.
warnings.filterwarnings("ignore", message=r"urllib3 v2 only supports OpenSSL")
warnings.filterwarnings("ignore", message=r"You are using a Python version .* past its end of life", category=FutureWarning)

import requests
from google.auth.exceptions import RefreshError, TransportError

HOME = Path.home() / ".driveup"
STATE = HOME / "state.json"
TOKEN = HOME / "token.json"
CLIENT = HOME / "credentials.json"
LOCK = HOME / "lock"

# drive.file only lets driveup see files and folders it created itself.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
BASE = os.environ.get("DRIVEUP_BASE", "https://www.googleapis.com")  # override only for testing
API = f"{BASE}/drive/v3/files"
UPLOAD = f"{BASE}/upload/drive/v3/files"
FOLDER = "application/vnd.google-apps.folder"
CHUNK = 64 * 1024 * 1024  # bigger pieces, fewer round trips; Drive requires multiples of 256 KB
ONE_SHOT = 5 * 1024 * 1024  # up to this size, send metadata and bytes in one request
SMALL = CHUNK  # files that fit in one piece upload several at a time
WORKERS = 8
SKIP = {".DS_Store", "Thumbs.db", "desktop.ini", ".localized", "Icon\r"}
# Drive-wide problems: no point trying the next file.
STOPPERS = {"storageQuotaExceeded": "Your Google Drive is full.",
            "uploadLimitExceeded": "Google's daily upload limit (about 750 GB) is reached. Run `driveup run` again tomorrow.",
            "activeItemCreationLimitExceeded": "Drive's limit on the number of files is reached."}
FINISHED = {"verified", "mismatch"}
NET_ERRORS = (requests.ConnectionError, requests.Timeout, TransportError)

# driveup's own Google sign-in, shared by everyone who installs it. Google treats
# a desktop app's client secret as not confidential: it can't be kept secret in
# software people run on their own computers, and it only identifies the app.
# A credentials.json in ~/.driveup overrides this.
CLIENT_CONFIG = {
    "installed": {
        "client_id": "407233369004-cb0bbks9vou75csn397ufuomcqrvif58.apps.googleusercontent.com",
        "client_secret": "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",
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


class Stop(Exception):
    """Drive won't take more uploads right now (full, daily limit). Stops the whole run."""


# ---------- state ----------

def load():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"folders": {}, "files": {}}


LOCK_STATE = threading.RLock()  # guards writing state.json and creating folders
LISTED = {}  # Drive folder id -> {name: [files]}, what was already there, fetched once per folder
_saved_at = 0.0


def save(state, force=True):
    """Write progress to disk. With force=False, at most every few seconds:
    with tens of thousands of files, the state file is big."""
    global _saved_at
    with LOCK_STATE:
        if not force and time.time() - _saved_at < 5:
            return
        HOME.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, separators=(",", ":")))
        os.replace(tmp, STATE)
        _saved_at = time.time()


# ---------- output ----------

def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


QUIET = threading.Event()  # set while several files upload at once: no live progress line


def line(text):
    if QUIET.is_set():
        return
    sys.stdout.write("\r\033[K" + text)
    sys.stdout.flush()


def alert(message):
    """Scheduled backups run where nobody sees the log, so their failures also show as a
    macOS notification. Does nothing for runs started by hand."""
    if os.environ.get("DRIVEUP_SCHEDULED") and sys.platform == "darwin":
        subprocess.run(["osascript", "-e", "on run argv", "-e",
                        'display notification (item 1 of argv) with title "driveup backup didn\'t finish"',
                        "-e", "end run", message], capture_output=True)


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
        if os.environ.get("DRIVEUP_SCHEDULED"):
            alert("Google sign-in expired. Run: driveup login")
            sys.exit("Google sign-in expired, so this scheduled backup can't run. Run: driveup login")
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
            time.sleep(delay + random.random() * delay)
            delay = min(delay * 2, 60)


def check(r):
    if not r.ok:
        for reason, message in STOPPERS.items():
            if reason in r.text:
                raise Stop(message)
        raise UploadError(f"Drive returned {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else {}  # starting an upload replies with headers only


def esc(name):
    return name.replace("\\", "\\\\").replace("'", "\\'")


def folder_id(sess, state, remote):
    """Find or create each folder along a Drive path like 'Backups/Trip'."""
    with LOCK_STATE:
        return _folder_id(sess, state, remote)


def _folder_id(sess, state, remote):
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
                LISTED[state["folders"][path]] = {}  # brand new, so nothing to compare against
            save(state, force=False)
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
        save(state, force=e["size"] > SMALL)
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

def find_copy(sess, parent, name, digest):
    """An identical file already in this Drive folder (e.g. sent just before an interruption)."""
    q = f"name = '{esc(name)}' and '{parent}' in parents and trashed = false"
    found = check(call(sess, "GET", API, params={"q": q, "fields": "files(id,md5Checksum)"}))["files"]
    return next((f for f in found if f.get("md5Checksum") == digest), None)


def listing(sess, parent):
    """What's in a Drive folder (that driveup can see), as {name: [files]}. Fetched once per run."""
    with LOCK_STATE:
        if parent not in LISTED:
            found, token = {}, None
            while True:
                params = {"q": f"'{parent}' in parents and trashed = false", "pageSize": 1000,
                          "fields": "nextPageToken,files(id,name,size,md5Checksum)"}
                if token:
                    params["pageToken"] = token
                page = check(call(sess, "GET", API, params=params))
                for f in page.get("files", []):
                    found.setdefault(f["name"], []).append(f)
                if not (token := page.get("nextPageToken")):
                    break
            LISTED[parent] = found
        return LISTED[parent]


def lookup_folder(sess, state, remote):
    """Like folder_id, but only finds: returns None instead of creating anything."""
    remote = remote.strip("/")
    if not remote:
        return "root"
    if remote in state["folders"]:
        return state["folders"][remote]
    parent = "root"
    for part in remote.split("/"):
        q = f"name = '{esc(part)}' and mimeType = '{FOLDER}' and '{parent}' in parents and trashed = false"
        found = check(call(sess, "GET", API, params={"q": q, "fields": "files(id)"}))["files"]
        if not found:
            return None
        parent = found[0]["id"]
    return parent


def already_there(sess, state, e, path):
    """An identical file already in the Drive folder, from an earlier run driveup no longer
    remembers. One listing per folder; local checksums only for same-name, same-size files."""
    parent = folder_id(sess, state, e["remote_dir"])
    same = [f for f in listing(sess, parent).get(e["name"], []) if int(f.get("size", -1)) == e["size"]]
    if same:
        digest = md5(path)
        return next((f for f in same if f.get("md5Checksum") == digest), None)


def one_shot(sess, state, e, path):
    """Small files: metadata and bytes in a single request, checked against the bytes sent."""
    data = path.read_bytes()
    digest = hashlib.md5(data).hexdigest()
    mime = mimetypes.guess_type(e["name"])[0] or "application/octet-stream"
    delay = 2
    for attempt in range(8):
        parent = folder_id(sess, state, e["remote_dir"])
        # Only after an interruption can it already be there; checking every file would double the requests.
        if (e["interrupted"] or attempt) and (copy := find_copy(sess, parent, e["name"], digest)):
            e.update(file_id=copy["id"], sent=e["size"], status="verified")
            return
        b = uuid.uuid4().hex
        meta = json.dumps({"name": e["name"], "parents": [parent]})
        body = (f"--{b}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                f"--{b}\r\nContent-Type: {mime}\r\n\r\n").encode() + data + f"\r\n--{b}--".encode()
        try:
            r = sess.post(UPLOAD, params={"uploadType": "multipart", "fields": "id,md5Checksum"}, data=body,
                          headers={"Content-Type": f"multipart/related; boundary={b}"}, timeout=120)
        except NET_ERRORS:
            r = None  # it may or may not have arrived; the next attempt looks before sending again
        if r is not None and r.status_code == 404 and attempt == 0:
            with LOCK_STATE:
                state["folders"].clear()  # a cached folder was deleted in Drive; rebuild the path
            continue
        if r is None or r.status_code in (429, 500, 502, 503, 504) or (r.status_code == 403 and "ateLimit" in r.text):
            time.sleep(delay + random.random() * delay)
            delay = min(delay * 2, 60)
            continue
        done = check(r)
        e.update(file_id=done["id"], sent=e["size"], status="verified" if done.get("md5Checksum") == digest else "mismatch")
        return
    raise UploadError("Drive kept failing, try again later")


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
    if not e.get("session") and (copy := already_there(sess, state, e, path)):
        e.update(file_id=copy["id"], sent=size, status="verified", note="already in Drive")
        return
    if 0 < size <= ONE_SHOT:
        return one_shot(sess, state, e, path)

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
            save(state, force=False)
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
    save(state, force=size > SMALL)
    remote_md5 = done.get("md5Checksum") or check(
        call(sess, "GET", f"{API}/{done['id']}", params={"fields": "md5Checksum"})).get("md5Checksum")
    line("  checking the upload against your file...")
    e["status"] = "verified" if remote_md5 == md5(path) else "mismatch"


# ---------- commands ----------

def walk(paths, remote_root):
    """Each local file under paths, with the Drive folder it belongs in."""
    for raw in paths:
        p = Path(raw).expanduser().resolve()
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and f.name not in SKIP:
                    yield f, "/".join(filter(None, [remote_root, p.name, *f.relative_to(p).parent.parts]))
        elif p.is_file():
            yield p, remote_root
        else:
            print(f"Not found, skipping: {raw}")


def cmd_add(args):
    state = load()
    queued, skipped, total = 0, 0, 0
    remote_root = args.to.strip("/")
    for f, remote_dir in walk(args.paths, remote_root):
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
        if not os.environ.get("DRIVEUP_SCHEDULED"):
            print(f'\nShare it with someone: driveup link "{remote_root or args.paths[0]}"')


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
            if not (todo := upload_all(args)):
                return
        finally:
            caffeinate.terminate()
    if bad := [e for e in todo if e["status"] in ("failed", "mismatch")]:
        alert(f"{len(bad)} file(s) didn't upload. See: driveup status")
    print()
    cmd_status(args)


RESULT = {"verified": "✓", "mismatch": "✗ uploaded, but Drive's copy does not match your file. Upload it again by hand:",
          "missing": "✗ no longer on your Mac, skipped:", "failed": "✗ failed:"}


def report(i, n, e):
    mark = RESULT.get(e["status"], e["status"])
    why = f"  ({e['error']})" if e["status"] == "failed" else f"  ({e['note']})" if e.get("note") else ""
    print(f"[{i}/{n}] {mark} {e['remote_dir']}/{e['name']}{why}")


def upload_one(sess, state, e):
    try:
        upload_file(sess, state, e)
    except UploadError as err:
        e.update(status="failed", error=str(err))


def upload_many(creds, state, todo, n, done):
    """Small files, several at a time: most of their time is round trips, not bytes."""
    from google.auth.transport.requests import AuthorizedSession

    local, stop = threading.local(), threading.Event()

    def work(e):
        if stop.is_set():
            return None
        if not hasattr(local, "sess"):
            local.sess = AuthorizedSession(creds)  # one connection pool per thread
        upload_one(local.sess, state, e)
        save(state, force=False)
        return e

    QUIET.set()
    pool = ThreadPoolExecutor(WORKERS)
    try:
        for f in as_completed([pool.submit(work, e) for e in todo]):
            if e := f.result():
                done(e)
    except BaseException:
        stop.set()  # let uploads in flight finish (seconds), start no new ones
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    finally:
        QUIET.clear()
        pool.shutdown(wait=True)


def upload_all(args):
    state = load()
    todo = [e for e in state["files"].values() if e["status"] not in FINISHED]
    if not todo:
        print("Nothing left to upload.")
        return False
    for e in todo:
        e["interrupted"] = e["status"] == "uploading"
        e.setdefault("file_id", None)  # every key exists up front, so saving mid-run never sees a dict grow
        e.setdefault("error", None)
        e.setdefault("note", None)
    sess = session()
    small = [e for e in todo if e["size"] <= SMALL]
    big = [e for e in todo if e["size"] > SMALL]
    print(f"Uploading {len(todo)} file(s), {human(sum(e['size'] - e.get('sent', 0) for e in todo))} to go"
          f"{f', {WORKERS} at a time' if len(small) > 1 else ''}. Safe to close the lid or press Ctrl+C at any point.\n")
    n, count, lock = len(todo), [0], threading.Lock()

    def done(e):
        with lock:
            count[0] += 1
            report(count[0], n, e)

    def big_files(s, stop, failed):
        """Big files, one at a time, alongside the small ones: small files are capped by
        Google's ~3 new files a second and leave most of the bandwidth unused."""
        try:
            for e in big:
                if stop.is_set():
                    return
                if not QUIET.is_set():
                    print(f"{e['remote_dir']}/{e['name']}  ({human(e['size'])})")
                upload_one(s, state, e)
                save(state, force=False)
                line("")
                done(e)
        except BaseException as err:  # e.g. Drive full: hand it to the main thread
            failed.append(err)

    stop, failed = threading.Event(), []
    worker = None
    try:
        if big:
            from google.auth.transport.requests import AuthorizedSession
            worker = threading.Thread(target=big_files, args=(AuthorizedSession(sess.credentials), stop, failed),
                                      daemon=True)  # Ctrl+C mustn't wait for a multi-GB file; it resumes next run
            if small:
                QUIET.set()  # live progress returns once only the big file is left
            worker.start()
        if small:
            upload_many(sess.credentials, state, small, n, done)
        if worker:
            while worker.is_alive():
                worker.join(0.5)
        if failed:
            raise failed[0]
    except Stop as err:
        print(f"\nStopped: {err}")
        alert(str(err))
    finally:
        stop.set()
        save(state)
    return todo


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


def under(path, roots):
    return not roots or any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def cmd_check(args):
    """Compare local files with Drive without uploading: proof before you delete or wipe anything."""
    state, sess = load(), session()
    remote_root = args.to.strip("/")
    same, missing, different = 0, [], []
    for i, (f, remote_dir) in enumerate(walk(args.paths, remote_root), 1):
        if i % 25 == 0:
            line(f"  checked {i} file(s)...")
        parent = lookup_folder(sess, state, remote_dir)
        there = listing(sess, parent).get(f.name, []) if parent else []
        size = f.stat().st_size
        sized = [x for x in there if int(x.get("size", -1)) == size]
        where = f"{remote_dir}/{f.name}"
        if not there:
            missing.append(where)
        elif sized and md5(f) in {x.get("md5Checksum") for x in sized}:
            same += 1
        else:
            different.append(where)
    line("")
    print(f"✓ {same} file(s) are in Drive and match your copy exactly.")
    for label, items in (("✗ not in Drive", missing), ("≠ in Drive but different from your copy", different)):
        if items:
            print(f"{label}: {len(items)}")
            for w in items[:50]:
                print(f"    {w}")
            if len(items) > 50:
                print(f"    ... and {len(items) - 50} more")
    if missing or different:
        print("\nUpload them with: driveup add " + " ".join(f'"{p}"' for p in args.paths)
              + (f' --to "{remote_root}"' if remote_root != "driveup" else ""))
    print("(driveup can only see files it uploaded itself, so files you put in Drive another way count as missing.)")


# NSFileManager's trashItem, what Finder uses: each file goes to the Trash on its own disk.
TRASH_JS = """
ObjC.import("Foundation");
function run(argv) {
  const fm = $.NSFileManager.defaultManager;
  return JSON.stringify(argv.map(p => {
    const err = $();
    return fm.trashItemAtURLResultingItemURLError($.NSURL.fileURLWithPath(p), $(), err)
      ? null : ObjC.unwrap(err.localizedDescription);
  }));
}"""


def trash_many(paths):
    """Move files to the Trash (not a permanent delete). Returns {path: error} for any that failed.
    On a Mac each file goes to the Trash of the disk it's on, so freeing an external drive
    doesn't copy everything onto the Mac's own disk on the way."""
    errors = {}
    if sys.platform != "darwin":
        bin_ = Path.home() / ".local" / "share" / "Trash" / "files"
        bin_.mkdir(parents=True, exist_ok=True)
        for p in map(Path, paths):
            dest, n = bin_ / p.name, 1
            while dest.exists():
                dest = bin_ / f"{p.stem} ({n}){p.suffix}"
                n += 1
            try:
                shutil.move(str(p), dest)
            except OSError as err:
                errors[str(p)] = str(err)
        return errors
    for i in range(0, len(paths), 500):  # stay well under the command-line length limit
        batch = paths[i:i + 500]
        r = subprocess.run(["osascript", "-l", "JavaScript", "-e", TRASH_JS, *batch], capture_output=True, text=True)
        try:
            results = json.loads(r.stdout)
        except ValueError:
            results = [r.stderr.strip() or "couldn't reach the Trash"] * len(batch)
        errors.update({p: err for p, err in zip(batch, results) if err})
    return errors


def cmd_free(args):
    """Free up space: move local files to the Trash, but only ones Drive has confirmed."""
    state = load()
    roots = [str(Path(p).expanduser().resolve()) for p in args.paths]
    picked = {}
    for e in state["files"].values():
        if e["status"] != "verified" or not e.get("file_id") or not under(e["local"], roots):
            continue
        try:
            st = os.stat(e["local"])
        except FileNotFoundError:
            continue
        if (st.st_size, int(st.st_mtime)) == (e["size"], e["mtime"]):  # unchanged since the checked upload
            picked.setdefault(e["local"], e)
    if not picked:
        sys.exit("Nothing to free: no uploaded-and-verified files are still on this Mac"
                 + (" under those folders." if roots else "."))
    # Make sure each copy is still in Drive (not deleted or trashed since), one listing per folder.
    sess = session()
    print("Confirming each file is still in Drive...")
    safe = []
    for e in picked.values():
        parent = state["folders"].get(e["remote_dir"]) or lookup_folder(sess, state, e["remote_dir"])
        ids = {x["id"] for x in listing(sess, parent).get(e["name"], [])} if parent else set()
        if e["file_id"] in ids:
            safe.append(e)
    gone = len(picked) - len(safe)
    if gone:
        print(f"Keeping {gone} file(s) on your Mac: their Drive copy is gone or in Drive's trash.")
    if not safe:
        return
    total = sum(e["size"] for e in safe)
    print(f"{len(safe)} file(s), {human(total)}, are safely in Drive and match your copies.")
    if not args.yes:
        answer = input("Move them to the Trash on this Mac? You can still put them back until you empty it. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            return print("Nothing moved.")
    moved, freed, errors = 0, 0, trash_many([e["local"] for e in safe])
    for e in safe:
        if err := errors.get(e["local"]):
            print(f"  couldn't move {e['local']}: {err}")
        else:
            e["note"] = "moved to Trash on this Mac"
            moved += 1
            freed += e["size"]
    save(state)
    print(f"Moved {moved} file(s), {human(freed)}, to the Trash. "
          "Empty the Trash to get the space back.")


def drive_target(sess, state, target):
    """A Drive file or folder id for a local path driveup uploaded, or a Drive folder path."""
    p = Path(target).expanduser()
    if p.exists():
        p = str(p.resolve())
        if os.path.isfile(p):
            hit = next((e for e in state["files"].values() if e["local"] == p and e.get("file_id")), None)
            return hit and hit["file_id"]
        for e in state["files"].values():
            if e["local"].startswith(p + "/"):
                depth = len(Path(e["local"]).parent.relative_to(p).parts)
                parts = e["remote_dir"].split("/")
                remote = "/".join(parts[:len(parts) - depth])
                return state["folders"].get(remote) or lookup_folder(sess, state, remote)
        return None
    return lookup_folder(sess, state, target)


def cmd_link(args):
    state, sess = load(), session()
    fid = drive_target(sess, state, args.target)
    if not fid or fid == "root":
        sys.exit(f"driveup hasn't uploaded \"{args.target}\". Give a file or folder it uploaded, "
                 'or a Drive folder path like "Backups/Movies".')
    check(call(sess, "POST", f"{API}/{fid}/permissions", json={"role": "reader", "type": "anyone"}))
    url = check(call(sess, "GET", f"{API}/{fid}", params={"fields": "webViewLink"}))["webViewLink"]
    print(url)
    if shutil.which("pbcopy"):
        subprocess.run(["pbcopy"], input=url, text=True)
        print("Copied. Anyone with this link can view (not edit). Stop sharing from Drive's Share button.")


# ---------- scheduled backups (macOS launchd) ----------

AGENTS = Path.home() / "Library" / "LaunchAgents"


def agent_label(paths, to):
    return "com.driveup." + hashlib.sha1("\0".join([to, *paths]).encode()).hexdigest()[:8]


def launchctl(*a):
    return subprocess.run(["launchctl", *a], capture_output=True, text=True)


def cmd_schedule(args):
    if sys.platform != "darwin":
        sys.exit("Scheduled backups use macOS launchd. On Linux, add `driveup add ...` to cron instead.")
    domain = f"gui/{os.getuid()}"
    agents = sorted(AGENTS.glob("com.driveup.*.plist"))
    if args.list or (not args.paths and not args.off):
        if not agents:
            return print('No scheduled backups. Add one with: driveup schedule ~/Documents --to "Backups"')
        for a in agents:
            job = plistlib.loads(a.read_bytes())
            cmd = job["ProgramArguments"]
            when = "hourly" if "StartInterval" in job else "daily at 2:00"
            print(f"{job['Label'][12:]}  {when}  {' '.join(cmd[2:])}")
        return print("\nTurn one off with: driveup schedule --off <id>   (or --off all)")
    if args.off:
        hits = agents if args.off == "all" else [a for a in agents if a.stem.endswith(args.off)]
        if not hits:
            sys.exit(f"No scheduled backup called {args.off}. See them with: driveup schedule --list")
        for a in hits:
            launchctl("bootout", f"{domain}/{a.stem}")
            a.unlink()
            print(f"Turned off {a.stem[12:]}.")
        return
    paths = [str(Path(p).expanduser().resolve()) for p in args.paths]
    for p in paths:
        if not os.path.exists(p):
            sys.exit(f"Not found: {p}")
    to = args.to.strip("/")
    label = agent_label(paths, to)
    exe = Path(sys.argv[0]).resolve()
    program = [str(exe)] if os.access(exe, os.X_OK) and exe.suffix != ".py" else [sys.executable, str(Path(__file__).resolve())]
    logs = HOME / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    job = {
        "Label": label,
        "ProgramArguments": [*program, "add", *paths, "--to", to],
        "EnvironmentVariables": {"DRIVEUP_SCHEDULED": "1", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        "StandardOutPath": str(logs / f"{label[12:]}.log"),
        "StandardErrorPath": str(logs / f"{label[12:]}.log"),
        "ProcessType": "Background",
        **({"StartInterval": 3600} if args.every == "hourly" else {"StartCalendarInterval": {"Hour": 2, "Minute": 0}}),
    }
    AGENTS.mkdir(parents=True, exist_ok=True)
    plist = AGENTS / f"{label}.plist"
    launchctl("bootout", f"{domain}/{label}")  # replace an existing schedule for the same folders
    plist.write_bytes(plistlib.dumps(job))
    r = launchctl("bootstrap", domain, str(plist))
    if r.returncode:
        plist.unlink()
        sys.exit(f"macOS didn't accept the schedule: {r.stderr.strip()}")
    when = "every hour" if args.every == "hourly" else "every day at 2:00 (or when your Mac next wakes)"
    print(f"Scheduled ({label[12:]}): {when}, new and changed files go to Drive folder \"{to or 'My Drive'}\".")
    print(f"Log: {job['StandardOutPath']}")
    private = [p for p in paths if any(p.startswith(str(Path.home() / d)) for d in ("Desktop", "Documents", "Downloads"))]
    if private:
        print("\nmacOS guards Desktop, Documents and Downloads from background jobs. If the log says "
              f"'Operation not permitted', open System Settings > Privacy & Security > Full Disk Access and add:\n  {program[0]}")


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
    c = sub.add_parser("check", help="compare local files with Drive, without uploading")
    c.add_argument("paths", nargs="+")
    c.add_argument("--to", default="driveup", help="Drive folder they were uploaded to (default: driveup)")
    f = sub.add_parser("free", help="move files already safe in Drive to the Trash on this Mac")
    f.add_argument("paths", nargs="*", help="only files under these folders (default: everything uploaded)")
    f.add_argument("--yes", action="store_true", help="don't ask first")
    sub.add_parser("link", help="get a view-only share link for something uploaded").add_argument(
        "target", help='a file or folder driveup uploaded, or a Drive folder path like "Backups/Movies"')
    sc = sub.add_parser("schedule", help="back up folders automatically (daily or hourly)")
    sc.add_argument("paths", nargs="*")
    sc.add_argument("--to", default="driveup", help="Drive folder (default: driveup)")
    sc.add_argument("--every", choices=["daily", "hourly"], default="daily")
    sc.add_argument("--list", action="store_true", help="show scheduled backups")
    sc.add_argument("--off", metavar="ID", help="turn off a scheduled backup (or 'all')")
    args = ap.parse_args()
    if not args.cmd:  # plain `driveup` opens the file picker
        args = ap.parse_args(["pick"])
    try:
        {"pick": cmd_pick, "add": cmd_add, "run": cmd_run, "status": cmd_status, "clear": cmd_clear, "login": cmd_login,
         "check": cmd_check, "free": cmd_free, "link": cmd_link, "schedule": cmd_schedule}[args.cmd](args)
    except KeyboardInterrupt:
        print("\n\nPaused. Progress is saved. Continue anytime with: driveup run")
    except PermissionError as err:  # in a scheduled run: macOS guarding Desktop, Documents or Downloads
        alert(f"macOS blocked {err.filename or 'a folder'}. Give driveup Full Disk Access in "
              "System Settings > Privacy & Security.")
        raise
    except Exception as err:
        alert(f"{type(err).__name__}: {err}")
        raise


if __name__ == "__main__":
    main()
