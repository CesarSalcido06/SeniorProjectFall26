"""Meshy API client (image -> textured mesh -> humanoid rig) plus an offline mock with the same interface.

API docs: https://docs.meshy.ai/en/api/image-to-3d and https://docs.meshy.ai/en/api/rigging-and-animation
"""
import io
import json
import struct
import time
import uuid

import httpx
from PIL import Image

# Meshy generation settings per category. Characters get an A-pose so they rig cleanly.
# Which categories get rigged depends on the generator: see each client's `rig_categories`.
PROFILES = {
    "prop": {"pose_mode": ""},
    "character": {"pose_mode": "a-pose"},
    "creature": {"pose_mode": ""},
}


def generation_body(image_uri: str, category: str) -> dict:
    return {
        "image_url": image_uri,
        "ai_model": "latest",
        "should_texture": True,
        "enable_pbr": True,
        "should_remesh": True,
        "topology": "triangle",
        "target_polycount": 30000,  # game-friendly, and well under the rigger's 320k face limit
        "pose_mode": PROFILES[category]["pose_mode"],
        "target_formats": ["glb", "fbx"],
    }


class MeshyError(RuntimeError):
    pass


class MeshyClient:
    rig_categories = frozenset({"character"})  # Meshy's auto-rig only supports humanoids

    def __init__(self, api_key: str, base_url: str = "https://api.meshy.ai") -> None:
        if not api_key:
            raise MeshyError("MESHY_API_KEY is not set.")
        self._client = httpx.AsyncClient(
            base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=60
        )

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        resp = await self._client.request(method, path, **kwargs)
        if resp.status_code >= 400:
            try:
                message = resp.json().get("message", resp.text)
            except ValueError:
                message = resp.text
            raise MeshyError(f"Meshy {method} {path} -> HTTP {resp.status_code}: {message[:300]}")
        return resp.json()

    async def create_image_to_3d(self, image_uri: str, category: str, extra_views: dict | None = None) -> str:
        # Only the front photo is sent: Meshy's multi-image endpoint is a separate task type that we
        # haven't wired up (or been able to test without a paid key)
        data = await self._request("POST", "/openapi/v1/image-to-3d", json=generation_body(image_uri, category))
        return data["result"]

    async def get_image_to_3d(self, task_id: str) -> dict:
        return await self._request("GET", f"/openapi/v1/image-to-3d/{task_id}")

    async def create_rigging(self, input_task_id: str, height_meters: float = 1.7) -> str:
        body = {"input_task_id": input_task_id, "height_meters": height_meters}
        data = await self._request("POST", "/openapi/v1/rigging", json=body)
        return data["result"]

    async def get_rigging(self, task_id: str) -> dict:
        return await self._request("GET", f"/openapi/v1/rigging/{task_id}")

    async def download(self, url: str) -> bytes:
        # Asset URLs are signed and expire, so jobs keep their own copy
        async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content


# ---------------------------------------------------------------------------
# Offline mock: same calls, no network, no credits. Produces a colored cube GLB.
# ---------------------------------------------------------------------------

_MOCK_COLORS = {"prop": (0.85, 0.55, 0.2), "character": (0.2, 0.55, 0.95), "creature": (0.3, 0.8, 0.4)}


def cube_glb(color: tuple[float, float, float] = (0.8, 0.8, 0.8)) -> bytes:
    """A 1m cube with normals, as a valid binary glTF 2.0 file (opens in three.js, Blender, Unity glTFast)."""
    faces = [  # normal, then the two axes spanning that face
        ((1, 0, 0), (0, 1, 0), (0, 0, 1)), ((-1, 0, 0), (0, 0, 1), (0, 1, 0)),
        ((0, 1, 0), (0, 0, 1), (1, 0, 0)), ((0, -1, 0), (1, 0, 0), (0, 0, 1)),
        ((0, 0, 1), (1, 0, 0), (0, 1, 0)), ((0, 0, -1), (0, 1, 0), (1, 0, 0)),
    ]
    positions, normals, indices = [], [], []
    for n, u, v in faces:
        base = len(positions)
        for su, sv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            positions.append(tuple(0.5 * (n[i] + su * u[i] + sv * v[i]) for i in range(3)))
            normals.append(n)
        indices += [base, base + 1, base + 2, base, base + 2, base + 3]

    pos_bytes = b"".join(struct.pack("<3f", *p) for p in positions)
    nrm_bytes = b"".join(struct.pack("<3f", *n) for n in normals)
    idx_bytes = struct.pack(f"<{len(indices)}H", *indices)
    binary = pos_bytes + nrm_bytes + idx_bytes
    binary += b"\0" * (-len(binary) % 4)

    gltf = {
        "asset": {"version": "2.0", "generator": "photo-to-asset mock"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": "MockAsset"}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "NORMAL": 1}, "indices": 2, "material": 0}]}],
        "materials": [{"pbrMetallicRoughness": {"baseColorFactor": [*color, 1.0], "metallicFactor": 0.1, "roughnessFactor": 0.6}}],
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(pos_bytes), "target": 34962},
            {"buffer": 0, "byteOffset": len(pos_bytes), "byteLength": len(nrm_bytes), "target": 34962},
            {"buffer": 0, "byteOffset": len(pos_bytes) + len(nrm_bytes), "byteLength": len(idx_bytes), "target": 34963},
        ],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": len(positions), "type": "VEC3", "min": [-0.5] * 3, "max": [0.5] * 3},
            {"bufferView": 1, "componentType": 5126, "count": len(normals), "type": "VEC3"},
            {"bufferView": 2, "componentType": 5123, "count": len(indices), "type": "SCALAR"},
        ],
    }
    json_bytes = json.dumps(gltf, separators=(",", ":")).encode()
    json_bytes += b" " * (-len(json_bytes) % 4)
    total = 12 + 8 + len(json_bytes) + 8 + len(binary)
    return (
        struct.pack("<III", 0x46546C67, 2, total)
        + struct.pack("<II", len(json_bytes), 0x4E4F534A) + json_bytes
        + struct.pack("<II", len(binary), 0x004E4942) + binary
    )


