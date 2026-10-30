// Capture app: take photos on the phone, send them through Firebase, watch the 3D model get made.
//
// How it talks to the backend (it never calls the backend directly):
//   1. Sign in anonymously, so the security rules know who uploaded what.
//   2. Upload each photo to Storage:   uploads/{jobId}/front.jpg (+ left.jpg, back.jpg, right.jpg)
//   3. Create the Firestore document:  jobs/{jobId}  { status: "uploaded", category, views, ... }
//   4. The backend (backend/app/firebase_sync.py) notices it, runs the job, and keeps updating that
//      document (status, progress, files). We just listen to it with onSnapshot.
//   5. When it's done, the files are in Storage at jobs/{jobId}/model.glb etc.
// The document fields are listed at the top of backend/app/firebase_sync.py.

import { initializeApp } from "https://www.gstatic.com/firebasejs/12.19.0/firebase-app.js";
import {
  connectAuthEmulator, getAuth, onAuthStateChanged, signInAnonymously,
} from "https://www.gstatic.com/firebasejs/12.19.0/firebase-auth.js";
import {
  collection, connectFirestoreEmulator, deleteDoc, doc, getFirestore, onSnapshot, query, serverTimestamp, setDoc, where,
} from "https://www.gstatic.com/firebasejs/12.19.0/firebase-firestore.js";
import {
  connectStorageEmulator, getDownloadURL, getStorage, ref, uploadBytes,
} from "https://www.gstatic.com/firebasejs/12.19.0/firebase-storage.js";
import { emulatorHost, emulatorPorts, firebaseConfig, useEmulators } from "./firebase-config.js";

const app = initializeApp(firebaseConfig);
const auth = getAuth(app);
const db = getFirestore(app);
const storage = getStorage(app);
if (useEmulators) {
  connectAuthEmulator(auth, `http://${emulatorHost}:${emulatorPorts.auth}`, { disableWarnings: true });
  connectFirestoreEmulator(db, emulatorHost, emulatorPorts.firestore);
  connectStorageEmulator(storage, emulatorHost, emulatorPorts.storage);
}

const VIEWS = ["front", "left", "back", "right"];
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const kb = (n) => (n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.ceil(n / 1024) + " KB");
const msg = (text, cls = "") => { $("msg").textContent = text; $("msg").className = cls; };

$("mode").textContent = useEmulators
  ? `Local emulators on ${emulatorHost} (nothing leaves this network)`
  : `Live Firebase project ${firebaseConfig.projectId}`;

// --- Sign in ---------------------------------------------------------------------------------------
// Anonymous sign-in gives this browser a stable user id (kept across reloads), with no account to make.
// On the real project, Anonymous sign-in must be turned on in the console (see firebase/README.md).
let user = null;
onAuthStateChanged(auth, (u) => {
  user = u;
  if (u) {
    $("submit").disabled = false;
    watchMyJobs(u.uid);
  }
});
signInAnonymously(auth).catch((err) => {
  msg(`Couldn't sign in (${err.code}). ${useEmulators ? "Are the emulators running?" : "Is Anonymous sign-in turned on?"}`, "bad");
});

// --- Upload ------------------------------------------------------------------------------------------
$("photo-front").addEventListener("change", () => {
  const file = $("photo-front").files[0];
  if (!file) return;
  $("preview").src = URL.createObjectURL(file);
  $("preview").style.display = "block";
});

$("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const photos = VIEWS.map((v) => [v, $(`photo-${v}`).files[0]]).filter(([, f]) => f);
  if (!photos.length || photos[0][0] !== "front") return msg("Take the front photo first.", "bad");
  const tooBig = photos.find(([, f]) => f.size >= 15 * 1024 * 1024);
  if (tooBig) return msg(`The ${tooBig[0]} photo is over 15 MB.`, "bad");

  $("submit").disabled = true;
  try {
    // A new document reference gives us a fresh job id before anything is written
    const jobRef = doc(collection(db, "jobs"));
    // Photos first: the backend starts as soon as the document exists, so they must already be there
    for (const [i, [view, file]] of photos.entries()) {
      msg(`Uploading ${view} photo (${i + 1} of ${photos.length})…`);
      await uploadBytes(ref(storage, `uploads/${jobRef.id}/${view}.jpg`), file, { contentType: file.type || "image/jpeg" });
    }
    // Every field here is checked by firestore.rules; adding a field means adding it there too
    await setDoc(jobRef, {
      status: "uploaded",
      owner: user.uid,
      category: $("category").value,
      force: $("force").checked,
      views: photos.map(([v]) => v),
      originalName: photos[0][1].name || "photo.jpg",
      createdAt: serverTimestamp(),
      source: "capture-app",
    });
    msg("Sent! Your job is in the list below.", "good");
    $("form").reset();
    $("preview").style.display = "none";
  } catch (err) {
    console.error(err);
    msg(`Upload failed: ${err.code || err.message}`, "bad");
  } finally {
    $("submit").disabled = false;
  }
});

