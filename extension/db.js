// IndexedDB storage. Jobs live here (not chrome.storage) because they hold file
// handles, which is what lets an upload resume after Chrome restarts.
let opened;

function open() {
  return opened ??= new Promise((resolve, reject) => {
    const req = indexedDB.open("driveup", 1);
    req.onupgradeneeded = () => {
      req.result.createObjectStore("jobs", { keyPath: "id" });
      req.result.createObjectStore("kv");
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function tx(store, mode, fn) {
  const db = await open();
  return new Promise((resolve, reject) => {
    const t = db.transaction(store, mode);
    const req = fn(t.objectStore(store));
    t.oncomplete = () => resolve(req.result);
    t.onerror = () => reject(t.error);
  });
}

export const allJobs = () => tx("jobs", "readonly", (s) => s.getAll());
export const putJob = (job) => tx("jobs", "readwrite", (s) => s.put(job));
export const deleteJob = (id) => tx("jobs", "readwrite", (s) => s.delete(id));
export const get = (key) => tx("kv", "readonly", (s) => s.get(key));
export const set = (key, value) => tx("kv", "readwrite", (s) => s.put(value, key));
