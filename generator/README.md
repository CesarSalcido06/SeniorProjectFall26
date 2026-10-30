# Local 3D generator

Our own replacement for Meshy's image-to-3D step, running on a local NVIDIA GPU with open-weight models:

| Step | Model | License |
|---|---|---|
| Background removal | rembg (U²-Net) | MIT |
| Photo → shape | Hunyuan3D-2mini turbo (0.6B) | Tencent Hunyuan 3D 2.0 Community License |
| Shape → texture | Hunyuan3D-Paint v2.0 turbo | same |

The Hunyuan license is free for us. It doesn't apply in the EU, UK, or South Korea, and organizations over
1M monthly users need a separate license.

## How it fits

```
backend (GENERATOR=local) ──HTTP──> generator/server.py :8100 ──> GPU
```

The backend's `LocalGeneratorClient` (`backend/app/meshy.py`) creates a task, polls it, and downloads
`model.glb` (textured) and `cutout.png` (the photo with its background removed, used as the thumbnail).
The API mirrors Meshy's, so nothing else in the backend changes.

One task runs at a time. Models load on the first task and unload after `GPU_IDLE_SECONDS` without work.

## Setup (once)

Needs Linux, an NVIDIA GPU with 12 GB+, CUDA toolkit 12.4 (`/usr/local/cuda-12.4`), and [uv](https://docs.astral.sh/uv/).

```bash
generator/setup.sh
```

The first run downloads about 10 GB of model weights into `~/.cache/hy3dgen` and `~/.cache/huggingface`.

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

- `POST /tasks` `{"image_url": "data:image/jpeg;base64,...", "category": "prop", "texture": true}` → `{"result": "<task id>"}`
- `GET /tasks/{id}` → `{"status": "PENDING|IN_PROGRESS|SUCCEEDED|FAILED", "progress": 0-100, "stage": "...", "seconds": 71.2, "faces": 40000, ...}`
- `GET /tasks/{id}/files/model.glb`, `.../cutout.png`
- `GET /health`

Tasks are kept in memory: restarting the service forgets them (the backend then fails the job, and Retry works).
