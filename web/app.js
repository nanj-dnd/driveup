import { CLIENT_ID } from "./config.js";
import { Drive, uploadFile, Paused, QuotaError } from "./engine.js";
import { AuthError, account, signIn, signOut, token } from "./auth.js";
import * as db from "./db.js";

const $ = (sel) => document.querySelector(sel);
const SKIP = new Set([".DS_Store", "Thumbs.db", "desktop.ini"]);
const ACTIVE = new Set(["waiting", "uploading", "checking"]);
const PROBLEM = new Set(["mismatch", "failed", "missing"]);
const PARALLEL = 3;              // files at once; helps a lot with folders of photos
const BIG = 100 * 1024 * 1024;   // ...but only one big file at a time, so one finishes before the next
const DAILY_RETRY = 60 * 60 * 1000;

const state = {
  email: null,
  settings: { speed: 0, existing: "keep", notify: true },
  jobs: [],
  folders: {},        // Drive path -> folder id
  batches: {},        // batch id -> { handles, dest }: what the user picked, for re-granting access and folder links
  blocked: new Set(), // jobs waiting for file permission after a Chrome restart
  active: new Set(),
  running: false,
  restart: false,
  controller: null,
  note: "",
  needsSignIn: false,
  quota: null,        // { reason, retryAt }
  free: null,         // bytes free in Drive
  uploaded: 0,        // bytes sent this session, for speed
  last: new Map(),    // job -> last reported offset
  samples: [],        // [time, uploaded]
};

const drive = new Drive(token, { onWait: (msg) => { state.note = msg; render(); } });

// ---------- formatting ----------

function human(n) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return i === 0 ? `${n} B` : `${n.toFixed(n < 10 ? 1 : 0)} ${units[i]}`;
}

function duration(s) {
  if (s < 90) return "under 2 min";
  if (s < 3600) return `${Math.round(s / 60)} min`;
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return m ? `${h} h ${m} min` : `${h} h`;
}

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const cleanPath = (p) => p.split("/").map((s) => s.trim()).filter(Boolean).join("/");
const join = (...parts) => parts.filter(Boolean).join("/");
const clock = (t) => new Date(t).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

function rate() {
  const s = state.samples;
  if (s.length < 2) return 0;
  const [t0, b0] = s[0], [t1, b1] = s[s.length - 1];
  return t1 - t0 > 3000 ? (b1 - b0) / ((t1 - t0) / 1000) : 0;
}

let toastTimer;
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 5000);
}

function notify(title, message) {
  if (!state.settings.notify || !document.hidden || globalThis.Notification?.permission !== "granted") return;
  const n = new Notification(title, { body: message, icon: "icons/128.png" });
  n.onclick = () => { focus(); n.close(); };
}

// Ask from a click, the first time it matters.
const askToNotify = () => {
  if (state.settings.notify && globalThis.Notification?.permission === "default") Notification.requestPermission();
};

// Keeps the screen (and so the computer) awake while uploading. Browsers drop it
// when the tab is hidden, so it's taken again whenever the tab comes back.
let wake;
async function stayAwake() {
  if (!state.running || document.hidden || wake) return;
  try { wake = await navigator.wakeLock.request("screen"); wake.onrelease = () => (wake = null); } catch {}
}

// ---------- persistence ----------

async function save(job) {
  await db.putJob(job);
  render();
}

async function pruneBatches() {
  const live = new Set(state.jobs.map((j) => j.batch));
  for (const id of Object.keys(state.batches)) if (!live.has(id)) delete state.batches[id];
  await db.set("batches", state.batches);
}

// ---------- adding files ----------

async function walk(dir, path, out) {
  for await (const h of dir.values()) {
    if (h.kind === "directory") await walk(h, join(path, h.name), out);
    else if (!SKIP.has(h.name)) out.push([h, path]);
  }
}

async function findSame(handle, remoteDir) {
  for (const j of state.jobs) {
    if (j.name === handle.name && j.remoteDir === remoteDir && (await j.handle.isSameEntry(handle).catch(() => false))) return j;
  }
}

