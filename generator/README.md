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
