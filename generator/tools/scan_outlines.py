"""Finds the object's outline in every photo, so the rest of the scan knows which pixels are the object.

Runs in the environment that has rembg (.venv), because the photogrammetry environment doesn't.
Usage: .venv/bin/python tools/scan_outlines.py <images dir> <outlines dir>

Writes one black and white PNG per photo, at <outlines dir>/<folder>/<photo name>.png. White is the object.
That's also the layout COLMAP expects if the outlines are handed to it as masks.
"""
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from rembg import new_session, remove
from scipy import ndimage

# The model works at about this size anyway, and full phone photos only make it slower
WORKING_SIDE = 1024


def biggest_blob(mask: np.ndarray) -> np.ndarray:
    """Bits of background sometimes get picked up too. The object is the biggest connected piece."""
    labels, count = ndimage.label(mask)
    if count < 2:
        return mask
    sizes = ndimage.sum(mask, labels, range(1, count + 1))
    return labels == int(np.argmax(sizes)) + 1


def main() -> int:
    images, outlines = Path(sys.argv[1]), Path(sys.argv[2])
    session = new_session("u2net", providers=["CPUExecutionProvider"])
    for photo in sorted(images.glob("*/*")):
        with Image.open(photo) as opened:
            full = opened.convert("RGB")
        small = full.copy()
        small.thumbnail((WORKING_SIDE, WORKING_SIDE))
        mask = remove(small, session=session, only_mask=True).resize(full.size, Image.BILINEAR)
        kept = biggest_blob(np.asarray(mask) > 128)
        folder = outlines / photo.parent.name
        folder.mkdir(parents=True, exist_ok=True)
        Image.fromarray((kept * 255).astype(np.uint8)).save(folder / f"{photo.name}.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
