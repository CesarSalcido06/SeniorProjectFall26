# Backend API

The contract between the backend (Cesar) and the clients: the phone capture app and the desktop hub (Daniel).
Base URL in development: `http://<backend-host>:8000`. All responses are JSON unless noted. CORS is open.

## Job lifecycle

```
queued → classifying → generating → rigging (characters only) → downloading → done
                                                                          ↘ failed (any step)
```

`progress` is 0–100 across the whole job (generation is 0–80, rigging 80–95, downloading 95–100).

## Endpoints

### `POST /api/jobs` — upload a photo
`multipart/form-data`:

| Field | Required | Values |
|---|---|---|
| `photo` | yes | JPG/PNG/WebP, max 15 MB, short side ≥ 512 px |
| `category` | no | `auto` (default), `prop`, `character`, `creature` |
| `force` | no | `true` skips the blur/lighting check (not the size/format check) |

- `201` → the new [job](#job-object).
- `422` → photo rejected: `{"detail": "Photo looks blurry. …", "quality": {"sharpness": 41.2, "brightness": 96.0, …}}`.
  Show `detail` to the user; it's written for them.

### `GET /api/jobs?status=done&limit=100` — list jobs (newest first)
### `GET /api/jobs/{id}` — one job
### `GET /api/jobs/{id}/files/{name}` — download a file (names come from the job's `files`)
### `POST /api/jobs/{id}/retry` — rerun a `failed` job (`409` otherwise)
### `DELETE /api/jobs/{id}` — delete a finished job and its files (`409` while running)
### `GET /api/config` — `{"categories": [...], "meshy_mode": "mock"|"live", "classifier": "manual"|"openai"}`
### `GET /api/health` — `{"status": "ok"}`

## Job object

```json
{
  "id": "4181202781d3",
  "created_at": 1793380000.1,
  "updated_at": 1793380095.7,
  "status": "done",
  "progress": 100,
  "requested_category": "auto",
  "category": "character",
  "category_source": "classifier",
  "classifier_note": "character",
  "original_name": "IMG_2041.jpg",
  "quality": {"width": 3024, "height": 4032, "sharpness": 1584.7, "brightness": 109.0, "forced": false,
              "normalized_width": 1536, "normalized_height": 2048},
  "meshy_task_id": "0193…",
  "rig_task_id": "0194…",
  "rig_status": "rigged",
  "error": null,
  "files": {
    "photo.jpg":     {"kind": "photo",     "size": 412233,  "url": "/api/jobs/4181202781d3/files/photo.jpg"},
    "model.glb":     {"kind": "model",     "size": 8123456, "url": "/api/jobs/4181202781d3/files/model.glb"},
    "model.fbx":     {"kind": "model",     "size": 9234567, "url": "…"},
    "thumbnail.png": {"kind": "thumbnail", "size": 120000,  "url": "…"},
    "rigged.glb":    {"kind": "rigged",    "size": 9345678, "url": "…"},
    "rigged.fbx":    {"kind": "rigged",    "size": 9456789, "url": "…"},
    "walking.fbx":   {"kind": "animation", "size": 1200000, "url": "…"},
    "running.fbx":   {"kind": "animation", "size": 1200000, "url": "…"}
  }
}
```

- `category_source`: `user` (picked at upload), `classifier` (vision model decided), or `default` (classifier off or failed → `prop`; the reason is in `classifier_note`).
- `rig_status`: `skipped` (prop/creature), `rigged`, or `failed: <reason>`. A failed rig still finishes the job with the unrigged model.
- `files` only lists what exists. Props and creatures have no `rigged.*` or animation files. In mock mode there is no FBX and `rigged.glb` is the same placeholder cube without a skeleton.

## What the Unity importer should use

| Category | Import | Notes |
|---|---|---|
| prop | `model.fbx` (or `model.glb` via glTFast) | static mesh + collider |
| creature | `model.fbx` | static for now: Meshy only auto-rigs humanoids |
| character | `rigged.fbx` + `walking.fbx` / `running.fbx` | Humanoid avatar, Animator Controller with walk/run |

## Category → Meshy settings

All jobs: `ai_model: latest`, textured with PBR maps, remeshed to 30k triangles, GLB + FBX output.
Characters also get `pose_mode: a-pose` and a rigging pass (`height_meters: 1.7`).

Meshy's rigging API only supports humanoid (bipedal) models, so creatures aren't rigged.
That's narrower than the proposal, which assumed quadruped rigs too.
