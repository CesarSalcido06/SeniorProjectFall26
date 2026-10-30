"""Runs each job: classify -> generate (Meshy) -> rig (characters) -> download the files locally."""
import asyncio
import base64
import time
import traceback

from .classifier import classify
from .meshy import PROFILES, MeshyError

PHOTO_NAME = "photo.jpg"
TASK_TIMEOUT_SECONDS = 20 * 60

# Meshy result key -> (our file name, kind). Kinds let the desktop hub pick what to import.
_MODEL_FILES = {"glb": ("model.glb", "model"), "fbx": ("model.fbx", "model")}
_RIG_FILES = {
    "rigged_character_glb_url": ("rigged.glb", "rigged"),
    "rigged_character_fbx_url": ("rigged.fbx", "rigged"),
}
_ANIMATION_FILES = {
    "walking_fbx_url": ("walking.fbx", "animation"),
    "running_fbx_url": ("running.fbx", "animation"),
}


class Worker:
    def __init__(self, store, meshy, settings) -> None:
        self.store = store
        self.meshy = meshy
        self.settings = settings
        self._slots = asyncio.Semaphore(settings.max_concurrent_jobs)
        self._tasks: dict[str, asyncio.Task] = {}

    def submit(self, job_id: str) -> None:
        if job_id not in self._tasks:
            task = asyncio.create_task(self._run(job_id))
            self._tasks[job_id] = task
            task.add_done_callback(lambda _: self._tasks.pop(job_id, None))

    def resume_unfinished(self) -> None:
        for job in self.store.unfinished():
            self.submit(job["id"])

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def _run(self, job_id: str) -> None:
        async with self._slots:
            try:
                await self._process(job_id)
            except asyncio.CancelledError:
                raise  # server shutting down; the job resumes on next start
            except Exception as exc:
                traceback.print_exc()
                self.store.update(job_id, status="failed", error=f"{type(exc).__name__}: {exc}")

    async def _process(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None:
            return
        job_dir = self.settings.job_dir(job_id)
        photo = (job_dir / PHOTO_NAME).read_bytes()

        if not job["category"]:
            self.store.update(job_id, status="classifying")
            result = await classify(photo, job["requested_category"], self.settings)
            job = self.store.update(
                job_id, category=result.category, category_source=result.source, classifier_note=result.note
            )
        category = job["category"]

        task_id = job["meshy_task_id"]
        if not task_id:
            image_uri = "data:image/jpeg;base64," + base64.b64encode(photo).decode()
            task_id = await self.meshy.create_image_to_3d(image_uri, category)
            self.store.update(job_id, meshy_task_id=task_id)
        self.store.update(job_id, status="generating")
        generated = await self._wait(job_id, self.meshy.get_image_to_3d, task_id, progress_span=(0, 80))

        rigged = None
        if PROFILES[category]["rig"]:
            self.store.update(job_id, status="rigging")
            try:
                rig_id = job["rig_task_id"] or await self.meshy.create_rigging(task_id)
                self.store.update(job_id, rig_task_id=rig_id)
                rigged = await self._wait(job_id, self.meshy.get_rigging, rig_id, progress_span=(80, 95))
                self.store.update(job_id, rig_status="rigged")
            except MeshyError as exc:
                # Keep the unrigged model rather than failing the whole job
                self.store.update(job_id, rig_status=f"failed: {exc}")
        else:
            self.store.update(job_id, rig_status="skipped")

        self.store.update(job_id, status="downloading", progress=95)
        files = {PHOTO_NAME: {"kind": "photo", "size": len(photo)}}
        downloads = []
        for key, (name, kind) in _MODEL_FILES.items():
            if url := (generated.get("model_urls") or {}).get(key):
                downloads.append((url, name, kind))
        if url := generated.get("thumbnail_url"):
            downloads.append((url, "thumbnail.png", "thumbnail"))
        if rigged:
            result = rigged.get("result") or {}
            for key, (name, kind) in _RIG_FILES.items():
                if url := result.get(key):
                    downloads.append((url, name, kind))
            animations = result.get("basic_animations") or {}
            for key, (name, kind) in _ANIMATION_FILES.items():
                if url := animations.get(key):
                    downloads.append((url, name, kind))

        for url, name, kind in downloads:
            data = await self.meshy.download(url)
            (job_dir / name).write_bytes(data)
            files[name] = {"kind": kind, "size": len(data)}

        self.store.update(job_id, status="done", progress=100, files=files, error=None)

    async def _wait(self, job_id, fetch, task_id: str, progress_span: tuple[int, int]) -> dict:
        start, end = progress_span
        deadline = time.monotonic() + TASK_TIMEOUT_SECONDS
        while True:
            task = await fetch(task_id)
            status = task.get("status")
            pct = int(task.get("progress") or 0)
            self.store.update(job_id, progress=start + (end - start) * pct // 100)
            if status == "SUCCEEDED":
                return task
            if status in ("FAILED", "CANCELED"):
                message = (task.get("task_error") or {}).get("message") or status.lower()
                raise MeshyError(f"Meshy task {task_id} {status.lower()}: {message}")
            if time.monotonic() > deadline:
                raise MeshyError(f"Meshy task {task_id} timed out after {TASK_TIMEOUT_SECONDS // 60} minutes")
            await asyncio.sleep(self.settings.meshy_poll_seconds)