async function addHandles(handles) {
  if (!handles.length) return;
  const dest = cleanPath($("#dest").value);
  $("#dest").value = dest;
  await db.set("dest", dest);

  const found = [];
  for (const h of handles) {
    if (h.kind === "file") found.push([h, dest]);
    else await walk(h, join(dest, h.name), found);
  }

  const batch = crypto.randomUUID();
  let added = 0, skipped = 0;
  for (const [handle, remoteDir] of found) {
    const file = await handle.getFile();
    const same = await findSame(handle, remoteDir);
    if (same && same.size === file.size && same.lastModified === file.lastModified) {
      if (same.status === "failed" || same.status === "missing") { same.status = "waiting"; same.error = null; await save(same); added++; }
      else skipped++;
      continue;
    }
    const job = {
      id: crypto.randomUUID(), batch, handle, name: handle.name, remoteDir,
      size: file.size, lastModified: file.lastModified, status: "waiting", sent: 0, added: Date.now() + added / 1000,
    };
    state.jobs.push(job);
    await db.putJob(job);
    added++;
  }
  state.batches[batch] = { handles, dest };
  await db.set("batches", state.batches);

  const where = dest ? `My Drive / ${dest}` : "My Drive";
  toast(added
    ? `Added ${plural(added, "file")} for ${where}${skipped ? `, skipped ${skipped} already uploaded` : ""}. Anything already in Drive is skipped, not copied.`
    : `Already uploaded. Nothing new to add.`);
  render();
  await refreshSpace();
  run();
}

async function pick(kind) {
  try {
    const handles = kind === "folder" ? [await showDirectoryPicker({ mode: "read" })] : await showOpenFilePicker({ multiple: true });
    await addHandles(handles);
  } catch (err) {
    if (err.name !== "AbortError") toast(err.message);
  }
}

// ---------- uploading ----------

const pending = () => state.jobs.filter((j) => ACTIVE.has(j.status));
const needed = () => pending().reduce((n, j) => n + j.size - (j.sent || 0), 0);

async function refreshSpace() {
  try { state.free = await drive.space(); } catch { /* shown next time */ }
  render();
}

// Called from a click when Chrome has forgotten file access (after a restart):
// Chrome only shows its permission prompt in response to a click.
async function regrant() {
  const batches = new Set(pending().map((j) => j.batch));
  for (const [id, { handles }] of Object.entries(state.batches)) {
    if (!batches.has(id)) continue;
    for (const h of handles) {
      try { if ((await h.queryPermission({ mode: "read" })) !== "granted") await h.requestPermission({ mode: "read" }); } catch {}
    }
  }
  await checkPermissions();
}

async function checkPermissions() {
  state.blocked.clear();
  for (const j of pending()) {
    if ((await j.handle.queryPermission({ mode: "read" }).catch(() => "denied")) !== "granted") state.blocked.add(j.id);
  }
  render();
}

// Shared speed limit across all parallel uploads.
let nextSlot = 0;
async function throttle(bytes, signal) {
  const bps = state.settings.speed * 1024 * 1024;
  if (!bps) return;
  const now = performance.now();
  nextSlot = Math.max(nextSlot, now);
  const wait = nextSlot - now;
  nextSlot += (bytes / bps) * 1000;
  if (wait > 0) await new Promise((resolve) => {
    const t = setTimeout(resolve, wait);
    signal.addEventListener("abort", () => { clearTimeout(t); resolve(); }, { once: true });
  });
}

function nextJob() {
  const bigBusy = [...state.active].some((j) => j.size >= BIG);
  return pending().find((j) => !state.blocked.has(j.id) && !state.active.has(j) && !(bigBusy && j.size >= BIG));
}

