# SeniorProjectFall26

Photo-to-Game-Asset Pipeline (CSUN senior project, Fall 2026): take a photo of a real object and get a
game-ready 3D asset (mesh, texture, and a skeleton for characters and creatures) to import into Unity.

| Folder | What | Start here |
|---|---|---|
| `backend/` | Python API: photo checks, classifier, job queue | [`backend/README.md`](backend/README.md) |
| `generator/` | Our own 3D generator on a local GPU (Hunyuan3D-2 + UniRig) | [`generator/README.md`](generator/README.md) |
| `firebase/` | Firebase project: website, capture app, security rules | [`firebase/README.md`](firebase/README.md) (first-time setup guide) |
| `docs/API.md` | How apps talk to the backend | |

Proposal: `proposal-submission.pdf`. Deck: `presentation.pptx`.
