"""Local 3D generator service: photo -> textured GLB on this machine's GPU.

The backend talks to it the same way it talks to Meshy (create a task, poll it, download files).
Run from generator/: .venv/bin/uvicorn server:app --host 127.0.0.1 --port 8100
"""
import base64
import io
import json
import os
import queue
import subprocess
import threading
import time
import traceback
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import BaseModel

DATA_DIR = Path(os.getenv("GENERATOR_DATA_DIR", Path(__file__).parent / "data"))
ENGINE = os.getenv("GENERATOR_ENGINE", "hunyuan")
# The GPU is shared with BenchAI/MegaMan. These commands hand it over and back, e.g.
#   GPU_ACQUIRE_CMD="systemctl --user stop benchai-router"
#   GPU_RELEASE_CMD="systemctl --user start benchai-router"
GPU_ACQUIRE_CMD = os.getenv("GPU_ACQUIRE_CMD", "")
GPU_RELEASE_CMD = os.getenv("GPU_RELEASE_CMD", "")
# Unload models (and run GPU_RELEASE_CMD) after this many idle seconds
IDLE_SECONDS = float(os.getenv("GPU_IDLE_SECONDS", "120"))


from engines import scan, unirig  # noqa: E402


def make_engine(name: str):
    if name == "hunyuan":
        from engines.hunyuan import HunyuanEngine

        return HunyuanEngine(max_faces=int(os.getenv("MAX_FACES", "40000")))
    raise ValueError(f"Unknown GENERATOR_ENGINE {name!r}")


# Extra angles the multi-view model understands, beyond the front photo
EXTRA_VIEWS = ("left", "back", "right")


class TaskRequest(BaseModel):
    image_url: str  # front photo, data:image/...;base64,... (same field name as Meshy)
    category: str = "prop"
    texture: bool = True
    views: dict[str, str] = {}  # optional {"left"|"back"|"right": data URI}


def _decode(uri: str, what: str) -> bytes:
    header, _, b64 = uri.partition(",")
    if not header.startswith("data:image/") or not b64:
        raise HTTPException(400, f"{what} must be a base64 data URI")
    return base64.b64decode(b64)