let dailyTimer;
async function run() {
  if (state.running || !state.email) return;
  clearTimeout(dailyTimer);
  state.running = true;
  state.needsSignIn = false;
  state.quota = null;
  state.controller = new AbortController();
  stayAwake();
  render();
  refreshSpace();

  const signal = state.controller.signal;
  // Holding a lock tells Chrome this tab is busy, so it doesn't freeze it in the background.
  let unlock;
  navigator.locks?.request("driveup-uploading", () => new Promise((r) => (unlock = r)));
  const worker = async () => {
    let job;
    while (!signal.aborted && (job = nextJob())) {
      state.active.add(job);
      try { await uploadOne(job, signal); } finally { state.active.delete(job); state.last.delete(job); }
    }
  };
  const userPaused = () => signal.aborted && !state.quota && !state.needsSignIn && !state.restart;
  try {
    await Promise.all(Array.from({ length: PARALLEL }, worker));
  } finally {
    state.running = false;
    state.note = "";
    wake?.release();
    unlock?.();
    render();
  }

  if (state.restart) {
    state.restart = false;
    return run();
  }
  if (state.quota?.reason === "uploadLimitExceeded") {
    state.quota.retryAt = Date.now() + DAILY_RETRY;
    dailyTimer = setTimeout(run, DAILY_RETRY);
    render();
  }
  if (!userPaused()) announce();
}

function announce() {
  if (state.quota) return notify("Driveup paused", quotaText(state.quota));
  if (state.needsSignIn) return notify("Driveup paused", "Sign in again to keep uploading.");
  if (state.blocked.size) return notify("Driveup paused", "Your browser needs your OK to keep reading your files.");
  const problems = state.jobs.filter((j) => PROBLEM.has(j.status)).length;
  const done = state.jobs.filter((j) => j.status === "done").length;
  if (problems) notify("Uploads finished, with problems", `${plural(done, "file")} uploaded. ${problems} need${problems === 1 ? "s" : ""} attention.`);
  else if (done) notify("All uploads finished", `${plural(done, "file")} uploaded, and each one matches Drive's copy.`);
}

async function uploadOne(job, signal) {
  state.note = "";
  render();
  let file;
  try {
    file = await job.handle.getFile();
  } catch (err) {
    if (err.name === "NotAllowedError") { state.blocked.add(job.id); return render(); }
    Object.assign(job, { status: "missing" });
    return save(job);
  }
  try {
    await uploadFile(drive, job, file, {
      folders: state.folders,
      saveFolders: (f) => db.set("folders", f),
      save,
      onProgress,
      signal,
      existing: state.settings.existing,
      throttle,
      limited: () => state.settings.speed > 0,
    });
  } catch (err) {
    if (err instanceof Paused) {
      job.status = "waiting";
    } else if (err instanceof QuotaError) {
      // The whole account is stuck, not this file: stop everything instead of failing each file.
      job.status = "waiting";
      state.quota = { reason: err.reason };
      state.controller.abort();
      refreshSpace();
    } else if (err instanceof AuthError) {
      job.status = "waiting";
      state.needsSignIn = true;
      state.controller.abort();
    } else {
      Object.assign(job, { status: "failed", error: err.message });
    }
    await save(job);
  }
}

function onProgress(job, bytes) {
  state.note = "";
  if (job.status === "checking") {
    job.checked = bytes;
  } else {
    const last = state.last.get(job);
    if (last !== undefined && bytes > last) state.uploaded += bytes - last;
    state.last.set(job, bytes);
    const now = Date.now();
    state.samples.push([now, state.uploaded]);
    while (state.samples.length > 2 && now - state.samples[0][0] > 30000) state.samples.shift();
  }
  render();
}

function pause() {
  state.restart = false;
  clearTimeout(dailyTimer);
  state.controller?.abort();
}

async function remove(job) {
  state.jobs = state.jobs.filter((j) => j !== job);
  await db.deleteJob(job.id);
  if (state.active.has(job)) { state.restart = true; state.controller.abort(); }
  await pruneBatches();
  render();
}

async function retry(job) {
  Object.assign(job, { status: "waiting", error: null });
  await save(job);
  run();
}

async function clearFinished() {
  for (const j of state.jobs.filter((j) => j.status === "done")) await db.deleteJob(j.id);
  state.jobs = state.jobs.filter((j) => j.status !== "done");
  await pruneBatches();
  render();
}

// ---------- links and reports ----------

