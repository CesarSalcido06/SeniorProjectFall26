"""Links the backend to Firebase, so apps can use Firebase instead of calling this API directly.

Two directions:
  - Intake: the capture app (firebase/public/capture/) uploads photos to Storage under uploads/{jobId}/
    and creates Firestore jobs/{jobId} with status "uploaded". We claim it, check the photos with the same
    quality gate as POST /api/jobs, and run it like any other job (same id in both places).
  - Mirror: every job, however it arrived, is copied to Firestore jobs/{jobId} as it changes, and its
    finished files are uploaded to Storage under jobs/{jobId}/. Apps watch the document for progress.

Firestore document jobs/{jobId}, camelCase for JavaScript:
    status          uploaded | rejected (Firebase only) | queued | classifying | generating | rigging |
                    downloading | done | failed
    progress        0-100
    category, requestedCategory, categorySource, classifierNote, rigStatus, error, quality
    files           [{name, kind, size, view?, path}] where path is the Storage path, e.g. jobs/<id>/model.glb
    views, owner, force, originalName, source ("capture-app" or "api"), createdAt, updatedAt

Firebase calls are blocking, so they run on two background threads and never slow the worker down.
Turn it on with FIREBASE_PROJECT_ID (see backend/.env.example and firebase/README.md).
"""
import mimetypes
import os
import queue
import socket
import threading
import time
import traceback
from datetime import datetime, timezone

from . import intake

COLLECTION = "jobs"
RETRY_SECONDS = 5
# Fields copied from the job store to Firestore (store name -> Firestore name)
_MIRRORED = {
    "status": "status",
    "progress": "progress",
    "requested_category": "requestedCategory",
    "category": "category",
    "category_source": "categorySource",
    "classifier_note": "classifierNote",
    "rig_status": "rigStatus",
    "error": "error",
    "quality": "quality",
    "original_name": "originalName",
}
mimetypes.add_type("model/gltf-binary", ".glb")
mimetypes.add_type("application/octet-stream", ".fbx")


def _connect(settings):
    """Returns (firestore client, storage bucket). Imported lazily: firebase-admin is only needed when enabled."""
    import firebase_admin
    from firebase_admin import credentials, firestore, storage

    if settings.firebase_emulators:
        # Both Google client libraries check these variables and then talk to the emulators instead
        os.environ["FIRESTORE_EMULATOR_HOST"] = settings.firestore_emulator_host
        os.environ["FIREBASE_STORAGE_EMULATOR_HOST"] = settings.storage_emulator_host
        os.environ["STORAGE_EMULATOR_HOST"] = f"http://{settings.storage_emulator_host}"
        import google.auth.credentials

        class EmulatorCredential(credentials.Base):
            # The emulators accept anyone, but the SDK still insists on a credential object
            def get_credential(self):
                return google.auth.credentials.AnonymousCredentials()

        cred = EmulatorCredential()
    elif settings.firebase_credentials:
        cred = credentials.Certificate(settings.firebase_credentials)
    else:
        cred = credentials.ApplicationDefault()  # e.g. after `gcloud auth application-default login`

    options = {"projectId": settings.firebase_project_id, "storageBucket": settings.firebase_storage_bucket}
    try:
        app = firebase_admin.get_app("photo-to-asset")
    except ValueError:
        app = firebase_admin.initialize_app(cred, options, name="photo-to-asset")
    return firestore.client(app), storage.bucket(app=app)