def _thumbnail(color: tuple[float, float, float]) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (256, 256), tuple(int(c * 255) for c in color)).save(out, "PNG")
    return out.getvalue()


class MockMeshy:
    """Pretends each task takes `seconds` to finish. Files are generated on download."""

    rig_categories = frozenset({"character"})

    def __init__(self, seconds: float = 6) -> None:
        self.seconds = seconds
        self._tasks: dict[str, dict] = {}

    def _progress(self, task: dict) -> dict:
        elapsed = time.monotonic() - task["started"]
        if self.seconds <= 0 or elapsed >= self.seconds:
            return {"status": "SUCCEEDED", "progress": 100}
        return {"status": "IN_PROGRESS", "progress": int(100 * elapsed / self.seconds)}

    async def create_image_to_3d(self, image_uri: str, category: str, extra_views: dict | None = None) -> str:
        task_id = "mock-" + uuid.uuid4().hex[:8]
        self._tasks[task_id] = {"started": time.monotonic(), "category": category}
        return task_id

    async def get_image_to_3d(self, task_id: str) -> dict:
        task = self._tasks.get(task_id)
        if task is None:  # e.g. server restarted mid-job; mock tasks live in memory
            raise MeshyError(f"Unknown mock task {task_id}")
        state = self._progress(task)
        if state["status"] == "SUCCEEDED":
            c = task["category"]
            state["model_urls"] = {"glb": f"mock://{c}/model.glb"}
            state["thumbnail_url"] = f"mock://{c}/thumbnail.png"
        return state

    async def create_scan(self, photos: list, longest_mm: float | None, ai_depth: bool = True, ai_fill: bool = True) -> str:
        task_id = "mock-scan-" + uuid.uuid4().hex[:8]
        self._tasks[task_id] = {
            "started": time.monotonic(), "category": "prop", "longest_mm": longest_mm, "ai_depth": ai_depth, "ai_fill": ai_fill,
        }
        return task_id

    async def get_scan(self, task_id: str) -> dict:
        task = self._tasks.get(task_id)
        if task is None:
            raise MeshyError(f"Unknown mock task {task_id}")
        state = {**self._progress(task), "kind": "scan", "stage": "pretending"}
        if state["status"] == "SUCCEEDED":
            longest = task["longest_mm"]
            state["model_urls"] = {"glb": "mock://prop/model.glb"}
            state["report"] = {
                "photos_uploaded": 8, "photos_used": 8, "size_mm": [longest, longest, longest] if longest else None,
                "proportions": [1, 1, 1], "warnings": [],
                "surface_pct": {"measured": 100 if not task["ai_fill"] else 70, "ai_depth": 0, "ai_fill": 0 if not task["ai_fill"] else 30, "smoothed": 0},
            }
            if task["ai_depth"] or task["ai_fill"]:
                state["extra_models"] = {"model_sources.glb": "mock://prop/model.glb", "model_measured.glb": "mock://prop/model.glb"}
        return state

    async def create_rigging(self, input_task_id: str, height_meters: float = 1.7) -> str:
        task_id = "mock-rig-" + uuid.uuid4().hex[:8]
        self._tasks[task_id] = {"started": time.monotonic(), "category": "character"}
        return task_id

    async def get_rigging(self, task_id: str) -> dict:
        task = self._tasks.get(task_id)
        if task is None:
            raise MeshyError(f"Unknown mock task {task_id}")
        state = self._progress(task)
        if state["status"] == "SUCCEEDED":
            # The mock "rig" is the plain cube: it has no skeleton
            state["result"] = {"rigged_character_glb_url": "mock://character/model.glb"}
        return state

    async def download(self, url: str) -> bytes:
        category, name = url.removeprefix("mock://").split("/", 1)
        color = _MOCK_COLORS.get(category, (0.8, 0.8, 0.8))
        return cube_glb(color) if name.endswith(".glb") else _thumbnail(color)