class Generator:
    def __init__(self) -> None:
        self.engine = make_engine(ENGINE)
        self.tasks: dict[str, dict] = {}
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._rembg = None
        self._gpu_held = False
        self._last_used = time.monotonic()
        threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, req: TaskRequest) -> str:
        unknown = set(req.views) - set(EXTRA_VIEWS)
        if unknown:
            raise HTTPException(400, f"Unknown views {sorted(unknown)}; use {', '.join(EXTRA_VIEWS)}")
        inputs = {"front": _decode(req.image_url, "image_url")}
        inputs.update({view: _decode(uri, f"views.{view}") for view, uri in req.views.items()})
        task_id = uuid.uuid4().hex[:12]
        task_dir = DATA_DIR / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        for view, data in inputs.items():
            (task_dir / f"input_{view}").write_bytes(data)
        self.tasks[task_id] = {
            "id": task_id, "kind": "generate", "status": "PENDING", "progress": 0, "stage": "queued",
            "category": req.category, "texture": req.texture, "engine": self.engine.name,
            "views": list(inputs), "files": [],
            "error": None, "created_at": time.time(), "seconds": None,
        }
        self._queue.put(task_id)
        return task_id

    def submit_rig(self, source_id: str) -> str:
        source = self.tasks.get(source_id)
        if source is None or source["kind"] != "generate":
            raise HTTPException(404, "Unknown generation task")
        if source["status"] != "SUCCEEDED":
            raise HTTPException(409, "Generation task hasn't finished")
        if not unirig.available():
            raise HTTPException(501, "UniRig isn't installed (see generator/README.md)")
        task_id = uuid.uuid4().hex[:12]
        (DATA_DIR / task_id).mkdir(parents=True, exist_ok=True)
        self.tasks[task_id] = {
            "id": task_id, "kind": "rig", "source": source_id, "status": "PENDING", "progress": 0,
            "stage": "queued", "engine": "unirig", "files": [], "error": None, "created_at": time.time(),
            "seconds": None,
        }
        self._queue.put(task_id)
        return task_id

    def submit_scan(self, photos: list[bytes], longest_mm: float | None, ai_depth: bool = True, ai_fill: bool = True) -> str:
        task_id = uuid.uuid4().hex[:12]
        photo_dir = DATA_DIR / task_id / "photos"
        photo_dir.mkdir(parents=True, exist_ok=True)
        for index, data in enumerate(photos):
            (photo_dir / f"{index:04d}.jpg").write_bytes(data)
        self.tasks[task_id] = {
            "id": task_id, "kind": "scan", "status": "PENDING", "progress": 0, "stage": "queued",
            "engine": "colmap", "longest_mm": longest_mm, "ai_depth": ai_depth, "ai_fill": ai_fill, "files": [], "report": None, "error": None,
            "created_at": time.time(), "seconds": None,
        }
        self._queue.put(task_id)
        return task_id

    def _set(self, task_id: str, **fields) -> None:
        self.tasks[task_id].update(fields)

    def _acquire_gpu(self) -> None:
        if self._gpu_held:
            return
        if GPU_ACQUIRE_CMD:
            subprocess.run(GPU_ACQUIRE_CMD, shell=True, check=False, timeout=120)
            time.sleep(3)  # let the other process release its VRAM
        self._gpu_held = True

    def _release_gpu(self) -> None:
        self.engine.unload()
        self._rembg = None
        if self._gpu_held and GPU_RELEASE_CMD:
            subprocess.run(GPU_RELEASE_CMD, shell=True, check=False, timeout=120)
        self._gpu_held = False

    def _loop(self) -> None:
        # One GPU, one task at a time
        while True:
            try:
                task_id = self._queue.get(timeout=5)
            except queue.Empty:
                if self._gpu_held and time.monotonic() - self._last_used > IDLE_SECONDS:
                    self._release_gpu()
                continue
            try:
                self._run(task_id)
            except scan.ScanError as exc:
                self._set(task_id, status="FAILED", error=str(exc))  # already written for the user
            except Exception as exc:
                traceback.print_exc()
                self._set(task_id, status="FAILED", error=f"{type(exc).__name__}: {exc}")
            self._last_used = time.monotonic()

    def _run(self, task_id: str) -> None:
        kind = self.tasks[task_id]["kind"]
        if kind == "rig":
            self._run_rig(task_id)
        elif kind == "scan":
            self._run_scan(task_id)
        else:
            self._run_generate(task_id)

    def _run_scan(self, task_id: str) -> None:
        task = self.tasks[task_id]
        task_dir = DATA_DIR / task_id
        started = time.monotonic()
        self._set(task_id, status="IN_PROGRESS", progress=1, stage="starting")
        self._acquire_gpu()
        # COLMAP runs in its own process and needs the VRAM the generator's models hold
        self.engine.unload()

        # The gap fill takes the last stretch of the progress bar, if it's on
        share = 0.9 if task["ai_fill"] else 1.0

        def progress(pct: int, stage: str) -> None:
            self._set(task_id, progress=int(pct * share), stage=stage)

        work = task_dir / "work"
        report = scan.run(task_dir / "photos", work, task["longest_mm"], task["ai_depth"], progress)
        if task["ai_fill"]:
            report = self._fill_scan_gaps(task_id, work, report)

        (task_dir / "model.glb").write_bytes((work / "model.glb").read_bytes())
        files = ["model.glb"]
        for extra in ("model_measured.glb", "model_sources.glb"):
            if (work / extra).exists():
                (task_dir / extra).write_bytes((work / extra).read_bytes())
                files.append(extra)
        if unirig.available():
            try:
                unirig.to_fbx(task_dir / "model.glb", task_dir / "model.fbx", task_dir / "export.log")
                files.append("model.fbx")
            except unirig.StepFailed:
                traceback.print_exc()  # GLB is enough to finish the task
        self._set(
            task_id, status="SUCCEEDED", progress=100, stage="done", files=files, report=report,
            seconds=round(time.monotonic() - started, 1),
        )

    def _fill_scan_gaps(self, task_id: str, work: Path, report: dict) -> dict:
        """Patches what the scan missed. A failure here can't cost the user their scan, so it only adds a warning."""
        try:
            self._set(task_id, progress=90, stage="AI model for the gaps")
            photos = scan.pick_fill_photos(work)
            if self._rembg is None:
                from hy3dgen.rembg import BackgroundRemover

                self._rembg = BackgroundRemover()
            cutouts = {view: self._rembg(Image.open(path).convert("RGB")) for view, path in photos.items()}
            mesh = self.engine.generate(cutouts, texture=False, progress=lambda pct, stage: None)
            mesh.export(work / "ai.glb")
            self.engine.unload()  # the merge doesn't need the GPU, but nothing else should wait on these models
            self._set(task_id, progress=96, stage="filling gaps")
            return scan.fill_gaps(work, work / "ai.glb")
        except Exception as exc:
            traceback.print_exc()
            self.engine.unload()
            report["warnings"].append(f"The AI gap fill didn't run, so unseen areas are still missing ({type(exc).__name__}).")
            (work / "report.json").write_text(json.dumps(report, indent=2))
            return report

    def _run_rig(self, task_id: str) -> None:
        task = self.tasks[task_id]
        started = time.monotonic()
        self._set(task_id, status="IN_PROGRESS", progress=1, stage="starting")
        self._acquire_gpu()
        # UniRig runs in its own processes and needs the VRAM the generator's models hold
        self.engine.unload()

        def progress(pct: int, stage: str) -> None:
            self._set(task_id, progress=pct, stage=stage)

        unirig.rig(DATA_DIR / task["source"] / "model.glb", DATA_DIR / task_id / "work", progress)
        self._set(
            task_id, status="SUCCEEDED", progress=100, stage="done", files=["rigged.glb", "rigged.fbx"],
            seconds=round(time.monotonic() - started, 1),
        )

    def _run_generate(self, task_id: str) -> None:
        task = self.tasks[task_id]
        task_dir = DATA_DIR / task_id
        started = time.monotonic()
        self._set(task_id, status="IN_PROGRESS", progress=1, stage="loading models")
        self._acquire_gpu()

        self._set(task_id, progress=5, stage="removing background")
        if self._rembg is None:
            from hy3dgen.rembg import BackgroundRemover

            self._rembg = BackgroundRemover()
        cutouts = {}
        for view in task["views"]:
            image = Image.open(io.BytesIO((task_dir / f"input_{view}").read_bytes()))
            cutouts[view] = self._rembg(image.convert("RGB"))
            cutouts[view].save(task_dir / ("cutout.png" if view == "front" else f"cutout_{view}.png"))

        def progress(pct: int, stage: str) -> None:
            self._set(task_id, progress=pct, stage=stage)

        mesh = self.engine.generate(cutouts, texture=task["texture"], progress=progress)
        self._set(task_id, progress=95, stage="exporting")
        mesh.export(task_dir / "model.glb")
        files = ["model.glb", "cutout.png"]
        if unirig.available():  # its Blender module also does the FBX export
            try:
                unirig.to_fbx(task_dir / "model.glb", task_dir / "model.fbx", task_dir / "export.log")
                files.append("model.fbx")
            except unirig.StepFailed:
                traceback.print_exc()  # GLB is enough to finish the task
        self._set(
            task_id, status="SUCCEEDED", progress=100, stage="done", files=files,
            seconds=round(time.monotonic() - started, 1), faces=int(len(mesh.faces)),
        )


