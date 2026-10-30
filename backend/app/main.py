"""HTTP API for the photo-to-game-asset pipeline. Run: uvicorn app.main:app --host 0.0.0.0 --port 8000"""
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import CATEGORIES, Settings
from .db import ACTIVE_STATUSES, JobStore
from .images import PhotoRejected, process_photo
from .meshy import make_client
from .worker import Worker, view_photo_name

STATIC_DIR = Path(__file__).parent / "static"
_SAFE_NAME = re.compile(r"^[a-z0-9_.-]+$")


def create_app(settings: Settings | None = None, meshy=None) -> FastAPI:
    settings = settings or Settings()
    store = JobStore(settings.db_path)
    worker = Worker(store, meshy or make_client(settings), settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker.resume_unfinished()
        yield
        await worker.shutdown()

    app = FastAPI(title="Photo-to-Game-Asset API", version="0.1.0", lifespan=lifespan)
    # The desktop hub and phone app may be served from other origins during development
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.state.settings, app.state.store, app.state.worker = settings, store, worker

    def public(job: dict) -> dict:
        job = dict(job)
        job["files"] = {
            name: {**info, "url": f"/api/jobs/{job['id']}/files/{name}"} for name, info in job["files"].items()
        }
        return job

    def get_or_404(job_id: str) -> dict:
        job = store.get(job_id)
        if job is None:
            raise HTTPException(404, "Job not found")
        return job

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/config")
    def config():
        return {
            "categories": ["auto", *CATEGORIES],
            "generator": settings.generator,
            "classifier": settings.classifier,
            "max_upload_mb": settings.max_upload_mb,
        }

    @app.post("/api/jobs", status_code=201)
    async def create_job(
        photo: UploadFile = File(...),
        category: str = Form("auto"),
        force: bool = Form(False),
        left: UploadFile | None = File(None),
        back: UploadFile | None = File(None),
        right: UploadFile | None = File(None),
    ):
        """`photo` is the front view. Optional extra angles help the local generator get the shape right."""
        if category != "auto" and category not in CATEGORIES:
            raise HTTPException(422, f"category must be one of: auto, {', '.join(CATEGORIES)}")
        uploads = {"front": photo, "left": left, "back": back, "right": right}
        photos = {}  # view -> (jpeg bytes, quality)
        for view, upload in uploads.items():
            if upload is None or (view != "front" and not upload.filename):
                continue  # browsers send empty parts for unused file inputs
            try:
                photos[view] = process_photo(await upload.read(), settings, force=force)
            except PhotoRejected as exc:
                detail = exc.reason if view == "front" else f"{view.capitalize()} photo: {exc.reason}"
                return JSONResponse(status_code=422, content={"detail": detail, "quality": exc.quality, "view": view})

        normalized, quality = photos["front"]
        job = store.create(category, photo.filename or "photo", quality)
        job_dir = settings.job_dir(job["id"])
        job_dir.mkdir(parents=True, exist_ok=True)
        files = {}
        for view, (data, view_quality) in photos.items():
            name = view_photo_name(view)
            (job_dir / name).write_bytes(data)
            files[name] = {"kind": "photo", "view": view, "size": len(data)}
            if view != "front":
                files[name]["quality"] = view_quality
        job = store.update(job["id"], files=files)
        worker.submit(job["id"])
        return public(job)

    @app.get("/api/jobs")
    def list_jobs(status: str | None = None, limit: int = 100):
        return [public(j) for j in store.list(status, min(limit, 500))]

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        return public(get_or_404(job_id))

    @app.get("/api/jobs/{job_id}/files/{name}")
    def get_file(job_id: str, name: str):
        job = get_or_404(job_id)
        if not _SAFE_NAME.match(name) or name not in job["files"]:
            raise HTTPException(404, "File not found")
        return FileResponse(settings.job_dir(job_id) / name, filename=f"{job_id}-{name}")

    @app.post("/api/jobs/{job_id}/retry")
    def retry_job(job_id: str):
        job = get_or_404(job_id)
        if job["status"] != "failed":
            raise HTTPException(409, "Only failed jobs can be retried")
        # Start over from generation; keep the classification
        job = store.update(job_id, status="queued", progress=0, error=None, meshy_task_id=None, rig_task_id=None, rig_status=None)
        worker.submit(job_id)
        return public(job)

    @app.delete("/api/jobs/{job_id}", status_code=204)
    def delete_job(job_id: str):
        job = get_or_404(job_id)
        if job["status"] in ACTIVE_STATUSES:
            raise HTTPException(409, "Job is still running")
        store.delete(job_id)
        shutil.rmtree(settings.job_dir(job_id), ignore_errors=True)

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


app = create_app()
