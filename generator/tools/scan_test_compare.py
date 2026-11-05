"""Compares a finished scan with the real part from scan_test_render.py and prints the error in millimeters.

Usage: .venv-scan/bin/python tools/scan_test_compare.py <model.glb> <truth.npz> [<model_sources.glb>]

Given the color-by-source copy of the model, it also splits the error by where each part of the surface came from.

Only surfaces a camera could see are compared (not the underside sitting on the table). The real part measures
60 x 35 x 20 mm, and a test scan should be scaled from the 60 only, so the 35 and 20 are a fair check.
"""
import json
import sys

import numpy as np
import trimesh
from scipy.spatial import cKDTree


def sample_surface(vertices, faces, count, seed=0):
    mesh = trimesh.Trimesh(vertices, faces, process=False)
    return trimesh.sample.sample_surface(mesh, count, seed=seed)[0]


def main() -> None:
    scene = trimesh.load(sys.argv[1])
    scan = scene.to_geometry() if isinstance(scene, trimesh.Scene) else scene
    truth = np.load(sys.argv[2])
    # Truth is z up, the scan is y up
    true_vertices = truth["vertices"][:, [0, 2, 1]] * (1, 1, -1)
    true_points = sample_surface(true_vertices, truth["faces"], 200_000)
    true_points = true_points[true_points[:, 1] > 0.0006]
    scan_points = sample_surface(scan.vertices, scan.faces, 200_000)
    scan_above = scan_points[scan_points[:, 1] > 0.0006]
    colored = None
    align_on = scan_above
    if len(sys.argv) > 3:
        loaded = trimesh.load(sys.argv[3], process=False)
        colored = loaded.to_geometry() if isinstance(loaded, trimesh.Scene) else loaded
        samples, _, colors = trimesh.sample.sample_surface(colored, 200_000, sample_color=True, seed=1)
        # Line up using measured surface only, so an estimated patch can't drag everything else out of place
        measured = np.all(colors[:, :3] == (47, 128, 237), axis=1) & (samples[:, 1] > 0.0006)
        if measured.sum() > 1000:
            align_on = samples[measured]

    # The scan's yaw is arbitrary: start ICP from all four quarter turns and keep the closest fit
    best = None
    subset = align_on[np.random.default_rng(0).choice(len(align_on), min(len(align_on), 20_000), replace=False)]
    true_tree = cKDTree(true_points)
    for quarter in range(4):
        angle = quarter * np.pi / 2
        start = np.eye(4)
        start[0, 0], start[0, 2], start[2, 0], start[2, 2] = np.cos(angle), np.sin(angle), -np.sin(angle), np.cos(angle)
        matrix, _, cost = trimesh.registration.icp(subset, true_points, initial=start, max_iterations=60, scale=False)
        if best is None or cost < best[1]:
            best = (matrix, cost)
    aligned = trimesh.transform_points(scan_above, best[0])

    scan_to_true = true_tree.query(aligned)[0] * 1000
    true_to_scan = cKDTree(aligned).query(true_points)[0] * 1000
    sizes = (scan.bounds[1] - scan.bounds[0]) * 1000
    by_source = {}
    if colored is not None:
        above = samples[:, 1] > 0.0006  # sampled, so big faces count for more than tiny ones
        error = true_tree.query(trimesh.transform_points(samples[above], best[0]))[0] * 1000
        names = {(47, 128, 237): "measured", (245, 158, 11): "ai_depth", (219, 39, 119): "ai_fill", (150, 150, 150): "smoothed"}
        for rgb, name in names.items():
            chosen = np.all(colors[above][:, :3] == rgb, axis=1)
            if chosen.sum() > 100:
                by_source[name] = {
                    "pct_of_surface": round(float(chosen.mean() * 100), 1),
                    "median": round(float(np.median(error[chosen])), 3),
                    "p95": round(float(np.percentile(error[chosen], 95)), 3),
                    "max": round(float(error[chosen].max()), 3),
                }
    print(json.dumps({
        "scan_size_mm_bbox": sorted((round(float(s), 2) for s in sizes), reverse=True),
        "true_size_mm": [60, 35, 20],
        "error_scan_to_true_mm": {
            "median": round(float(np.median(scan_to_true)), 3),
            "p95": round(float(np.percentile(scan_to_true, 95)), 3),
            "max": round(float(scan_to_true.max()), 3),
        },
        "missing_true_surface_pct_over_1mm": round(float(np.mean(true_to_scan > 1) * 100), 2),
        "error_by_source_mm": by_source,
    }, indent=2))


if __name__ == "__main__":
    main()