# ---------------------------------------------------------------------------
# Our own generator (generator/server.py, Hunyuan3D on the local GPU)
# ---------------------------------------------------------------------------


class LocalGeneratorClient:
    """Speaks to generator/server.py and returns Meshy-shaped task dicts, so the worker doesn't care which it is."""

    # UniRig predicts a skeleton for any body plan, so creatures get rigged too
    rig_categories = frozenset({"character", "creature"})

    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=60, transport=transport)

    async def _get(self, path: str) -> dict:
        try:
            resp = await self._client.get(path)
        except httpx.TransportError as exc:
            raise MeshyError(f"Local generator unreachable at {self.base_url} ({type(exc).__name__}). Is it running?")
        if resp.status_code == 404:
            # Tasks live in the generator's memory; a restart forgets them
            raise MeshyError("Local generator lost this task (it was restarted). Retry the job.")
        resp.raise_for_status()
        return resp.json()

    async def create_image_to_3d(self, image_uri: str, category: str, extra_views: dict | None = None) -> str:
        """extra_views: {"left"|"back"|"right": data URI}. With any of them the generator uses its multi-view model."""
        body = {"image_url": image_uri, "category": category, "texture": True, "views": extra_views or {}}
        try:
            resp = await self._client.post("/tasks", json=body)
        except httpx.TransportError as exc:
            raise MeshyError(f"Local generator unreachable at {self.base_url} ({type(exc).__name__}). Is it running?")
        resp.raise_for_status()
        return resp.json()["result"]

    def _files(self, task_id: str, task: dict) -> dict:
        return {name: f"{self.base_url}/tasks/{task_id}/files/{name}" for name in task.get("files", [])}

    async def get_image_to_3d(self, task_id: str) -> dict:
        task = await self._get(f"/tasks/{task_id}")
        if task["status"] == "SUCCEEDED":
            files = self._files(task_id, task)
            task["model_urls"] = {fmt: files[f"model.{fmt}"] for fmt in ("glb", "fbx") if f"model.{fmt}" in files}
            task["thumbnail_url"] = files.get("cutout.png")
        if task.get("error"):
            task["task_error"] = {"message": task["error"]}
        return task

    async def create_scan(self, photos: list, longest_mm: float | None, ai_depth: bool = True, ai_fill: bool = True) -> str:
        """photos: paths of the JPEGs on disk. Sent as a form, since dozens of photos are too big for JSON."""
        files = [("photos", (path.name, path.read_bytes(), "image/jpeg")) for path in photos]
        data = {"ai_depth": str(ai_depth).lower(), "ai_fill": str(ai_fill).lower()}
        if longest_mm:
            data["longest_mm"] = str(longest_mm)
        try:
            resp = await self._client.post("/scans", files=files, data=data, timeout=300)
        except httpx.TransportError as exc:
            raise MeshyError(f"Local generator unreachable at {self.base_url} ({type(exc).__name__}). Is it running?")
        if resp.status_code >= 400:
            raise MeshyError(f"Local generator refused the scan: HTTP {resp.status_code} {resp.text[:200]}")
        return resp.json()["result"]

    async def get_scan(self, task_id: str) -> dict:
        task = await self._get(f"/tasks/{task_id}")
        if task["status"] == "SUCCEEDED":
            files = self._files(task_id, task)
            task["model_urls"] = {fmt: files[f"model.{fmt}"] for fmt in ("glb", "fbx") if f"model.{fmt}" in files}
            task["extra_models"] = {name: files[name] for name in ("model_sources.glb", "model_measured.glb") if name in files}
        if task.get("error"):
            task["task_error"] = {"message": task["error"]}
        return task

    async def create_rigging(self, input_task_id: str, height_meters: float = 1.7) -> str:
        try:
            resp = await self._client.post(f"/tasks/{input_task_id}/rig")
        except httpx.TransportError as exc:
            raise MeshyError(f"Local generator unreachable at {self.base_url} ({type(exc).__name__}). Is it running?")
        if resp.status_code >= 400:
            raise MeshyError(f"Local generator refused rigging: HTTP {resp.status_code} {resp.text[:200]}")
        return resp.json()["result"]

    async def get_rigging(self, task_id: str) -> dict:
        task = await self._get(f"/tasks/{task_id}")
        if task["status"] == "SUCCEEDED":
            files = self._files(task_id, task)
            task["result"] = {
                "rigged_character_glb_url": files.get("rigged.glb"),
                "rigged_character_fbx_url": files.get("rigged.fbx"),
            }
        if task.get("error"):
            task["task_error"] = {"message": task["error"]}
        return task

    async def download(self, url: str) -> bytes:
        resp = await self._client.get(url, timeout=300)
        resp.raise_for_status()
        return resp.content


def make_client(settings):
    if settings.generator == "mock":
        return MockMeshy(settings.mock_seconds)
    if settings.generator == "local":
        return LocalGeneratorClient(settings.generator_url)
    return MeshyClient(settings.meshy_api_key, settings.meshy_base_url)
