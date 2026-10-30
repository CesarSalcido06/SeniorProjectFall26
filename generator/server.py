"""Local 3D generator service: photo -> textured GLB on this machine's GPU.

The backend talks to it the same way it talks to Meshy (create a task, poll it, download files).
Run from generator/: .venv/bin/uvicorn server:app --host 127.0.0.1 --port 8100
"""
import base64
import io
import os
import queue
import subprocess
import threading
import time
import traceback
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
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


from engines import unirig  # noqa: E402


def make_engine(name: str):
    if name == "hunyuan":
        from engines.hunyuan import HunyuanEngine

        return HunyuanEngine(max_faces=int(os.getenv("MAX_FACES", "40000")))
    raise ValueError(f"Unknown GENERATOR_ENGINE {name!r}")


class TaskRequest(BaseModel):
    image_url: str  # data:image/...;base64,... (same field name as Meshy)
    category: str = "prop"
    texture: bool = True


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
        header, _, b64 = req.image_url.partition(",")
        if not header.startswith("data:image/") or not b64:
            raise HTTPException(400, "image_url must be a base64 data URI")
        task_id = uuid.uuid4().hex[:12]
        task_dir = DATA_DIR / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "input").write_bytes(base64.b64decode(b64))
        self.tasks[task_id] = {
            "id": task_id, "kind": "generate", "status": "PENDING", "progress": 0, "stage": "queued",
            "category": req.category, "texture": req.texture, "engine": self.engine.name, "files": [],
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
            except Exception as exc:
                traceback.print_exc()
                self._set(task_id, status="FAILED", error=f"{type(exc).__name__}: {exc}")
            self._last_used = time.monotonic()

    def _run(self, task_id: str) -> None:
        if self.tasks[task_id]["kind"] == "rig":
            self._run_rig(task_id)
        else:
            self._run_generate(task_id)

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

        image = Image.open(io.BytesIO((task_dir / "input").read_bytes()))
        self._set(task_id, progress=5, stage="removing background")
        if self._rembg is None:
            from hy3dgen.rembg import BackgroundRemover

            self._rembg = BackgroundRemover()
        cutout = self._rembg(image.convert("RGB"))
        cutout.save(task_dir / "cutout.png")

        def progress(pct: int, stage: str) -> None:
            self._set(task_id, progress=pct, stage=stage)

        mesh = self.engine.generate(cutout, texture=task["texture"], progress=progress)
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
