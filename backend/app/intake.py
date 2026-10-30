"""Accepting new jobs: check every photo, then save them and create the job.

Shared by the HTTP upload (main.py) and the Firebase listener (firebase_sync.py), so photos get the same
quality gate however they arrive.
"""
from .images import PhotoRejected, process_photo
from .worker import view_photo_name

VIEWS = ("front", "left", "back", "right")


class ViewRejected(Exception):
    """One of the photos failed the quality gate. `detail` is written for the user."""

    def __init__(self, view: str, reason: str, quality: dict | None) -> None:
        self.view, self.quality = view, quality
        self.detail = reason if view == "front" else f"{view.capitalize()} photo: {reason}"
        super().__init__(self.detail)


def check_photos(raw: dict[str, bytes], settings, force: bool) -> dict[str, tuple[bytes, dict]]:
    """raw: {view: uploaded bytes}, must include "front". Returns {view: (normalized jpeg, quality)}."""
    checked = {}
    for view in VIEWS:
        if view not in raw:
            continue
        try:
            checked[view] = process_photo(raw[view], settings, force=force)
        except PhotoRejected as exc:
            raise ViewRejected(view, exc.reason, exc.quality) from exc
    return checked


def create_job(store, settings, photos: dict[str, tuple[bytes, dict]], category: str, original_name: str,
               job_id: str | None = None) -> dict:
    """Save checked photos and create the job (queued). The caller hands it to the worker."""
    _, quality = photos["front"]
    job = store.create(category, original_name, quality, job_id=job_id)
    job_dir = settings.job_dir(job["id"])
    job_dir.mkdir(parents=True, exist_ok=True)
    files = {}
    for view, (data, view_quality) in photos.items():
        name = view_photo_name(view)
        (job_dir / name).write_bytes(data)
        files[name] = {"kind": "photo", "view": view, "size": len(data)}
        if view != "front":
            files[name]["quality"] = view_quality
    return store.update(job["id"], files=files)