def _timestamp(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


class FirebaseSync:
    def __init__(self, settings, store, worker) -> None:
        self.settings, self.store, self.worker = settings, store, worker
        self.db = self.bucket = None
        self._loop = None
        self._watch = None
        self._changes: "queue.Queue[str | None]" = queue.Queue()
        self._pending: dict[str, dict | None] = {}  # job id -> latest state (None = deleted)
        self._pending_lock = threading.Lock()
        self._uploaded: dict[str, set[str]] = {}  # job id -> files already in Storage
        self._known: set[str] = set()  # job ids whose Firestore document we've already checked for
        self._intake: "queue.Queue[str | None]" = queue.Queue()

    # --- lifecycle (called from main.py) ---

    def start(self, loop) -> None:
        from google.cloud.firestore_v1.base_query import FieldFilter

        self._loop = loop
        self.db, self.bucket = _connect(self.settings)
        self.store.listeners.append(self._on_job_changed)
        threading.Thread(target=self._mirror_loop, name="firebase-mirror", daemon=True).start()
        threading.Thread(target=self._intake_loop, name="firebase-intake", daemon=True).start()
        # Also picks up jobs created while the backend was off: the first snapshot includes every match
        query = self.db.collection(COLLECTION).where(filter=FieldFilter("status", "==", "uploaded"))
        self._watch = query.on_snapshot(self._on_uploaded)
        where = "emulators" if self.settings.firebase_emulators else "live project"
        print(f"Firebase: linked to {self.settings.firebase_project_id} ({where})", flush=True)

    def stop(self) -> None:
        if self._watch:
            self._watch.unsubscribe()
        self._changes.put(None)
        self._intake.put(None)

    # --- intake: Firestore -> backend ---

    def _on_uploaded(self, snapshots, changes, read_time) -> None:
        # Runs on Firestore's listener thread; hand off so this thread stays free
        for change in changes:
            if change.type.name == "ADDED":
                self._intake.put(change.document.id)

    def _intake_loop(self) -> None:
        while (job_id := self._intake.get()) is not None:
            try:
                self._take(job_id)
            except Exception as exc:
                traceback.print_exc()
                try:
                    self.db.collection(COLLECTION).document(job_id).update(
                        {"status": "failed", "error": f"Backend couldn't start this job: {type(exc).__name__}: {exc}"}
                    )
                except Exception:
                    traceback.print_exc()  # Firebase itself is unreachable; keep the thread alive

    def _take(self, job_id: str) -> None:
        from google.cloud import firestore

        ref = self.db.collection(COLLECTION).document(job_id)

        @firestore.transactional
        def claim(transaction):
            # Two backends watching one project must not both run the job
            snap = ref.get(transaction=transaction)
            if not snap.exists or snap.get("status") != "uploaded":
                return None
            transaction.update(ref, {"status": "queued", "claimedBy": socket.gethostname(),
                                     "updatedAt": firestore.SERVER_TIMESTAMP})
            return snap.to_dict()

        doc = claim(self.db.transaction())
        if doc is None or self.store.get(job_id):
            return

        raw = {}
        for view in doc.get("views") or ["front"]:
            blob = self.bucket.blob(f"uploads/{job_id}/{view}.jpg")
            if not blob.exists():
                ref.update({"status": "failed", "error": f"The {view} photo never reached Storage. Try again."})
                return
            raw[view] = blob.download_as_bytes()

        try:
            photos = intake.check_photos(raw, self.settings, bool(doc.get("force")))
        except intake.ViewRejected as exc:
            ref.update({"status": "rejected", "error": exc.detail, "rejectedView": exc.view, "quality": exc.quality,
                        "updatedAt": firestore.SERVER_TIMESTAMP})
            return

        category = doc.get("category") or "auto"
        intake.create_job(self.store, self.settings, photos, category, doc.get("originalName") or "photo", job_id=job_id)
        self._loop.call_soon_threadsafe(self.worker.submit, job_id)

    # --- mirror: backend -> Firestore + Storage ---

    def _on_job_changed(self, job_id: str, job: dict | None) -> None:
        # Called by the job store on whichever thread changed it. Keep only the latest state per job,
        # so a burst of progress updates becomes one Firestore write.
        with self._pending_lock:
            first = job_id not in self._pending
            self._pending[job_id] = job
        if first:
            self._changes.put(job_id)

    def _mirror_loop(self) -> None:
        while (job_id := self._changes.get()) is not None:
            with self._pending_lock:
                job = self._pending.pop(job_id, None)
            try:
                if job is None:
                    self._delete(job_id)
                else:
                    self._mirror(job)
            except Exception:
                # Firebase being down mustn't stop jobs. Try again shortly, unless a newer change is already
                # queued (it carries the latest state anyway); otherwise a job's final "done" could be lost.
                traceback.print_exc()
                time.sleep(RETRY_SECONDS)
                with self._pending_lock:
                    retry = job_id not in self._pending
                if retry:
                    self._on_job_changed(job_id, job)

    def _mirror(self, job: dict) -> None:
        from google.cloud import firestore

        job_id = job["id"]
        files = []
        for name, info in job["files"].items():
            path = f"jobs/{job_id}/{name}"
            # Upload once the job is done, so apps never see a half-written set of files
            if job["status"] == "done" and name not in self._uploaded.setdefault(job_id, set()):
                content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
                self.bucket.blob(path).upload_from_filename(str(self.settings.job_dir(job_id) / name),
                                                            content_type=content_type)
                self._uploaded[job_id].add(name)
            entry = {"name": name, "kind": info["kind"], "size": info["size"]}
            if "view" in info:
                entry["view"] = info["view"]
            if job["status"] == "done":
                entry["path"] = path
            files.append(entry)

        doc = {firebase_name: job.get(name) for name, firebase_name in _MIRRORED.items()}
        doc.update(files=files, createdAt=_timestamp(job["created_at"]), updatedAt=firestore.SERVER_TIMESTAMP,
                   views=[f["view"] for f in files if f["kind"] == "photo"])
        ref = self.db.collection(COLLECTION).document(job_id)
        if job_id not in self._known:
            if not ref.get(["source"]).exists:
                doc["source"] = "api"  # uploaded through POST /api/jobs rather than the capture app
            self._known.add(job_id)
        ref.set(doc, merge=True)  # merge keeps fields the capture app wrote (owner, force, ...)

    def _delete(self, job_id: str) -> None:
        # Files first, so once the document is gone, everything is
        for prefix in (f"jobs/{job_id}/", f"uploads/{job_id}/"):
            for blob in self.bucket.list_blobs(prefix=prefix):
                blob.delete()
        self.db.collection(COLLECTION).document(job_id).delete()
        self._uploaded.pop(job_id, None)
        self._known.discard(job_id)
