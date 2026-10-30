# Backend: photo → game asset

FastAPI service that takes a photo, checks it, classifies the object (prop / character / creature),
sends it to Meshy for a textured 3D model (plus a humanoid rig for characters), and stores the files
for the desktop hub to import. The API contract is in [`../docs/API.md`](../docs/API.md).

It also serves a bare-bones test page at `/` for uploading from a phone and previewing results.
That page is only for testing; the real phone app and desktop hub replace it.

## Run it

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate   # optional
pip install -r requirements.txt
cp .env.example .env                                 # optional; defaults run fully offline
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` on the computer, or `http://<computer's LAN IP>:8000` on a phone on the same Wi-Fi
(the file picker opens the camera).

With no `MESHY_API_KEY`, Meshy runs in **mock mode**: each job takes ~6 s and returns a colored placeholder cube
(orange = prop, blue = character, green = creature), so everything downstream can be built without spending credits.

## Tests

```bash
cd backend && python3 -m pytest -q
```

They run the whole pipeline against the mock: upload → classify → generate → rig → download.
The Firebase tests (`tests/test_firebase.py`) skip unless the emulators are running; see
[`../firebase/README.md`](../firebase/README.md#tests).

## Firebase

Set `FIREBASE_PROJECT_ID` (see `.env.example`) and the backend also works through Firebase: the capture app in
`firebase/public/capture/` uploads photos to Storage and creates a Firestore document, and the backend picks it up.
Every job, however it arrived, is copied to Firestore `jobs/{id}` with its files in Storage. Step-by-step setup,
including running it all locally on the emulators, is in [`../firebase/README.md`](../firebase/README.md).

## Layout

| File | What it does |
|---|---|
| `app/main.py` | HTTP routes |
| `app/images.py` | Rejects blurry/dark/tiny photos, fixes rotation, strips EXIF, resizes to ≤2048 px JPEG |
| `app/classifier.py` | Vision-model prompt and call (any OpenAI-compatible endpoint) |
| `app/intake.py` | Checks every uploaded photo and creates the job (shared by the API and Firebase) |
| `app/meshy.py` | Generator clients: Meshy, the local GPU generator, and the offline mock |
| `app/firebase_sync.py` | Optional Firebase link: takes jobs from the capture app, mirrors every job to Firestore + Storage |
| `app/worker.py` | Background job runner; resumes unfinished jobs on restart |
| `app/db.py` | SQLite job store |
| `app/static/index.html` | Test GUI |

## Classifier

`CLASSIFIER=manual` (default) uses the category chosen at upload. `CLASSIFIER=openai` sends the photo to any
OpenAI-compatible vision model. On Cesar's PC that's BenchAI's Qwen2-VL (MegaMan.EXE), which got 7/7 right on
the deck images (six scans → prop, the sculpted head → character). The first load takes ~30 s while it swaps models in.
If the classifier fails or answers nonsense, the job continues as a prop and the reason is in `classifier_note`.
