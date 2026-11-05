"""Photogrammetry scans: runs tools/scan_pipeline.py and tools/scan_fill.py in their own environment (.venv-scan).

COLMAP's Python package brings its own CUDA libraries, so it stays apart from Hunyuan's environment.
"""
import json
import subprocess
from pathlib import Path

GENERATOR_DIR = Path(__file__).resolve().parent.parent
PYTHON = GENERATOR_DIR / ".venv-scan" / "bin" / "python"
SCRIPT = GENERATOR_DIR / "tools" / "scan_pipeline.py"
FILL_SCRIPT = GENERATOR_DIR / "tools" / "scan_fill.py"
# The environment with torch, for the depth model
AI_PYTHON = GENERATOR_DIR / ".venv" / "bin" / "python"
TIMEOUT_SECONDS = 60 * 60


class ScanError(RuntimeError):
    pass


def available() -> bool:
    return PYTHON.exists()


def run(photos: Path, work: Path, longest_mm: float | None, ai_depth: bool, progress) -> dict:
    """Returns the report. work/model.glb is the model. Raises ScanError with a message for the user."""
    work.mkdir(parents=True, exist_ok=True)
    cmd = [str(PYTHON), str(SCRIPT), str(photos), str(work)]
    if longest_mm:
        cmd += ["--longest-mm", str(longest_mm)]
    if ai_depth:
        cmd += ["--ai-python", str(AI_PYTHON)]
    failure = None
    log_lines: list[str] = []
    with (work / "scan.log").open("w") as log:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
        try:
            for line in process.stdout:
                log.write(line)
                log.flush()
                log_lines = (log_lines + [line.strip()])[-5:]
                if line.startswith("PROGRESS "):
                    _, pct, stage = line.strip().split(" ", 2)
                    progress(int(pct), stage)
                elif line.startswith("ERROR "):
                    failure = line[6:].strip()
            code = process.wait(timeout=TIMEOUT_SECONDS)
        finally:
            if process.poll() is None:
                process.kill()
    if failure:
        raise ScanError(failure)
    if code != 0:
        raise ScanError(f"Scan crashed (exit {code}): {' | '.join(log_lines)}")
    return json.loads((work / "report.json").read_text())


def pick_fill_photos(work: Path) -> dict[str, Path]:
    """The photos to give Hunyuan3D: {"front": path, and whichever of left/back/right there were shots from}."""
    done = subprocess.run([str(PYTHON), str(FILL_SCRIPT), "pick", str(work)], capture_output=True, text=True, errors="replace")
    if done.returncode != 0:
        raise ScanError(f"Couldn't pick photos for the gap fill: {done.stdout[-300:]} {done.stderr[-300:]}")
    return {view: Path(path) for view, path in json.loads(done.stdout.strip().splitlines()[-1]).items()}


def fill_gaps(work: Path, ai_model: Path) -> dict:
    """Uses the Hunyuan3D model for what the scan is missing. Returns the updated report."""
    with (work / "fill.log").open("w") as log:
        done = subprocess.run(
            [str(PYTHON), str(FILL_SCRIPT), "merge", str(work), str(ai_model)],
            stdout=log, stderr=subprocess.STDOUT, timeout=TIMEOUT_SECONDS,
        )
    if done.returncode != 0:
        raise ScanError(f"The gap fill crashed (exit {done.returncode}); see {work / 'fill.log'}")
    return json.loads((work / "report.json").read_text())