app = FastAPI(title="Local 3D generator")
gen = Generator()


@app.get("/health")
def health():
    return {
        "status": "ok", "engine": gen.engine.name, "rigging": "unirig" if unirig.available() else None,
        "models_loaded": gen.engine.loaded, "queued": gen._queue.qsize(),
    }


@app.post("/tasks", status_code=201)
def create_task(req: TaskRequest):
    return {"result": gen.submit(req)}


@app.post("/scans", status_code=201)
async def create_scan(
    photos: list[UploadFile] = File(...), longest_mm: float | None = Form(None),
    ai_depth: bool = Form(True), ai_fill: bool = Form(True),
):
    if not scan.available():
        raise HTTPException(501, "Photogrammetry isn't installed (see generator/README.md)")
    return {"result": gen.submit_scan([await photo.read() for photo in photos], longest_mm, ai_depth, ai_fill)}


@app.post("/tasks/{task_id}/rig", status_code=201)
def rig_task(task_id: str):
    return {"result": gen.submit_rig(task_id)}


@app.get("/tasks/{task_id}")
def get_task(task_id: str):
    task = gen.tasks.get(task_id)
    if task is None:
        raise HTTPException(404, "Unknown task")
    return task


@app.get("/tasks/{task_id}/files/{name}")
def get_file(task_id: str, name: str):
    task = gen.tasks.get(task_id)
    if task is None or name not in task["files"]:
        raise HTTPException(404, "File not found")
    return FileResponse(DATA_DIR / task_id / name)
