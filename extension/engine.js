// Upload engine: Drive's resumable upload protocol, with progress saved after
// every chunk so an interrupted file continues from the last byte Drive confirmed.
import { MD5 } from "./md5.js";

export const CHUNK = 8 * 1024 * 1024; // Drive requires multiples of 256 KB
export const SMALL_CHUNK = 1024 * 1024; // used under a speed limit, so traffic stays smooth
const FOLDER = "application/vnd.google-apps.folder";

export class Paused extends Error {}
export class DriveError extends Error {}
// Problems with the whole account rather than one file: storage full, daily limit.
export class QuotaError extends DriveError {
  constructor(reason, message) { super(message); this.reason = reason; }
}
const QUOTA = new Set(["storageQuotaExceeded", "uploadLimitExceeded", "activeItemCreationLimitExceeded", "teamDriveFileLimitExceeded"]);

const sleep = (ms, signal) => new Promise((resolve, reject) => {
  if (signal?.aborted) return reject(new Paused());
  const t = setTimeout(resolve, ms);
  signal?.addEventListener("abort", () => { clearTimeout(t); reject(new Paused()); }, { once: true });
});

async function reason(r) {
  try { return JSON.parse(await r.clone().text()).error.errors[0].reason; } catch { return ""; }
}

// "busy" waits briefly; "limited" is Google throttling this account, so back off longer.
async function retryable(r) {
  if ([429, 500, 502, 503, 504].includes(r.status)) return "busy";
  if (r.status === 403 && /rateLimit/i.test(await reason(r))) return "limited";
  return null;
}

async function check(r) {
  if (r.ok) return r;
  const why = await reason(r);
  const text = (await r.text()).slice(0, 200);
  if (QUOTA.has(why)) throw new QuotaError(why, text);
  throw new DriveError(`Drive returned ${r.status}: ${text}`);
}

