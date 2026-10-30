"""Auto-rigging with UniRig (VAST-AI, MIT license): predicts a skeleton and skin weights for any model.

UniRig needs Python 3.11 and Blender-as-a-module (bpy 4.2), so it runs in its own environment
(.venv-unirig) as subprocesses: skeleton -> skin weights -> merge onto the textured mesh -> clean up.
Its general-purpose model names bones bone_0..bone_N, so Unity imports the result as a Generic rig.
"""
import os
import subprocess
from pathlib import Path

GENERATOR_DIR = Path(__file__).resolve().parent.parent
REPO = GENERATOR_DIR / "vendor" / "UniRig"
VENV_BIN = GENERATOR_DIR / ".venv-unirig" / "bin"
TOOLS = GENERATOR_DIR / "tools"


class StepFailed(RuntimeError):
    pass


def available() -> bool:
    return REPO.exists() and (VENV_BIN / "python").exists()


def _run(cmd: list, log: Path, cwd: Path = REPO, timeout: int = 900) -> None:
    env = {**os.environ, "PATH": f"{VENV_BIN}:{os.environ['PATH']}"}
    with log.open("a") as fh:
        fh.write(f"\n$ {' '.join(map(str, cmd))}\n")
        fh.flush()
        result = subprocess.run(cmd, cwd=cwd, env=env, stdout=fh, stderr=subprocess.STDOUT, timeout=timeout)
    if result.returncode != 0:
        tail = log.read_text().strip().splitlines()[-5:]
        raise StepFailed(f"{Path(cmd[1]).name} exited {result.returncode}: {' | '.join(tail)}")


def to_fbx(glb: Path, fbx: Path, log: Path) -> None:
    """Static models: GLB -> FBX with embedded textures."""
    _run([VENV_BIN / "python", TOOLS / "glb_to_fbx.py", glb, fbx], log, cwd=GENERATOR_DIR, timeout=300)


def rig(model_glb: Path, work_dir: Path, progress) -> tuple[Path, Path]:
    """Returns (rigged.glb, rigged.fbx) in work_dir's parent."""
    work_dir.mkdir(parents=True, exist_ok=True)
    log = work_dir / "unirig.log"
    skeleton, skin, merged = work_dir / "skeleton.fbx", work_dir / "skin.fbx", work_dir / "merged.glb"
    out_glb, out_fbx = work_dir.parent / "rigged.glb", work_dir.parent / "rigged.fbx"

    progress(10, "predicting skeleton")
    _run(["bash", "launch/inference/generate_skeleton.sh", "--input", model_glb, "--output", skeleton], log)
    progress(40, "predicting skin weights")
    _run(["bash", "launch/inference/generate_skin.sh", "--input", skeleton, "--output", skin], log)
    progress(85, "binding rig to model")
    # Merge the skin result (not the skeleton) or the output has bones but no weights
    _run(["bash", "launch/inference/merge.sh", "--source", skin, "--target", model_glb, "--output", merged], log)
    progress(95, "exporting")
    _run([VENV_BIN / "python", TOOLS / "finalize_rig.py", merged, out_glb, out_fbx], log, cwd=GENERATOR_DIR, timeout=300)
    return out_glb, out_fbx