async function getLink(id, isFolder, what) {
  const ok = confirm(`Make a link to ${what}?\n\nAnyone who has the link will be able to view${isFolder ? " everything in this folder" : " it"}. You can turn this off later in Google Drive under Share.`);
  if (!ok) return;
  try {
    const link = await drive.share(id, isFolder);
    await navigator.clipboard.writeText(link);
    toast("Link copied. Anyone with it can view.");
  } catch (err) {
    toast(`Couldn't make a link: ${err.message}`);
  }
}

const fileUrl = (id) => `https://drive.google.com/file/d/${id}/view`;
const folderUrl = (id) => `https://drive.google.com/drive/folders/${id}`;

function statusWords(job) {
  const pct = job.size ? Math.floor(((job.sent || 0) * 100) / job.size) : 0;
  switch (job.status) {
    case "done": return job.note || (job.replaced ? "Uploaded and checked (replaced older copy)" : "Uploaded and checked");
    case "mismatch": return "Uploaded but doesn't match the original";
    case "failed": return `Failed: ${job.error || "unknown error"}`;
    case "missing": return "File no longer on this computer";
  }
  return job.sent ? `Partly uploaded (${pct}%)` : "Not uploaded yet";
}

function downloadReport() {
  const cell = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const rows = [["File", "Drive folder", "Size (bytes)", "Status", "Link"]];
  for (const j of state.jobs) rows.push([j.name, j.remoteDir || "My Drive", j.size, statusWords(j), j.fileId && j.status === "done" ? fileUrl(j.fileId) : ""]);
  const blob = new Blob([rows.map((r) => r.map(cell).join(",")).join("\n")], { type: "text/csv" });
  const a = Object.assign(document.createElement("a"), {
    href: URL.createObjectURL(blob), download: `driveup-report-${new Date().toISOString().slice(0, 10)}.csv`,
  });
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// Folders the user dropped in, for "open" and "get link".
function topFolders() {
  const out = [];
  for (const [id, { handles, dest }] of Object.entries(state.batches)) {
    for (const h of handles) {
      if (h.kind !== "directory") continue;
      const path = join(dest, h.name);
      const jobs = state.jobs.filter((j) => j.batch === id && (j.remoteDir === path || j.remoteDir.startsWith(path + "/")));
      if (jobs.length && !out.some((f) => f.path === path)) out.push({ path, name: h.name, jobs });
    }
  }
  return out;
}

// ---------- rendering ----------

function describe(job) {
  const pct = job.size ? Math.floor((job.sent * 100) / job.size) : 0;
  const amount = `${human(job.sent)} of ${human(job.size)}`;
  switch (job.status) {
    case "done": return ["done", statusWords(job), 100];
    case "mismatch": return ["problem", "Drive's copy doesn't match this file. Delete it in Drive and add it again.", 0];
    case "failed": return ["problem", job.error || "Upload failed", 0];
    case "missing": return ["problem", "This file isn't on your computer any more", 0];
    case "checking": {
      const c = job.size ? Math.floor(((job.checked || 0) * 100) / job.size) : 100;
      return ["checking", job.fileId ? `Uploaded. Checking it against your file… ${c}%` : `Checking whether it's already in Drive… ${c}%`, c];
    }
  }
  if (state.blocked.has(job.id)) return ["waiting", `Needs your OK to read this file again · ${pct}%`, pct];
  if (state.active.has(job)) {
    if (state.note) return ["uploading", `${state.note}… · ${pct}%`, pct];
    const r = state.active.size === 1 ? rate() : 0;
    const left = r ? ` · ${human(r)}/s · ${duration((job.size - job.sent) / r)} left` : "";
    return ["uploading", `${pct}% · ${amount}${left}`, pct];
  }
  if (job.sent) return ["waiting", `${state.running ? "Will continue" : "Paused"} at ${pct}% · ${amount}`, pct];
  return ["waiting", `Waiting · ${human(job.size)}`, 0];
}

function quotaText(q) {
  switch (q.reason) {
    case "storageQuotaExceeded": return "Your Google Drive is full. Free up space or get more storage, then press Resume. Nothing uploaded so far is lost.";
    case "uploadLimitExceeded": return `Google's daily upload limit (750 GB a day) is reached. Driveup will try again${q.retryAt ? ` at ${clock(q.retryAt)}` : " in an hour"}.`;
    case "activeItemCreationLimitExceeded": return "Your Drive has reached Google's limit of 5 million files. Delete some files, then press Resume.";
    default: return "Google stopped accepting uploads for this account. Press Resume to try again.";
  }
}

const MARKS = { done: "✓", problem: "!", uploading: "↑", checking: "↑", waiting: "•" };
const rows = new Map();

function row(job) {
  let li = rows.get(job.id);
  if (li) return li;
  li = document.createElement("li");
  li.className = "job";
  li.innerHTML = `<span class="mark"></span>
    <div class="name"><span class="file"></span><span class="where"></span></div>
    <div class="actions"></div>
    <div class="meta"></div>
    <div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100"><i></i></div>`;
  li.querySelector(".file").textContent = job.name;
  li.querySelector(".where").textContent = job.remoteDir ? `in ${job.remoteDir}` : "in My Drive";
  li.querySelector(".name").title = `${job.remoteDir ? job.remoteDir + "/" : ""}${job.name}`;
  rows.set(job.id, li);
  return li;
}

const button = (text, onclick, className = "link") => Object.assign(document.createElement("button"), { className, textContent: text, onclick });
const link = (text, href) => Object.assign(document.createElement("a"), { href, target: "_blank", textContent: text });

function actions(li, job) {
  const box = li.querySelector(".actions");
  const key = `${job.status}:${!!job.fileId}`;
  if (box.dataset.key === key) return;
  box.dataset.key = key;
  box.replaceChildren();
  if (job.status === "done" && job.fileId) {
    box.append(link("Open", fileUrl(job.fileId)), button("Get link", () => getLink(job.fileId, false, `"${job.name}"`)));
  }
  if (job.status === "failed" || job.status === "missing") box.append(button("Retry", () => retry(job)));
  const x = button("×", () => remove(job), "icon");
  x.title = "Remove from list";
  x.setAttribute("aria-label", `Remove ${job.name} from list`);
  box.append(x);
}

function drawFolders() {
  const list = $("#folders");
  const folders = topFolders();
  const key = JSON.stringify(folders.map((f) => [f.path, !!state.folders[f.path], f.jobs.filter((j) => j.status === "done").length, f.jobs.length]));
  if (list.dataset.key === key) return;
  list.dataset.key = key;
  list.replaceChildren();
  list.hidden = !folders.length;
  for (const f of folders) {
    const done = f.jobs.filter((j) => j.status === "done").length;
    const li = document.createElement("li");
    const label = document.createElement("span");
    label.innerHTML = `<b></b> <span class="fine"></span>`;
    label.querySelector("b").textContent = f.name;
    label.querySelector(".fine").textContent = done === f.jobs.length ? `all ${plural(f.jobs.length, "file")} uploaded` : `${done} of ${plural(f.jobs.length, "file")} uploaded`;
    li.append(label);
    const id = state.folders[f.path];
    if (id) {
      const acts = document.createElement("span");
      acts.className = "folder-actions";
      acts.append(link("Open in Drive", folderUrl(id)), button("Get link", () => getLink(id, true, `the folder "${f.name}"`)));
      li.append(acts);
    }
    list.append(li);
  }
}

let frame;
function render() {
  title();
  if (frame) return;
  frame = requestAnimationFrame(() => { frame = null; draw(); });
}

// Chrome doesn't paint hidden tabs, so the tab title carries progress while you're elsewhere.
function title() {
  const total = state.jobs.reduce((n, j) => n + j.size, 0);
  const sent = state.jobs.reduce((n, j) => n + (j.status === "done" ? j.size : j.sent || 0), 0);
  document.title = state.running && total ? `${Math.floor((sent * 100) / total)}% · Driveup` : "Driveup";
}

function draw() {
  const signedIn = !!state.email;
  $("#account").hidden = !signedIn;
  $("#email").textContent = state.email || "";
  $("#welcome").hidden = signedIn;
  $("#app").hidden = !signedIn;
  if (!signedIn) return;

  // Banner: things only the user can fix.
  const banner = $("#banner"), action = $("#banner-action"), extra = $("#banner-link");
  extra.hidden = true;
  if (state.needsSignIn) {
    $("#banner-text").textContent = "Google needs you to sign in again before uploads can continue. To skip this next time, allow pop-ups for this site (the icon at the right of the address bar).";
    action.textContent = "Sign in";
    action.onclick = doSignIn;
  } else if (state.quota) {
    $("#banner-text").textContent = quotaText(state.quota);
    action.textContent = state.quota.reason === "uploadLimitExceeded" ? "Try now" : "Resume";
    action.onclick = run;
    if (state.quota.reason === "storageQuotaExceeded") {
      extra.hidden = false;
      extra.href = "https://one.google.com/storage";
      extra.textContent = "See what's using space";
    }
  } else if (state.blocked.size) {
    $("#banner-text").textContent = `Your browser restarted, so it needs your OK to read ${state.blocked.size === 1 ? "this file" : `these ${state.blocked.size} files`} again.`;
    action.textContent = "Resume uploads";
    action.onclick = async () => { await regrant(); run(); };
  }
  banner.hidden = !(state.needsSignIn || state.quota || state.blocked.size);

  // Summary.
  const jobs = state.jobs;
  const total = jobs.reduce((n, j) => n + j.size, 0);
  const sent = jobs.reduce((n, j) => n + (j.status === "done" || j.status === "mismatch" || (j.status === "checking" && j.fileId) ? j.size : j.sent || 0), 0);
  const done = jobs.filter((j) => j.status === "done").length;
  const problems = jobs.filter((j) => PROBLEM.has(j.status)).length;
  const left = pending().length;
  $("#summary").hidden = !jobs.length;
  if (jobs.length) {
    $("#summary-title").textContent = !left && !problems
      ? (jobs.length === 1 ? "Done. It matches Drive's copy." : `All ${jobs.length} files uploaded. Every one matches Drive's copy.`)
      : `${done} of ${plural(jobs.length, "file")} uploaded`;

    const bits = [`${human(sent)} of ${human(total)}`];
    if (state.running) {
      const r = rate();
      if (state.note) bits.push(state.note);
      else if (r) bits.push(`${human(r)}/s`, `about ${duration((total - sent) / r)} left`);
      if (state.settings.speed) bits.push(`limited to ${state.settings.speed} MB/s`);
    } else if (left && !state.blocked.size && !state.needsSignIn && !state.quota) bits.push("Paused");
    if (problems) bits.push(`${problems} need${problems === 1 ? "s" : ""} attention`);
    $("#summary-sub").textContent = bits.join(" · ");
    $("#overall").style.width = `${total ? (sent * 100) / total : 100}%`;

    // Space check up front, so Drive doesn't fill up halfway through.
    const need = needed();
    const short = state.free !== null && left && need > state.free && !state.quota;
    $("#space").hidden = !short;
    if (short) $("#space").textContent = `These uploads need ${human(need)}, but your Drive has ${human(Math.max(0, state.free))} free. Driveup will upload what fits, then pause.`;

    const toggle = $("#toggle");
    toggle.hidden = !left || state.needsSignIn || !!state.quota || state.blocked.size === left;
    toggle.textContent = state.running ? "Pause" : "Resume";
    toggle.className = state.running ? "" : "primary";
    toggle.onclick = state.running ? pause : run;
  }

  drawFolders();

  // Rows: uploading first, then problems, then waiting, then finished.
  const rank = { uploading: 0, checking: 0, problem: 1, waiting: 2, done: 3 };
  const described = jobs.map((j) => [j, describe(j)]);
  described.sort((a, b) => rank[a[1][0]] - rank[b[1][0]] || a[0].added - b[0].added);

  const list = $("#jobs");
  const live = new Set();
  described.forEach(([job, [kind, text, pct]], i) => {
    const li = row(job);
    live.add(job.id);
    li.dataset.s = kind;
    li.querySelector(".mark").textContent = MARKS[kind];
    li.querySelector(".meta").textContent = text;
    li.querySelector(".bar i").style.width = `${pct}%`;
    li.querySelector(".bar").setAttribute("aria-valuenow", String(pct));
    actions(li, job);
    if (list.children[i] !== li) list.insertBefore(li, list.children[i] || null);
  });
  for (const [id, li] of rows) if (!live.has(id)) { li.remove(); rows.delete(id); }

  $("#list-foot").hidden = !jobs.length;
  $("#clear").hidden = !done;
}

// ---------- sign-in and settings ----------

async function doSignIn() {
  try {
    state.email = await signIn();
    state.needsSignIn = false;
    render();
    await checkPermissions();
    run();
  } catch (err) {
    toast(err instanceof TypeError ? "You're offline. Connect to the internet and try again." : `Sign-in didn't finish: ${err.message}`);
  }
}

async function saveSettings() {
  state.settings = {
    speed: Number($("#speed").value),
    existing: $("#existing").value,
    notify: $("#notify").checked,
  };
  await db.set("settings", state.settings);
  askToNotify();
  render();
}

// ---------- start ----------

function wire() {
  $("#signin").onclick = doSignIn;
  $("#signout").onclick = async () => {
    pause();
    await signOut();
    state.email = null;
    render();
  };
  $("#pick-files").onclick = () => { askToNotify(); pick("files"); };
  $("#pick-folder").onclick = () => { askToNotify(); pick("folder"); };
  $("#clear").onclick = clearFinished;
  $("#report").onclick = downloadReport;
  $("#dest").onchange = (e) => db.set("dest", (e.target.value = cleanPath(e.target.value)));
  for (const id of ["speed", "existing", "notify"]) $(`#${id}`).onchange = saveSettings;

  const zone = $("#drop");
  addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("over"); });
  addEventListener("dragleave", (e) => { if (!e.relatedTarget) zone.classList.remove("over"); });
  addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("over");
    if (!state.email) return;
    // getAsFileSystemHandle must be called for every item before anything is awaited.
    const items = [...e.dataTransfer.items].filter((i) => i.kind === "file").map((i) => i.getAsFileSystemHandle());
    Promise.all(items).then((hs) => addHandles(hs.filter(Boolean)));
  });

  document.addEventListener("visibilitychange", () => { if (!document.hidden) { render(); stayAwake(); } });
  addEventListener("beforeunload", (e) => { if (state.running) e.preventDefault(); });
}