const json = async (r) => (await check(r)).json();
const esc = (name) => name.replace(/\\/g, "\\\\").replace(/'/g, "\\'");
const received = (r) => { // "bytes=0-12345"
  const range = r.headers.get("Range");
  return range ? Number(range.split("-").pop()) + 1 : 0;
};
const WAIT = {
  busy: "Drive is busy, retrying",
  limited: "Google is limiting uploads right now (this happens after about 750 GB in a day). Retrying automatically",
};

export class Drive {
  // token(forceRefresh) resolves to an OAuth access token.
  constructor(token, { base = "https://www.googleapis.com", onWait = () => {} } = {}) {
    this.token = token;
    this.api = `${base}/drive/v3/files`;
    this.upload = `${base}/upload/drive/v3/files`;
    this.about = `${base}/drive/v3/about`;
    this.onWait = onWait;
    this.folderQueue = Promise.resolve(); // folder creation runs one at a time, so parallel uploads don't make twins
  }

  // One attempt. A dropped connection rejects with a TypeError.
  async send(method, url, { headers = {}, body, signal } = {}) {
    const go = async (force) => fetch(url, {
      method, body, signal, headers: { ...headers, Authorization: `Bearer ${await this.token(force)}` },
    });
    const r = await go(false);
    return r.status === 401 ? go(true) : r;
  }

  // Waits out network drops (sleep, wifi changes) and server hiccups.
  async call(method, url, opts = {}) {
    for (let delay = 2000; ; ) {
      let kind = "network";
      try {
        const r = await this.send(method, url, opts);
        kind = await retryable(r);
        if (!kind) return r;
        this.onWait(WAIT[kind]);
      } catch (err) {
        if (opts.signal?.aborted) throw new Paused();
        if (!(err instanceof TypeError)) throw err;
        this.onWait("Waiting for internet connection");
      }
      await sleep(delay, opts.signal);
      delay = Math.min(delay * 2, kind === "limited" ? 300000 : 30000);
    }
  }

  postJSON(url, body, signal, method = "POST") {
    return this.call(method, url, { signal, body: JSON.stringify(body), headers: { "Content-Type": "application/json; charset=UTF-8" } });
  }

  async space(signal) {
    const { storageQuota: q } = await json(await this.call("GET", `${this.about}?fields=storageQuota(limit,usage)`, { signal }));
    return q.limit ? Number(q.limit) - Number(q.usage) : Infinity; // no limit on some Workspace plans
  }

  // Files Driveup already put in this folder under this name.
  async find(parent, name, signal) {
    const q = `name = '${esc(name)}' and '${parent}' in parents and trashed = false and mimeType != '${FOLDER}'`;
    const url = `${this.api}?${new URLSearchParams({ q, fields: "files(id,size,md5Checksum)" })}`;
    return (await json(await this.call("GET", url, { signal }))).files;
  }

  // Anyone with the link can view. Returns the link.
  async share(id, isFolder, signal) {
    await json(await this.postJSON(`${this.api}/${id}/permissions?fields=id`, { role: "reader", type: "anyone" }, signal));
    return isFolder ? `https://drive.google.com/drive/folders/${id}?usp=sharing` : `https://drive.google.com/file/d/${id}/view?usp=sharing`;
  }

  // Find or create each folder along a Drive path like "Backups/Trip".
  folderId(path, folders, saveFolders, signal) {
    const run = this.folderQueue.then(() => this.#folderId(path, folders, saveFolders, signal));
    this.folderQueue = run.catch(() => {});
    return run;
  }

  async #folderId(path, folders, saveFolders, signal) {
    let parent = "root", sofar = "";
    for (const part of path.split("/").filter(Boolean)) {
      sofar = sofar ? `${sofar}/${part}` : part;
      if (!folders[sofar]) {
        const q = `name = '${esc(part)}' and mimeType = '${FOLDER}' and '${parent}' in parents and trashed = false`;
        const { files } = await json(await this.call("GET", `${this.api}?${new URLSearchParams({ q, fields: "files(id)" })}`, { signal }));
        folders[sofar] = files.length ? files[0].id
          : (await json(await this.postJSON(`${this.api}?fields=id`, { name: part, mimeType: FOLDER, parents: [parent] }, signal))).id;
        await saveFolders(folders);
      }
      parent = folders[sofar];
    }
    return parent;
  }
}

export async function md5File(blob, { signal, onProgress } = {}) {
  const h = new MD5();
  for (let off = 0; off < blob.size; off += CHUNK) {
    if (signal?.aborted) throw new Paused();
    h.update(new Uint8Array(await blob.slice(off, off + CHUNK).arrayBuffer()));
    onProgress?.(Math.min(off + CHUNK, blob.size));
  }
  return h.hex();
}

// Uploads (or resumes) one job. The job is mutated and passed to save() as it progresses.
//   existing: "keep" uploads alongside a same-named file; "replace" makes it a new version of it.
//   Identical files already in Drive are always skipped.
export async function uploadFile(drive, job, file, {
  folders, saveFolders, save, onProgress = () => {}, signal,
  existing = "keep", throttle = async () => {}, limited = () => false,
}) {
  if (file.size !== job.size || file.lastModified !== job.lastModified) {
    // Changed on disk since it was queued: start this file over.
    Object.assign(job, { size: file.size, lastModified: file.lastModified, session: null, sent: 0, fileId: null, localMd5: null, looked: false, replaceId: null });
  }
  const parent = () => drive.folderId(job.remoteDir, folders, saveFolders, signal);
  const localMd5 = async () => (job.localMd5 ||= await md5File(file, { signal, onProgress: (n) => onProgress(job, n) }));

  // Already in Drive from an earlier upload? Then don't make a "(1)" copy.
  if (!job.fileId && !job.session && !job.looked) {
    const same = await drive.find(await parent(), job.name, signal);
    const sized = same.filter((f) => Number(f.size ?? 0) === job.size);
    if (sized.length) {
      job.status = "checking";
      const mine = job.size ? await localMd5() : null;
      const twin = sized.find((f) => !job.size || f.md5Checksum === mine);
      if (twin) {
        Object.assign(job, { fileId: twin.id, status: "done", note: "Already in Drive", sent: job.size });
        return save(job);
      }
    }
    if (same.length && existing === "replace") job.replaceId = same[0].id;
    job.looked = true;
    await save(job);
  }

  if (job.size === 0) {
    job.fileId = job.replaceId
      || (await json(await drive.postJSON(`${drive.api}?fields=id`, { name: job.name, parents: [await parent()] }, signal))).id;
    Object.assign(job, { status: "done", session: null });
    return save(job);
  }

  if (!job.fileId) {
    job.status = "uploading";
    const done = await sendBytes(drive, job, file, { parent, folders, save, onProgress, signal, throttle, limited });
    Object.assign(job, { fileId: done.id, remoteMd5: done.md5Checksum, sent: job.size, session: null, replaced: !!job.replaceId });
  }

  job.status = "checking";
  await save(job);
  job.remoteMd5 ||= (await json(await drive.call("GET", `${drive.api}/${job.fileId}?fields=md5Checksum`, { signal }))).md5Checksum;
  job.status = (await localMd5()) === job.remoteMd5 ? "done" : "mismatch";
  await save(job);
}

async function sendBytes(drive, job, file, { parent, folders, save, onProgress, signal, throttle, limited }) {
  const start = async () => {
    for (let attempt = 0; ; attempt++) {
      const replace = job.replaceId;
      const r = await drive.call(replace ? "PATCH" : "POST",
        `${drive.upload}${replace ? `/${replace}` : ""}?uploadType=resumable&fields=id,md5Checksum`, {
          signal,
          body: JSON.stringify(replace ? {} : { name: job.name, parents: [await parent()] }),
          headers: {
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": file.type || "application/octet-stream",
            "X-Upload-Content-Length": String(job.size),
          },
        });
      if (r.status === 404 && attempt === 0) {
        if (replace) job.replaceId = null; // the old copy was deleted; upload a new one
        else for (const k in folders) delete folders[k]; // a cached folder was deleted in Drive; rebuild the path
        continue;
      }
      await check(r);
      Object.assign(job, { session: r.headers.get("Location"), sent: 0, sessionStarted: Date.now() });
      await save(job);
      return { offset: 0 };
    }
  };

  // Ask Drive how much it has. Sessions expire after about a week; then start over.
  const resumePoint = async () => {
    if (!job.session) return start();
    const r = await drive.call("PUT", job.session, { signal, headers: { "Content-Range": `bytes */${job.size}` } });
    if (r.ok) return { offset: job.size, done: await r.json() };
    if (r.status === 308) return { offset: received(r) };
    if (r.status === 404 || r.status === 410) return start();
    await check(r);
  };

  let { offset, done } = await resumePoint();
  while (!done) {
    job.sent = offset;
    await save(job);
    onProgress(job, offset);

    const chunk = file.slice(offset, Math.min(offset + (limited() ? SMALL_CHUNK : CHUNK), job.size));
    await throttle(chunk.size, signal);
    let r;
    try {
      r = await drive.send("PUT", job.session, {
        signal, body: chunk, headers: { "Content-Range": `bytes ${offset}-${offset + chunk.size - 1}/${job.size}` },
      });
    } catch (err) {
      if (signal?.aborted) throw new Paused();
      if (!(err instanceof TypeError)) throw err;
      // Either the connection dropped or the file can't be read any more.
      try { await chunk.slice(0, 1).arrayBuffer(); } catch {
        throw new DriveError("The file changed or can't be read. Retry to upload it again.");
      }
      drive.onWait("Connection lost, waiting for it to come back");
      await sleep(3000, signal);
      ({ offset, done } = await resumePoint());
      continue;
    }
    if (r.ok) done = await r.json();
    else if (r.status === 308) offset = received(r);
    else if (r.status === 404 || r.status === 410) { job.session = null; ({ offset, done } = await resumePoint()); }
    else if (await retryable(r)) {
      drive.onWait(WAIT[await retryable(r)]);
      await sleep(5000, signal);
      ({ offset, done } = await resumePoint());
    } else await check(r);
  }
  onProgress(job, job.size);
  return done;
}
