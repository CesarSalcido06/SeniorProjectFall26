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
| `photo` | yes | The **front** view. JPG/PNG/WebP, max 15 MB, short side ≥ 512 px |
| `left`, `back`, `right` | no | Extra angles, same rules as `photo`. Send any subset |
| `category` | no | `auto` (default), `prop`, `character`, `creature` |
| `force` | no | `true` skips the blur/lighting check (not the size/format check) |

- `201` → the new [job](#job-object).
- `422` → a photo was rejected: `{"detail": "Photo looks blurry. …", "quality": {"sharpness": 41.2, "brightness": 96.0, …}, "view": "front"}`.
  For an extra angle, `detail` starts with the view (`"Back photo: …"`). Show `detail` to the user; it's written for them.
  Nothing is created when any photo is rejected.

#### Extra angles
With any extra angle, the local generator switches to its multi-view shape model (Hunyuan3D-2mv), which
gets the back and sides right instead of guessing them, and its texture pass uses every photo.
Meshy and the mock use only the front photo.

The views are a quarter turn apart around the object, named from the **object's** point of view
(like stage left): `left` is the object's left side, so the photographer steps to *their* right.
Keep the same distance and height for every shot, and don't move the object between shots.

### `GET /api/jobs?status=done&limit=100` — list jobs (newest first)
### `GET /api/jobs/{id}` — one job
### `GET /api/jobs/{id}/files/{name}` — download a file (names come from the job's `files`)
### `POST /api/jobs/{id}/retry` — rerun a `failed` job (`409` otherwise)
### `DELETE /api/jobs/{id}` — delete a finished job and its files (`409` while running)
### `GET /api/config` — `{"categories": [...], "generator": "local"|"meshy"|"mock", "classifier": "manual"|"openai"}`
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
    "photo.jpg":     {"kind": "photo", "view": "front", "size": 412233, "url": "/api/jobs/4181202781d3/files/photo.jpg"},
    "photo_back.jpg": {"kind": "photo", "view": "back", "size": 398102, "quality": {"sharpness": 902.4, …}, "url": "…"},
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
- `rig_status`: `skipped` (not rigged for this category), `rigged`, or `failed: <reason>`. A failed rig still finishes the job with the unrigged model.
- `quality` is the front photo's; each extra angle's is on its `files` entry. Photos are `photo.jpg` (front)
  and `photo_left.jpg` / `photo_back.jpg` / `photo_right.jpg`.
- `files` only lists what exists. What you get depends on the generator (`GET /api/config` → `generator`):

| | `local` (our Hunyuan3D + UniRig) | `meshy` | `mock` |
|---|---|---|---|
| `model.glb` / `model.fbx` | ✓ / ✓ | ✓ / ✓ | ✓ / – |
| Rigged categories | **character and creature** | character only | character (fake) |
| `rigged.glb` / `rigged.fbx` | ✓ / ✓ | ✓ / ✓ | ✓ / – (cube, no skeleton) |
| `walking.fbx` / `running.fbx` | – | ✓ (characters) | – |
| Bone names | `bone_0…bone_N` (Generic rig) | Mixamo-style humanoid | – |
| PBR maps | base color only | base color + metallic/roughness/normal | – |
| Time per asset (RTX 3060) | ~2 min static, ~3–3.5 min rigged | ~2 min | ~6 s |

## What the Unity importer should use

| Category | Import | Notes |
|---|---|---|
| prop | `model.fbx` (or `model.glb` via glTFast) | static mesh + MeshCollider/BoxCollider |
| creature | `rigged.fbx` if present, else `model.fbx` | **Generic** avatar (local generator) |
| character | `rigged.fbx` (+ `walking.fbx` / `running.fbx` from Meshy) | Meshy: Humanoid avatar. Local: Generic avatar, since UniRig's bones aren't named |

FBX files embed their textures. Scale: models are about 2 units tall (local) or real-world meters (Meshy).

## Category → generation settings

**Local:** Hunyuan3D-2mini-turbo shape from one photo, Hunyuan3D-2mv-turbo from several (≤40k faces), Hunyuan3D-Paint texture, UniRig skeleton + skin weights
for characters and creatures. See `generator/README.md`.

**Meshy:** `ai_model: latest`, textured with PBR maps, remeshed to 30k triangles, GLB + FBX output.
Characters also get `pose_mode: a-pose` and a rigging pass (`height_meters: 1.7`).
Meshy's rigging API only supports humanoid (bipedal) models, so creatures aren't rigged there.
