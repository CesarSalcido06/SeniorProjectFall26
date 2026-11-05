"""HTTP API for the photo-to-game-asset pipeline. Run: uvicorn app.main:app --host 0.0.0.0 --port 8000"""
import asyncio
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
from . import intake
from .meshy import make_client
from .worker import Worker

STATIC_DIR = Path(__file__).parent / "static"
_SAFE_NAME = re.compile(r"^[a-z0-9_.-]+$")


def create_app(settings: Settings | None = None, meshy=None) -> FastAPI:
    settings = settings or Settings()
    store = JobStore(settings.db_path)
    worker = Worker(store, meshy or make_client(settings), settings)

    firebase = None
    if settings.firebase_project_id:
        from .firebase_sync import FirebaseSync

        firebase = FirebaseSync(settings, store, worker)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if firebase:
            firebase.start(asyncio.get_running_loop())
        worker.resume_unfinished()
        yield
        await worker.shutdown()
        if firebase:
            firebase.stop()

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
            "firebase": settings.firebase_project_id or None,
            "scan": hasattr(worker.meshy, "create_scan"),
            "scan_min_photos": settings.scan_min_photos,
            "scan_max_photos": settings.scan_max_photos,
            "scan_max_side": settings.scan_max_side,
        }

    @app.post("/api/scans", status_code=201)
    def create_scan():
        """Opens a photogrammetry scan. Photos then come one at a time, so a dropped connection only costs one."""
        if not hasattr(worker.meshy, "create_scan"):
            raise HTTPException(501, "Scans need GENERATOR=local")
        return public(store.create_scan(None))

    @app.post("/api/scans/{job_id}/photos", status_code=201)
    async def add_scan_photo(job_id: str, photo: UploadFile = File(...)):
        data = await photo.read()
        job = get_or_404(job_id)
        if job["mode"] != "scan" or job["status"] != "uploading":
            raise HTTPException(409, "This scan isn't accepting photos")
        try:
            # Never rejected for blur: one soft photo is fine in a set of 40. Blurry ones get flagged instead.
            jpeg, quality = process_photo(data, settings, force=True, max_side=settings.scan_max_side, jpeg_quality=95)
        except PhotoRejected as exc:
            return JSONResponse(status_code=422, content={"detail": exc.reason, "quality": exc.quality})
        # No awaits from here to the update, so photos uploading side by side can't overwrite each other
        job = get_or_404(job_id)
        if job["status"] != "uploading":
            raise HTTPException(409, "This scan isn't accepting photos")
        count = sum(1 for info in job["files"].values() if info.get("view") == "scan")
        if count >= settings.scan_max_photos:
            raise HTTPException(422, f"A scan can use at most {settings.scan_max_photos} photos")
        name = f"scan_{count + 1:04d}.jpg"
        job_dir = settings.job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / name).write_bytes(jpeg)
        blurry = quality["sharpness"] < settings.blur_threshold
        files = {**job["files"], name: {"kind": "photo", "view": "scan", "size": len(jpeg), "blurry": blurry}}
        store.update(job_id, files=files)
        return {"name": name, "count": count + 1, "blurry": blurry}

    @app.post("/api/scans/{job_id}/start")
    async def start_scan(  # async: worker.submit needs the event loop
        job_id: str, longest_mm: float | None = Form(None), ai_depth: bool = Form(True), ai_fill: bool = Form(True),
    ):
        """longest_mm is the piece's longest side measured with a ruler; it sets the model's real size.

        ai_depth estimates plain faces the scan can't measure. ai_fill patches what no photo saw from an AI model.
        """
        job = get_or_404(job_id)
        if job["mode"] != "scan" or job["status"] != "uploading":
            raise HTTPException(409, "This scan already started")
        if longest_mm is not None and longest_mm <= 0:
            raise HTTPException(422, "The longest side must be above zero.")
        count = sum(1 for info in job["files"].values() if info.get("view") == "scan")
        if count < settings.scan_min_photos:
            raise HTTPException(422, f"A scan needs at least {settings.scan_min_photos} photos (it has {count}).")
        job = store.update(job_id, status="queued", progress=0, scan={"longest_mm": longest_mm, "ai_depth": ai_depth, "ai_fill": ai_fill})
        worker.submit(job_id)
        return public(job)

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
        raw = {}
        for view, upload in uploads.items():
            if upload is None or (view != "front" and not upload.filename):
                continue  # browsers send empty parts for unused file inputs
            raw[view] = await upload.read()
        try:
            photos = intake.check_photos(raw, settings, force)
        except intake.ViewRejected as exc:
            return JSONResponse(status_code=422, content={"detail": exc.detail, "quality": exc.quality, "view": exc.view})
        job = intake.create_job(store, settings, photos, category, photo.filename or "photo")
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
    async def retry_job(job_id: str):  # async: worker.submit needs the event loop
        job = get_or_404(job_id)
        if job["status"] != "failed":
            raise HTTPException(409, "Only failed jobs can be retried")
        # Start over from generation; keep the classification
        job = store.update(job_id, status="queued", progress=0, stage=None, error=None, meshy_task_id=None, rig_task_id=None, rig_status=None)
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