async function init() {
  if (!CLIENT_ID) {
    $("#setup").hidden = false;
    $("#redirect").textContent = location.origin;
    $("#copy-redirect").onclick = async () => {
      await navigator.clipboard.writeText(location.origin);
      toast("Copied.");
    };
    return;
  }
  // Picking files and resuming after a restart need the File System Access API.
  if (!globalThis.showOpenFilePicker) {
    $("#unsupported").hidden = false;
    return;
  }
  // Only one Driveup tab may upload, so the same file isn't uploaded twice.
  // The first tab holds this lock for as long as it's open.
  const first = await new Promise((resolve) => {
    navigator.locks.request("driveup-tab", { ifAvailable: true }, (lock) => {
      resolve(!!lock);
      return lock && new Promise(() => {});
    });
  });
  if (!first) {
    $("#elsewhere").hidden = false;
    return;
  }
  wire();

  state.email = await account();
  state.settings = { ...state.settings, ...(await db.get("settings")) };
  $("#speed").value = String(state.settings.speed);
  $("#existing").value = state.settings.existing;
  $("#notify").checked = state.settings.notify;
  state.folders = (await db.get("folders")) || {};
  state.batches = (await db.get("batches")) || {};
  $("#dest").value = (await db.get("dest")) ?? "Driveup";
  state.jobs = (await db.allJobs()).sort((a, b) => a.added - b.added);
  for (const j of state.jobs) if (j.status === "uploading" || j.status === "checking") j.status = "waiting";
  render();
  if (state.email && pending().length) {
    await checkPermissions();
    run();
  } else if (state.email) {
    refreshSpace();
  }
}

init();