// --- Job list ----------------------------------------------------------------------------------------
// Only this browser's jobs. (Sorting here instead of in the query means Firestore needs no extra index.)
let unsubscribe = null;
function watchMyJobs(uid) {
  if (unsubscribe) unsubscribe();
  const mine = query(collection(db, "jobs"), where("owner", "==", uid));
  unsubscribe = onSnapshot(mine, (snap) => {
    const jobs = snap.docs.map((d) => ({ id: d.id, ...d.data() }));
    jobs.sort((a, b) => (b.createdAt?.toMillis?.() ?? Date.now()) - (a.createdAt?.toMillis?.() ?? Date.now()));
    render(jobs.slice(0, 20));
  }, (err) => msg(`Can't load jobs: ${err.code}`, "bad"));
}

const WAITING = {
  uploaded: "Waiting for the backend (is it running with FIREBASE_PROJECT_ID set?)",
  queued: "Queued", classifying: "Working out the category", generating: "Generating the 3D model",
  rigging: "Adding a skeleton", downloading: "Saving files",
};
const urlCache = new Map(); // Storage path -> download URL

async function fileUrl(path) {
  if (!urlCache.has(path)) urlCache.set(path, getDownloadURL(ref(storage, path)));
  return urlCache.get(path);
}

function render(jobs) {
  const box = $("jobs");
  if (!jobs.length) {
    box.innerHTML = '<div class="muted">No jobs yet.</div>';
    return;
  }
  if (box.firstElementChild?.classList.contains("muted")) box.innerHTML = "";
  const ids = new Set(jobs.map((j) => `job-${j.id}`));
  for (const el of [...box.children]) if (!ids.has(el.id)) el.remove();

  for (const j of [...jobs].reverse()) {
    // Re-render only changed jobs, so the 3D previews don't reload on every progress tick
    const key = `${j.status}|${j.progress}|${j.rigStatus}|${j.error}`;
    let el = $(`job-${j.id}`);
    if (el && el.dataset.key === key) continue;
    const fresh = document.createElement("div");
    fresh.className = "job";
    fresh.id = `job-${j.id}`;
    fresh.dataset.key = key;
    fresh.innerHTML = jobHtml(j);
    if (el) el.replaceWith(fresh); else box.prepend(fresh);
    if (j.status === "done") fillFiles(fresh, j);
  }
}

function jobHtml(j) {
  const cat = j.category && j.category !== "auto" ? esc(j.category) : esc(j.requestedCategory || j.category);
  const angles = (j.views || []).length > 1 ? ` · ${j.views.length} angles` : "";
  const rig = j.rigStatus ? ` · rig: ${esc(j.rigStatus)}` : "";
  const state = WAITING[j.status] ? `<div class="muted">${WAITING[j.status]}…</div>` : "";
  const err = j.error ? `<div class="bad">${esc(j.error)}</div>` : "";
  const finished = ["done", "failed", "rejected"].includes(j.status);
  const remove = finished ? `<button class="ghost" data-delete="${j.id}">Remove</button>` : "";
  return `
    <div><strong>${esc(j.status)}</strong> · ${cat}${angles}${rig} <span class="muted">· ${esc(j.id)}</span></div>
    <div class="bar"><div style="width:${j.progress || 0}%"></div></div>
    ${state}${err}
    <div class="files"></div>
    <div class="viewer"></div>
    ${remove}`;
}

async function fillFiles(el, j) {
  const files = (j.files || []).filter((f) => f.path);
  const links = await Promise.all(files.map(async (f) => `<a href="${await fileUrl(f.path)}" target="_blank" rel="noopener">${esc(f.name)} (${kb(f.size)})</a>`));
  el.querySelector(".files").innerHTML = links.join("");
  const glb = files.find((f) => f.name === "rigged.glb") || files.find((f) => f.name === "model.glb");
  if (glb) {
    el.querySelector(".viewer").innerHTML =
      `<model-viewer src="${await fileUrl(glb.path)}" camera-controls auto-rotate shadow-intensity="1" alt="Generated model"></model-viewer>`;
  }
}

$("jobs").addEventListener("click", async (e) => {
  const id = e.target.dataset?.delete;
  if (!id) return;
  // Firestore rules let you remove your own finished jobs; the backend keeps its own copy
  await deleteDoc(doc(db, "jobs", id)).catch((err) => msg(`Couldn't remove: ${err.code}`, "bad"));
});
