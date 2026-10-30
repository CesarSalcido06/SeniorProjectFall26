"""End-to-end API tests against the offline Meshy mock. Run from backend/: python3 -m pytest -q"""
import io
import random
import struct
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageFilter

from app.classifier import parse_category
from app.config import Settings
from app.main import create_app
from app.meshy import MockMeshy, cube_glb


def photo_bytes(blur: float = 0, size=(800, 600), brightness: int | None = None) -> bytes:
    # Random blocks give lots of hard edges, like a sharp real photo
    rng = random.Random(1)
    img = Image.new("RGB", size)
    for x in range(0, size[0], 20):
        for y in range(0, size[1], 20):
            shade = brightness if brightness is not None else rng.randint(30, 220)
            img.paste((shade, rng.randint(30, 220) if brightness is None else shade, shade), (x, y, x + 20, y + 20))
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    out = io.BytesIO()
    img.save(out, "JPEG")
    return out.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CLASSIFIER", "manual")
    monkeypatch.setenv("MESHY_POLL_SECONDS", "0.05")
    settings = Settings()
    with TestClient(create_app(settings, meshy=MockMeshy(seconds=0.2))) as c:
        yield c


def upload(client, category="auto", data=None, force=False, views=None):
    files = {"photo": ("mug.jpg", data or photo_bytes(), "image/jpeg")}
    for view, view_data in (views or {}).items():
        files[view] = (f"{view}.jpg", view_data, "image/jpeg")
    return client.post("/api/jobs", files=files, data={"category": category, "force": str(force).lower()})


