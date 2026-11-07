# Local 3D generator

Our own replacement for Meshy's image-to-3D step, running on a local NVIDIA GPU with open-weight models:

| Step | Model | License |
|---|---|---|
| Background removal | rembg (U²-Net) | MIT |
| Photo → shape | Hunyuan3D-2mini turbo (0.6B) | Tencent Hunyuan 3D 2.0 Community License |
| Several angles → shape | Hunyuan3D-2mv turbo | same |
| Shape → texture | Hunyuan3D-Paint v2.0 turbo | same |
| Auto-rigging (skeleton + skin weights) | UniRig (SIGGRAPH 2025) | MIT |
| GLB → FBX export | Blender 4.2 as a Python module (`bpy`) | GPL (used as a tool, not shipped) |

UniRig predicts a skeleton for any body plan, so unlike Meshy (humanoids only) it rigs creatures too:
a dog photo came back with a spine, tail chain, four legs, jaw, and ears. Its bones are named
`bone_0…bone_N`, so Unity imports rigs as Generic, not Humanoid.

Measured on an RTX 3060 (12 GB): generation ~1 min (7.9 GB peak VRAM), rigging ~2 min more.

The Hunyuan license is free for us. It doesn't apply in the EU, UK, or South Korea, and organizations over
1M monthly users need a separate license.

## How it fits

```
backend (GENERATOR=local) ──HTTP──> generator/server.py :8100 ──> GPU
```

The backend's `LocalGeneratorClient` (`backend/app/meshy.py`) creates a task, polls it, and downloads
`model.glb` + `model.fbx` (textured) and `cutout.png` (the photo with its background removed, used as the
thumbnail). For characters and creatures it then asks for a rig and downloads `rigged.glb` + `rigged.fbx`.
The API mirrors Meshy's, so nothing else in the backend changes.

With extra angles (`views` below) the shape comes from the multi-view model, which sees the back and sides
instead of inventing them, and the texture pass uses every photo. Only one shape model is on the GPU at a
time; switching between single- and multi-view jobs reloads it (a few seconds).

One task runs at a time. Models load on the first task and unload after `GPU_IDLE_SECONDS` without work.

## Setup (once)

