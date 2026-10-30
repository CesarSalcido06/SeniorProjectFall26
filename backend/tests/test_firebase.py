"""Firebase link tests, against the real Firebase emulators (no Firebase account needed).

Run from the repo root; this starts the emulators, runs the tests, and stops them:
    cd firebase && firebase emulators:exec --only firestore,storage "cd ../backend && python3 -m pytest -q tests/test_firebase.py"
Skipped when the emulators aren't running (FIRESTORE_EMULATOR_HOST unset), so plain `pytest` still works.
"""
import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.meshy import MockMeshy
from test_api import photo_bytes, upload

pytestmark = pytest.mark.skipif(
    not os.getenv("FIRESTORE_EMULATOR_HOST"), reason="needs the Firebase emulators (see module docstring)"
)

PROJECT = "senior-project-7c405"


@pytest.fixture
def linked(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CLASSIFIER", "manual")
    monkeypatch.setenv("MESHY_POLL_SECONDS", "0.05")
    monkeypatch.setenv("FIREBASE_PROJECT_ID", PROJECT)
    monkeypatch.setenv("FIREBASE_EMULATORS", "true")
    settings = Settings()
    app = create_app(settings, meshy=MockMeshy(seconds=0.2))
    with TestClient(app) as client:
        from app.firebase_sync import _connect

        db, bucket = _connect(settings)
        yield client, db, bucket


def wait_for(fn, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError("timed out")


def doc_when(db, job_id, *statuses):
    def check():
        snap = db.collection("jobs").document(job_id).get()
        return snap.to_dict() if snap.exists and snap.get("status") in statuses else None

    return wait_for(check)


def submit_like_capture_app(db, bucket, views: dict[str, bytes], category="prop", force=False) -> str:
    """Does what firebase/public/capture/app.js does: photos to Storage first, then the job document."""
    from google.cloud import firestore

    job_id = uuid.uuid4().hex[:12]
    for view, data in views.items():
        bucket.blob(f"uploads/{job_id}/{view}.jpg").upload_from_string(data, content_type="image/jpeg")
    db.collection("jobs").document(job_id).set({
        "status": "uploaded", "owner": "test-user", "category": category, "force": force,
        "views": list(views), "originalName": "mug.jpg", "createdAt": firestore.SERVER_TIMESTAMP,
        "source": "capture-app",
    })
    return job_id


def test_capture_app_job_runs_and_files_land_in_storage(linked):
    client, db, bucket = linked
    job_id = submit_like_capture_app(db, bucket, {"front": photo_bytes(), "back": photo_bytes()}, category="character")
    doc = doc_when(db, job_id, "done", "failed", "rejected")
    assert doc["status"] == "done", doc
    assert doc["source"] == "capture-app" and doc["owner"] == "test-user"  # kept from the app's write
    assert doc["category"] == "character" and doc["rigStatus"] == "rigged"
    assert doc["views"] == ["front", "back"]
    files = {f["name"]: f for f in doc["files"]}
    assert {"photo.jpg", "photo_back.jpg", "model.glb", "rigged.glb", "thumbnail.png"} <= set(files)
    glb = bucket.blob(files["model.glb"]["path"]).download_as_bytes()
    assert glb[:4] == b"glTF"
    # Same job in the backend's own API
    assert client.get(f"/api/jobs/{job_id}").json()["status"] == "done"


def test_bad_photo_from_capture_app_is_rejected(linked):
    _, db, bucket = linked
    job_id = submit_like_capture_app(db, bucket, {"front": photo_bytes(), "left": photo_bytes(blur=8)})
    doc = doc_when(db, job_id, "done", "failed", "rejected")
    assert doc["status"] == "rejected"
    assert doc["error"].startswith("Left photo:") and doc["rejectedView"] == "left"


def test_missing_photo_fails_clearly(linked):
    from google.cloud import firestore

    _, db, _ = linked
    job_id = uuid.uuid4().hex[:12]
    db.collection("jobs").document(job_id).set({
        "status": "uploaded", "owner": "u", "category": "prop", "force": False, "views": ["front"],
        "createdAt": firestore.SERVER_TIMESTAMP, "source": "capture-app",
    })
    doc = doc_when(db, job_id, "failed")
    assert "never reached Storage" in doc["error"]


def test_api_jobs_are_mirrored_and_deleted(linked):
    client, db, bucket = linked
    job_id = upload(client, "prop").json()["id"]
    doc = doc_when(db, job_id, "done")
    assert doc["source"] == "api" and doc["progress"] == 100
    assert bucket.blob(f"jobs/{job_id}/model.glb").exists()

    assert client.delete(f"/api/jobs/{job_id}").status_code == 204
    wait_for(lambda: not db.collection("jobs").document(job_id).get().exists)
    assert not bucket.blob(f"jobs/{job_id}/model.glb").exists()
