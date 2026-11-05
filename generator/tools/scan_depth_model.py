"""Runs a depth-from-one-photo model (Depth Anything V2 Small, Apache 2.0) over a folder of photos.

Usage: .venv/bin/python tools/scan_depth_model.py <photo dir> <out dir>

Writes one .npy per photo, same folder layout, same pixel size. The values are relative: bigger means closer, with
an unknown scale and offset. scan_depth_help.py is what ties them to real measurements. Lives in its own script
because it needs torch from .venv, while the rest of the scan runs in .venv-scan.
"""
import sys
from pathlib import Path

import numpy as np
import torch
from PIL import Image
from transformers import AutoImageProcessor, AutoModelForDepthEstimation

MODEL = "depth-anything/Depth-Anything-V2-Small-hf"
# The model's default is 518. Going higher keeps the edges of small parts sharper and still fits next to nothing else.
INPUT_SIDE = 770


def main() -> int:
    photos, out = Path(sys.argv[1]), Path(sys.argv[2])
    device = "cuda" if torch.cuda.is_available() else "cpu"
    processor = AutoImageProcessor.from_pretrained(MODEL)
    model = AutoModelForDepthEstimation.from_pretrained(MODEL).to(device).eval()
    paths = sorted(p for p in photos.rglob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    for path in paths:
        image = Image.open(path).convert("RGB")
        inputs = processor(images=image, size={"height": INPUT_SIDE, "width": INPUT_SIDE}, return_tensors="pt").to(device)
        with torch.no_grad():
            depth = model(**inputs).predicted_depth
        depth = torch.nn.functional.interpolate(
            depth.unsqueeze(1), size=image.size[::-1], mode="bicubic", align_corners=False
        )[0, 0]
        target = out / path.relative_to(photos).with_suffix(".npy")
        target.parent.mkdir(parents=True, exist_ok=True)
        np.save(target, depth.cpu().numpy().astype(np.float32))
    print(f"{len(paths)} depth maps", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