Needs Linux, an NVIDIA GPU with 12 GB+, CUDA toolkit 12.4 (`/usr/local/cuda-12.4`), and [uv](https://docs.astral.sh/uv/).

```bash
generator/setup.sh
```

The first run downloads about 10 GB of model weights into `~/.cache/hy3dgen` and `~/.cache/huggingface`,
the first multi-view job about 5 GB more, and the first rig downloads UniRig's checkpoint (about 5.5 GB).

UniRig runs in its own environment (`.venv-unirig`, Python 3.11) because it needs Blender as a Python module.
The generator calls it through subprocesses (`engines/unirig.py`, `tools/`). If it isn't installed, rigging
returns `501` and jobs still finish with the unrigged model.

## Run

```bash
cd generator
.venv/bin/uvicorn server:app --host 127.0.0.1 --port 8100
```

Then start the backend with `GENERATOR=local` (see `backend/.env.example`).

### Sharing the GPU with BenchAI / MegaMan

BenchAI's model servers hold ~10 GB of the 12 GB card. The generator can hand the GPU over automatically:

```bash
GPU_ACQUIRE_CMD="systemctl --user stop benchai-router" \
GPU_RELEASE_CMD="systemctl --user start benchai-router" \
GPU_IDLE_SECONDS=120 \
.venv/bin/uvicorn server:app --host 127.0.0.1 --port 8100
```

The first task stops BenchAI, and BenchAI restarts 2 minutes after the last task. While BenchAI is down,
the MegaMan classifier is unavailable, so jobs uploaded as "auto" during that window fall back to prop
(the job's `classifier_note` says so). Pick the category by hand when uploading a batch.

## Accurate scans (photogrammetry plus AI help)

`POST /scans` takes 8+ photos and builds a model with COLMAP (`tools/scan_pipeline.py`, environment `.venv-scan`).
Every surface point remembers where it came from:

| Source | How it's made | Turn off with |
|---|---|---|
| Measured | Triangulated from the photos by COLMAP | can't be turned off |
| Outline | The shape left after carving away everything outside the piece's outline in each photo (`tools/scan_outlines.py`, `tools/scan_hull.py`). Works on plain and clear objects, but can't show dents or hollows. | can't be turned off |
| AI depth | Depth Anything V2 Small (`tools/scan_depth_model.py`, downloads about 100 MB on first use), fitted per photo to measured points and kept only where several photos agree (`tools/scan_depth_help.py`). Fills plain faces. | `ai_depth=false` |
| AI fill | Hunyuan3D-2mv model made from 4 of the photos, lined up with the scan, used only in holes (`tools/scan_fill.py`). Fills unseen areas like the underside. | `ai_fill=false` |

What protects the measurements:
- The model's size only ever comes from measured points, and nothing can stick out past the box they span.
- The gap fill never remeshes the scan's surface. It adds patches and only moves the patches to close the seam.
- A photo whose depth fit misses hidden measured points by too much is dropped. Too few photos left means no AI depth.
- AI fill is rejected if the two shapes don't line up, and it skips spots the cameras saw straight past (like a through-hole).
- If an AI step fails, the scan still finishes with a warning.

Each scan's files: `model.glb`, `model.fbx`, `model_measured.glb` (measured points only) and `model_sources.glb` (colored
blue measured, orange AI depth, pink AI fill, grey smoothed over by the mesher). `report.surface_pct` gives the split.

Test against a part of known size, using rendered photos:

```bash
.venv-unirig/bin/python tools/scan_test_render.py /tmp/part --surface speckled   # or plain
.venv-scan/bin/python tools/scan_pipeline.py /tmp/part/photos /tmp/run --longest-mm 60 --ai-python .venv/bin/python
.venv-scan/bin/python tools/scan_test_compare.py /tmp/run/model.glb /tmp/part/truth.npz /tmp/run/model_sources.glb
```

## Checking models with Autodesk's FBX SDK

`tools/check_fbx.py` opens FBX files with Autodesk's own FBX SDK (the library Maya and 3ds Max use) and
reports meshes, UVs, materials, textures, bones and skin weights. It exits non-zero on problems such as
unweighted vertices or a texture with no file extension. Setup is in the script's docstring (Linux,
Python 3.10, no Autodesk account needed).

```bash
.venv/bin/python tools/check_fbx.py ../backend/data/jobs/<job id>/*.fbx
```

Both FBX exports go through `tools/blender_tidy.py` first, which names the mesh `Model`, welds duplicate
vertices (the rig step used to leave 5x the vertices), and names the embedded texture `Model_basecolor.png`.

## Endpoints

- `POST /tasks` `{"image_url": "data:image/jpeg;base64,...", "category": "prop", "texture": true}` → `{"result": "<task id>"}`.
  `image_url` is the front view. Add `"views": {"left": "data:...", "back": "data:...", "right": "data:..."}`
  (any subset) for the multi-view model; left/right are the object's own sides.
- `GET /tasks/{id}` → `{"status": "PENDING|IN_PROGRESS|SUCCEEDED|FAILED", "progress": 0-100, "stage": "...", "seconds": 71.2, "faces": 40000, ...}`
- `GET /tasks/{id}/files/model.glb`, `model.fbx`, `cutout.png`
- `POST /tasks/{id}/rig` (on a finished generation task) → `{"result": "<rig task id>"}`; poll it like any task.
  Files: `rigged.glb`, `rigged.fbx`. `501` if UniRig isn't installed.
- `GET /health`

Tasks are kept in memory: restarting the service forgets them (the backend then fails the job, and Retry works).