def wait_done(client, job_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job stuck: {job}")


def test_prop_job_end_to_end(client):
    res = upload(client, "prop")
    assert res.status_code == 201, res.text
    job = wait_done(client, res.json()["id"])
    assert job["status"] == "done", job
    assert job["category"] == "prop" and job["category_source"] == "user"
    assert job["rig_status"] == "skipped"
    assert set(job["files"]) == {"photo.jpg", "model.glb", "thumbnail.png"}

    glb = client.get(job["files"]["model.glb"]["url"])
    assert glb.status_code == 200
    assert glb.content[:4] == b"glTF" and struct.unpack("<I", glb.content[8:12])[0] == len(glb.content)


def test_character_gets_rigged(client):
    job = wait_done(client, upload(client, "character").json()["id"])
    assert job["status"] == "done"
    assert job["rig_status"] == "rigged"
    assert "rigged.glb" in job["files"]


def test_creature_is_not_rigged(client):
    job = wait_done(client, upload(client, "creature").json()["id"])
    assert job["rig_status"] == "skipped"


def test_auto_without_classifier_defaults_to_prop(client):
    job = wait_done(client, upload(client, "auto").json()["id"])
    assert job["category"] == "prop" and job["category_source"] == "default"


def test_blurry_photo_rejected_unless_forced(client):
    blurry = photo_bytes(blur=8)
    res = upload(client, data=blurry)
    assert res.status_code == 422
    assert "blurry" in res.json()["detail"]
    assert upload(client, data=blurry, force=True).status_code == 201


def test_dark_photo_rejected(client):
    res = upload(client, data=photo_bytes(brightness=8), force=False)
    assert res.status_code == 422


def test_not_an_image_rejected_even_when_forced(client):
    res = upload(client, data=b"definitely not a jpeg", force=True)
    assert res.status_code == 422


def test_small_photo_rejected(client):
    assert upload(client, data=photo_bytes(size=(300, 200)), force=True).status_code == 422


def test_bad_category(client):
    assert upload(client, "vehicle").status_code == 422


def test_file_access_is_restricted(client):
    job = wait_done(client, upload(client, "prop").json()["id"])
    assert client.get(f"/api/jobs/{job['id']}/files/..%2Fjobs.db").status_code == 404
    assert client.get(f"/api/jobs/{job['id']}/files/rigged.glb").status_code == 404
    assert client.get("/api/jobs/nope/files/model.glb").status_code == 404


def test_delete_and_list(client):
    job = wait_done(client, upload(client, "prop").json()["id"])
    assert any(j["id"] == job["id"] for j in client.get("/api/jobs").json())
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 204
    assert client.get(f"/api/jobs/{job['id']}").status_code == 404


def test_parse_category():
    assert parse_category("Character.") == "character"
    assert parse_category("I think this is a creature") == "creature"
    assert parse_category("a vehicle") is None


def test_extra_views_are_stored(client):
    res = upload(client, "prop", views={"back": photo_bytes(), "left": photo_bytes()})
    assert res.status_code == 201, res.text
    job = wait_done(client, res.json()["id"])
    assert job["status"] == "done", job
    photos = {n: f["view"] for n, f in job["files"].items() if f["kind"] == "photo"}
    assert photos == {"photo.jpg": "front", "photo_back.jpg": "back", "photo_left.jpg": "left"}
    assert job["files"]["photo_back.jpg"]["quality"]["sharpness"] > 100


def test_bad_extra_view_rejected_and_named(client):
    res = upload(client, "prop", views={"right": photo_bytes(blur=8)})
    assert res.status_code == 422
    assert res.json()["detail"].startswith("Right photo:") and res.json()["view"] == "right"
    assert client.get("/api/jobs").json() == []


def test_empty_extra_view_is_ignored(client):
    files = {"photo": ("mug.jpg", photo_bytes(), "image/jpeg"), "back": ("", b"", "application/octet-stream")}
    res = client.post("/api/jobs", files=files, data={"category": "prop"})
    assert res.status_code == 201, res.text
    assert set(res.json()["files"]) == {"photo.jpg"}


def test_cube_glb_is_valid():
    data = cube_glb()
    magic, version, length = struct.unpack("<III", data[:12])
    assert (magic, version, length) == (0x46546C67, 2, len(data))


def fake_generator_transport(fail=False, rig=True, requests=None):
    """Stands in for generator/server.py. Task bodies it receives are appended to `requests`."""
    import json

    import httpx

    state = {"polls": 0}
    done = {"status": "SUCCEEDED", "progress": 100, "error": None}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/tasks":
            if requests is not None:
                requests.append(json.loads(request.content))
            return httpx.Response(201, json={"result": "gen123"})
        if request.method == "POST" and path == "/tasks/gen123/rig":
            return httpx.Response(201, json={"result": "rig456"}) if rig else httpx.Response(501, text="UniRig isn't installed")
        if path == "/tasks/gen123":
            state["polls"] += 1
            if fail:
                return httpx.Response(200, json={"status": "FAILED", "progress": 30, "error": "CUDA out of memory"})
            if state["polls"] < 2:
                return httpx.Response(200, json={"status": "IN_PROGRESS", "progress": 50, "error": None})
            return httpx.Response(200, json={**done, "files": ["model.glb", "cutout.png", "model.fbx"]})
        if path == "/tasks/rig456":
            return httpx.Response(200, json={**done, "files": ["rigged.glb", "rigged.fbx"]})
        if path.endswith(".glb") or path.endswith(".fbx"):
            return httpx.Response(200, content=cube_glb())
        if path == "/tasks/gen123/files/cutout.png":
            return httpx.Response(200, content=photo_bytes())
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def local_client(tmp_path, monkeypatch, request):
    from app.meshy import LocalGeneratorClient

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CLASSIFIER", "manual")
    monkeypatch.setenv("MESHY_POLL_SECONDS", "0.05")
    opts = getattr(request, "param", {})
    gen = LocalGeneratorClient("http://gen.test", transport=fake_generator_transport(**opts))
    with TestClient(create_app(Settings(), meshy=gen)) as c:
        yield c


def test_local_generator_prop(local_client):
    job = wait_done(local_client, upload(local_client, "prop").json()["id"])
    assert job["status"] == "done", job
    assert job["rig_status"] == "skipped"
    assert set(job["files"]) == {"photo.jpg", "model.glb", "model.fbx", "thumbnail.png"}


@pytest.mark.parametrize("category", ["character", "creature"])
def test_local_generator_rigs_characters_and_creatures(local_client, category):
    job = wait_done(local_client, upload(local_client, category).json()["id"])
    assert job["status"] == "done", job
    assert job["rig_status"] == "rigged"
    assert {"rigged.glb", "rigged.fbx", "model.glb"} <= set(job["files"])


@pytest.mark.parametrize("local_client", [{"rig": False}], indirect=True)
def test_local_generator_rig_unavailable_keeps_model(local_client):
    job = wait_done(local_client, upload(local_client, "character").json()["id"])
    assert job["status"] == "done"
    assert job["rig_status"].startswith("failed: Local generator refused rigging")
    assert "model.glb" in job["files"] and "rigged.glb" not in job["files"]


@pytest.mark.parametrize("local_client", [{"fail": True}], indirect=True)
def test_local_generator_failure_is_reported(local_client):
    job = wait_done(local_client, upload(local_client, "prop").json()["id"])
    assert job["status"] == "failed"
    assert "CUDA out of memory" in job["error"]


def test_local_generator_gets_extra_views(tmp_path, monkeypatch):
    from app.meshy import LocalGeneratorClient

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CLASSIFIER", "manual")
    monkeypatch.setenv("MESHY_POLL_SECONDS", "0.05")
    sent = []
    gen = LocalGeneratorClient("http://gen.test", transport=fake_generator_transport(requests=sent))
    with TestClient(create_app(Settings(), meshy=gen)) as c:
        job = wait_done(c, upload(c, "prop", views={"back": photo_bytes()}).json()["id"])
    assert job["status"] == "done", job
    assert set(sent[0]["views"]) == {"back"}
    assert sent[0]["views"]["back"].startswith("data:image/jpeg;base64,")
    assert "photo_back.jpg" in job["files"]


def test_generator_setting_validated(monkeypatch):
    monkeypatch.setenv("GENERATOR", "blender")
    with pytest.raises(ValueError):
        Settings()
