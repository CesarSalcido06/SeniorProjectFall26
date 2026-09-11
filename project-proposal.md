# Project Proposal: Photo-to-Game-Asset Pipeline

## 1. Overview

A system that takes a photo (or short video scan) of a real-world object from a phone, reconstructs it as a textured, rigged 3D asset, and prepares it for direct use in a game engine — starting with Unity, with Blender and Unreal as stretch targets.

The core insight: single-photo 3D reconstruction, PBR texturing, and auto-rigging are largely solved problems available via existing AI APIs (e.g. Meshy). The project's real contribution is the **automation and integration layer** — object classification to route the pipeline correctly, and engine-specific import scripts that turn a generic downloaded mesh into a properly configured, ready-to-use game asset (prefab, animator controller, collider) with no manual cleanup.

## 2. Problem Statement

Turning a real object into a usable game asset today requires: photographing/scanning it, manually running it through a 3D generation tool, manually rigging or fixing rigging, manually texturing, and manually importing and configuring it inside a target engine. Each step is a context switch and a skill barrier. This project collapses that into: take a photo, pick a target, get a ready-to-use asset.

## 3. System Architecture

### 3.1 High-level flow

```
Phone web app (capture & upload)
        ↓
Backend (object classification + Meshy API orchestration)
        ↓
Desktop app hub (job queue, target picker, GUI)
        ↓
Target engine (Unity / Blender / Unreal)
```

### 3.2 Components

| Component | Role | Primary language/stack |
|---|---|---|
| Phone web app | Camera capture, upload, job status display | JavaScript/TypeScript, React |
| Backend API | Object classification, Meshy API calls, job storage/polling | Node.js (Express) or Python (FastAPI) |
| Object classifier | Categorize capture as prop vs. creature vs. character to route rig/pose options | Python (pretrained vision model or vision-capable LLM call) |
| Desktop app hub | Job queue GUI, target picker, 3D preview, triggers handoff to target engine | Electron or Tauri + React, three.js for preview |
| Unity Editor package | Live-GUI plugin inside Unity; polls backend, imports asset, builds prefab/animator/collider | C# (Unity Editor API) |
| Blender headless script | Runs without opening Blender's GUI; imports mesh, applies rig, saves `.blend` | Python (`bpy`) |
| Unreal headless script (stretch) | CLI-driven import via Unreal's Python API, no GUI required | Python (Unreal Python API) |
| External AI service | Single-image to 3D mesh, PBR texturing, auto-rigging, animation presets | Meshy API (or comparable: Tripo, Rodin) |

### 3.3 Engine handoff detail

The desktop hub does not import assets itself for Unity — it notifies an already-running Unity Editor package (via local HTTP call or a watched file) to pull and import the job. For Blender and Unreal, the desktop hub runs the headless import script directly as a subprocess, since neither app needs to be open.

## 4. Languages & Technologies Summary

- **Frontend (web capture app)**: JavaScript/TypeScript, React
- **Backend**: Node.js or Python, REST API, job queue/polling logic
- **Classifier**: Python, pretrained image classification model or vision LLM API call
- **Desktop app**: JavaScript/TypeScript (Electron) or Rust + JS (Tauri), React for UI, three.js for 3D preview
- **Unity integration**: C#, Unity Editor scripting API (`EditorWindow`, `AssetDatabase`)
- **Blender integration**: Python, `bpy` module, Blender's background/headless mode
- **Unreal integration (stretch)**: Python, Unreal Editor's Python scripting API, command-line editor invocation
- **External API**: Meshy AI (image-to-3D, texturing, auto-rigging, animation) via REST API
- **Storage**: cloud object storage for generated assets (S3, Firebase Storage, or equivalent), plus a lightweight database for job metadata (Postgres, SQLite, or Firestore)

## 5. Scope & MVP Definition

**MVP (must-have)**
1. Phone web app: capture and upload a photo
2. Backend: classify object, call Meshy API, track job status
3. Desktop app: GUI showing job queue, preview, and a "send to Unity" action
4. Unity Editor package: pulls completed job, imports mesh, configures prefab/animator/collider

**Stretch goals**
1. Blender headless script as a second working target (proves the architecture generalizes beyond Unity)
2. Unreal headless script as a third target
3. Websocket-based push notification from backend to desktop app instead of polling
4. Batch processing (multiple objects in one session)

**Explicitly out of scope**
- Building a custom 3D reconstruction, texturing, or rigging model from scratch
- Multi-object scene reconstruction (single object per capture only)
- Real-time capture-to-asset (some processing latency via Meshy is expected and acceptable)

## 6. Team & Role Allocation (example for 3–4 members)

| Role | Owns | Notes |
|---|---|---|
| Member A | Phone web app + backend API | React + Node/Python; leverages existing web dev experience |
| Member B | Desktop app GUI | Electron/Tauri + React + three.js preview panel |
| Member C | Unity Editor package | C#; leverages existing Unity/Photon Fusion experience |
| Member D (if available) | Blender headless script (stretch) | Python/`bpy`; easiest of the two remaining headless targets |

## 7. Milestones / Sprint Plan

1. **Spike & scope lock**: manually validate the Meshy API pipeline end-to-end (upload photo → get rigged FBX back); confirm credit costs and rate limits; lock target engine to Unity for MVP.
2. **Capture & upload**: build the phone web app and backend upload/job endpoint.
3. **Classifier**: integrate a lightweight image classifier to route Meshy request parameters (pose type, rig style).
4. **Desktop app hub**: build job queue GUI, target picker, and preview pane.
5. **Unity Editor package**: build the C# import script — prefab creation, Animator Controller setup, collider assignment.
6. **Integration & demo**: connect all pieces end-to-end; produce a demo reel of several real objects going from photo to playable Unity prefab.
7. **Stretch work** (if time remains): Blender and/or Unreal headless targets.

## 8. Risks & Open Questions

- **API cost/limits**: Meshy is credit-metered; need to confirm a sustainable usage budget for development and live demos.
- **Rig quality**: auto-rigging works well for generic bipeds/quadrupeds but may need manual fallback for irregular objects — decide how the pipeline should degrade gracefully (e.g. skip rigging for static props).
- **Classification accuracy**: misclassifying an object (e.g. creature vs. prop) could send the wrong parameters to Meshy — needs a fallback/manual override in the desktop GUI.
- **Round-trip testing**: exported FBX/GLB behavior varies by engine version — test the full export-to-import path early and often rather than assuming compatibility.

## 9. Success Criteria

- A user can photograph a real object, select a target engine, and receive a properly configured, importable asset with no manual file editing.
- At least one full end-to-end demo works reliably for both a static prop and a rigged creature/character example.
- The Unity path is fully automated (capture → asset in a working prefab) by the final demo.
