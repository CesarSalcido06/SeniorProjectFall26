# Firebase setup guide

This folder is our Firebase project (`senior-project-7c405`): the website, the capture app, and the
security rules. It's written for a first-time Firebase user. Follow it top to bottom once and you'll have
the whole pipeline running on your own computer, with nothing deployed and nothing to pay for.

```
 Phone / browser                       Firebase                              Backend (Python)
 ─────────────────                     ───────────────────────               ─────────────────────────
 capture app      ── photos ──────────> Storage  uploads/{jobId}/front.jpg
 (public/capture) ── job document ────> Firestore jobs/{jobId}  ──notices──> firebase_sync.py claims it,
                  <── live progress ─── Firestore jobs/{jobId}  <──updates── runs the job (3D model, rig)
                  <── model files ───── Storage  jobs/{jobId}/model.glb <─── uploads the results
```

The apps never call the backend directly; Firebase sits in the middle. That means the phone app works from
anywhere once it's deployed, and the backend can stay on Cesar's PC with the GPU.

## What's in here

| Path | What it is |
|---|---|
| `firebase.json` | Firebase settings: which folder gets hosted, where the rules are, emulator ports |
| `.firebaserc` | Which Firebase project this folder belongs to (`senior-project-7c405`) |
| `public/` | Everything served on the website. `index.html` etc. is your current site, unchanged |
| `public/capture/` | The capture app: `index.html` (page), `app.js` (all the Firebase code), `firebase-config.js` (project settings, emulator switch) |
| `firestore.rules` | Who can read and write the database. Read the comments; they explain the job flow |
| `storage.rules` | Who can read and write files |
| `storage-cors.json` | Lets the live website download model files from Storage (only needed when going live) |

## Words you'll see

- **Firestore**: the database. It stores *documents* (like JSON objects) in *collections*. We use one
  collection, `jobs`, with one document per photo job.
- **Storage**: file storage (photos and 3D models).
- **Hosting**: serves `public/` as a website at https://senior-project-7c405.web.app.
- **Emulators**: fake versions of all of the above that run on your computer. They behave like the real
  thing, are free, and get wiped when you stop them. **Use these for all development.**
- **Security rules**: the code in `*.rules` that decides what a browser may do. The backend skips them,
  because it uses the Admin SDK (a privileged server-side library).

---

## 1. Install the tools (once)

You need four things. Check each one in a terminal (Windows: **PowerShell**; Mac: **Terminal**).

| Tool | Why | Check | Get it |
|---|---|---|---|
| Node.js 20 or newer (LTS) | runs the Firebase CLI | `node --version` | https://nodejs.org (the LTS installer) |
| Java 21 (JDK) | the Firestore emulator is written in Java | `java -version` | https://adoptium.net (Temurin 21, default options) |
| Python 3.10 or newer | runs the backend | Windows: `py --version` / Mac: `python3 --version` | https://python.org (Windows: tick **"Add python.exe to PATH"**) |
| Git | to get the code | `git --version` | https://git-scm.com |

Close and reopen the terminal after installing anything, so it picks up the new commands.

Then install the Firebase CLI (the `firebase` command):

```bash
npm install -g firebase-tools
firebase --version
```

> Windows: if PowerShell says *running scripts is disabled*, run this once and answer `Y`:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

You don't need to log in to Firebase for anything in steps 2 to 5. You only log in when deploying (step 7).

## 2. Get the code

```bash
git clone https://github.com/CesarSalcido06/SeniorProjectFall26.git
cd SeniorProjectFall26
```

(Already cloned? `git pull` instead.)

## 3. Set up the backend (once)

The backend is the Python server that does the work. On your machine it runs in **mock mode**: instead of
a real 3D model, every job produces a colored cube after a few seconds. That's all you need to build and
test the apps. (Real models need Cesar's GPU; see section 6.)

**Windows (PowerShell):**
```powershell
cd backend
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

**Mac:**
```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

`.venv` is a private folder of Python packages for this project. *Activate* it (the second command) in
every new terminal before running the backend; your prompt then starts with `(.venv)`.

Now open `backend/.env` in your editor. Change `GENERATOR=local` to `GENERATOR=mock`, and add these
three lines at the bottom:

```ini
GENERATOR=mock
FIREBASE_PROJECT_ID=senior-project-7c405
FIREBASE_EMULATORS=true
```

`.env` is ignored by Git, so your settings stay on your machine.

## 4. Run everything

You need **two terminals**, both left running.

**Terminal 1: the Firebase emulators**
```bash
cd SeniorProjectFall26/firebase
firebase emulators:start
```
Wait for `✔ All emulators ready!`. The first run downloads the emulators (about a minute).

**Terminal 2: the backend**
```bash
cd SeniorProjectFall26/backend
# activate the venv first: Windows .venv\Scripts\Activate.ps1   Mac source .venv/bin/activate
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```
Look for the line `Firebase: linked to senior-project-7c405 (emulators)`. If it isn't there,
`FIREBASE_PROJECT_ID` isn't set in `backend/.env`.

**Now open:**

| URL | What |
|---|---|
| http://localhost:5000/capture/ | The capture app. Pick a photo, press *Upload & generate*, and watch it finish |
| http://localhost:5000/ | Your website, served locally |
| http://localhost:4000 | **Emulator UI**: browse the `jobs` documents (Firestore tab) and files (Storage tab) live. Very useful |
| http://localhost:8000 | The backend's own test page and API (`docs/API.md`) |

Photos must be at least 512 px on the short side and reasonably sharp. Phone photos are fine. A rejected
photo shows the reason in the job list.

Stop either one with `Ctrl+C`. The emulators forget everything when stopped. That's normal.

## 5. Use it from your phone

1. Phone and computer on the **same Wi-Fi**.
2. Find your computer's local IP. Windows: `ipconfig` (the *IPv4 Address*, like `192.168.1.23`). Mac:
   System Settings > Wi-Fi > Details > IP address.
3. On the phone, open `http://<that IP>:5000/capture/`. *Front photo* opens the camera.

The app connects to the emulators on whichever computer served the page, so nothing else needs changing.
If the phone can't load the page, your firewall is blocking it. Windows shows an *Allow access* pop-up the
first time `java`/`node` start; allow it on **Private networks**. Mac: System Settings > Network > Firewall.

## 6. Real 3D models instead of cubes

The real generator needs an NVIDIA GPU with 12 GB, so it runs on Cesar's PC. Two ways to use it:

- **Easiest:** Cesar runs the backend with Firebase on his PC (same `.env` lines, but `GENERATOR=local`).
  Once the project is deployed (section 7), your capture app talks to the live Firebase, and his backend
  picks up the jobs. No networking needed between you.
- **On the same network:** in your `backend/.env`, set `GENERATOR=local` and
  `GENERATOR_URL=http://<Cesar's PC IP>:8100`, and Cesar starts the generator with `--host 0.0.0.0`.

## 7. Going live (deploying)

Do this when you want the real website to have the capture app. You need to be an **Owner** or **Editor**
on the Firebase project.

1. `firebase login` (opens a browser; use the Google account that owns the project).
2. **Firebase console** (https://console.firebase.google.com > `senior-project-7c405`):
   - **Upgrade to the Blaze plan** (bottom left, *Upgrade*). Storage requires it for every project now.
     It's pay-as-you-go with a free allowance (5 GB of storage), far more than we'll use, but it needs a
     card on file. Set a **budget alert** (e.g. $1) when it asks, so nothing surprises you.
   - **Build > Authentication > Get started > Sign-in method > Anonymous > Enable.** The capture app signs
     people in anonymously.
   - **Build > Firestore Database > Create database.** Pick a location near us (`us-west2`), start in
     *production mode* (our rules replace the default ones).
   - **Build > Storage > Get started.** Same location.
3. Deploy, from the `firebase/` folder:
   ```bash
   firebase deploy
   ```
   That uploads the website (`public/`, your current site plus `/capture/`), the Firestore rules and the
   Storage rules. Just one part: `firebase deploy --only hosting` (or `firestore:rules`, `storage`).
   Your homepage files in `public/` are a copy of what's live now, so deploying doesn't change it.
4. Let the website download model files from Storage (one time). In Google Cloud Shell
   (the `>_` icon at the top right of https://console.cloud.google.com), upload `storage-cors.json` and run:
   ```bash
   gcloud storage buckets update gs://senior-project-7c405.firebasestorage.app --cors-file=storage-cors.json
   ```
5. **Connect the backend to the live project** (on whichever PC runs it, usually Cesar's):
   - Console > Project settings > **Service accounts** > *Generate new private key*. This downloads a JSON
     file that gives **full access to the project**. Keep it outside the repo and never commit it or share it
     in chat. (The `.gitignore` blocks the usual file names, but don't rely on that.)
   - In `backend/.env`:
     ```ini
     FIREBASE_PROJECT_ID=senior-project-7c405
     FIREBASE_EMULATORS=false
     FIREBASE_CREDENTIALS=C:/Users/you/keys/senior-project-7c405-firebase-adminsdk.json
     ```
6. Open https://senior-project-7c405.web.app/capture/ on your phone. The header should say
   *Live Firebase project*.

To test the deployed page against your local emulators, or a local page against the live project, add
`?emulators=1` or `?emulators=0` to the URL.

## 8. Building on this

- **Restyle the capture app:** change `public/capture/index.html` freely. Keep the element `id`s that
  `app.js` uses (`photo-front`, `photo-left`, `photo-back`, `photo-right`, `category`, `force`, `submit`,
  `msg`, `jobs`, `mode`, `preview`), or update `app.js` to match.
- **Desktop hub:** use the Firebase JS SDK the same way `app.js` does, but list every job:
  `onSnapshot(collection(db, "jobs"), ...)`. Each document's fields are described at the top of
  `backend/app/firebase_sync.py`; `files[].path` is the Storage path to download (`getDownloadURL`).
- **Adding a field the app writes** (for example a job name): add it to the `setDoc` in `app.js` **and**
  to the `hasOnly([...])` list in `firestore.rules`. Otherwise the write is refused with
  `permission-denied`. Rule changes take effect in the emulators as soon as you save.
- **Unity:** download `rigged.fbx` / `model.fbx` from `files`. `docs/API.md` says which to import per category.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `firebase: command not found` / not recognized | Reopen the terminal after `npm install -g firebase-tools`. Windows: see the execution-policy note in section 1 |
| Emulators: `Could not spawn java` or a Java version error | Install Java 21 (section 1) and reopen the terminal |
| Emulators: `port taken` | Something else uses 4000/5000/8080/9099/9199. Close it, or change the port in `firebase.json` (and in `firebase-config.js` and `backend/.env`) |
| Capture app says *Couldn't sign in* | The emulators aren't running, or (live) Anonymous sign-in isn't enabled |
| Job stays on **uploaded** | The backend isn't running, isn't linked (no `Firebase: linked` line), or points at the other Firebase (emulators vs live) |
| Job **rejected** | The photo failed the quality check; the reason is shown. Tick *Skip blur/lighting check* to override |
| `permission-denied` in the browser console | The security rules refused a write. Check the fields against `firestore.rules` |
| Backend: `ModuleNotFoundError: firebase_admin` | Activate the venv, then `pip install -r requirements.txt` |
| Backend: `DefaultCredentialsError` | `FIREBASE_EMULATORS=false` but no `FIREBASE_CREDENTIALS`. For local work set `FIREBASE_EMULATORS=true` |
| Live site: model doesn't show, but the links work | Storage CORS isn't set (section 7, step 4) |

## Tests

The backend has automated tests for this link, run against real emulators. From `firebase/`:

```bash
firebase emulators:exec --only firestore,storage "cd ../backend && python -m pytest -q tests/test_firebase.py"
```

`emulators:exec` starts the emulators, runs the command, and stops them again.
