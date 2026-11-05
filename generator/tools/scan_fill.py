"""Fills in what a scan couldn't see (like the underside) using a Hunyuan3D model of the same piece.

Runs after scan_pipeline.py, in two steps, with the generator making the Hunyuan3D model in between:
  scan_fill.py pick <work dir>           picks 4 photos around the piece and crops them to <work dir>/ai_views
  scan_fill.py merge <work dir> <ai.glb> lines the model up with the scan and uses it where the scan has nothing

The rules that keep the scan's accuracy:
  - The scan's surface is never remeshed or moved. Hunyuan's model is only used for patches in its holes.
  - The size comes from the scan. Hunyuan's model is stretched to fit it, and can't stick out past it.
  - If a camera saw straight past a spot (like the table through a hole), nothing gets added there.
  - Patches need Hunyuan points behind them. Only the base on the table may be closed with nothing behind it.
  - If the two shapes don't line up well, the fill gets rejected and the scan is left as it was.
Prints one JSON line. Merging adds to report.json and replaces model.glb and model_sources.glb.
"""
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pycolmap
import trimesh
from PIL import Image
from scipy.spatial import cKDTree

from scan_pipeline import AI_FILL, build_mesh, export_models, read_cloud, write_cloud  # noqa: F401

# The fill is thrown out if the model misses the scan's points by more than this, as a fraction of the longest side
MAX_FIT_ERROR = 0.035
# A Hunyuan point only gets used this far (fraction of longest side) from anything the scan has
GAP_DISTANCE = 0.02
# Mesh within this distance (fraction of longest side) of a real point counts as backed by data
REACH = 0.012


def camera_right(forward: np.ndarray) -> np.ndarray:
    right = np.cross(forward, [0.0, 1.0, 0.0])
    return right / np.linalg.norm(right)


def pick(work: Path) -> dict:
    """Front, left, back and right photos of the piece, the way Hunyuan's multi-view model wants them."""
    views = json.loads((work / "views.json").read_text())
    frame = np.load(work / "frame.npz")
    center = np.array([0.0, float(frame["height"]) / 2, 0.0])
    positions = np.array([v["position"] for v in views]) - center
    flat = np.linalg.norm(positions[:, [0, 2]], axis=1)
    elevations = np.degrees(np.arctan2(positions[:, 1], flat))
    # Front looks at the piece's long side. "Left" means the piece's left: the camera steps to its own right.
    directions = {
        "front": np.array([0.0, 0, 1]),
        "left": camera_right(np.array([0.0, 0, -1])),
        "back": np.array([0.0, 0, -1]),
        "right": -camera_right(np.array([0.0, 0, -1])),
    }
    out = work / "ai_views"
    out.mkdir(exist_ok=True)
    picked, used = {}, set()
    for name, direction in directions.items():
        azimuth_off = np.degrees(np.arccos(np.clip(positions[:, [0, 2]] @ direction[[0, 2]] / np.maximum(flat, 1e-9), -1, 1)))
        score = azimuth_off + 0.5 * np.abs(elevations - 25)
        for index in np.argsort(score):
            if index not in used:
                break
        if azimuth_off[index] > 40:  # nothing shot from this side: Hunyuan is better off without a poor stand-in
            continue
        used.add(index)
        with Image.open(work / "images" / views[index]["name"]) as photo:
            # Crop to the piece so background removal has less to get wrong
            left, top, right, bottom = views[index]["box"]
            side = max(right - left, bottom - top) * 1.4
            mid_x, mid_y = (left + right) / 2, (top + bottom) / 2
            box = [mid_x - side / 2, mid_y - side / 2, mid_x + side / 2, mid_y + side / 2]
            photo.convert("RGB").crop([int(v) for v in box]).save(out / f"{name}.jpg", quality=95)
        picked[name] = str(out / f"{name}.jpg")
    if "front" not in picked:
        raise ValueError("No photo looks at the front of the piece")
    return picked


class SeenThrough:
    """Answers: did a camera see something measured behind this spot? If so, the spot has to be empty air."""

    CELL = 8  # pixels per grid cell
    MIN_VOTES = 3  # one or two stray far-off points shouldn't carve anything

    def __init__(self, work: Path, frame, longest: float) -> None:
        self.rotation, self.shift, self.scale = frame["rotation"], frame["shift"], float(frame["scale"])
        self.tolerance = longest * 0.015 / self.scale  # back in COLMAP's units, which is what the cameras use
        self.reconstruction = pycolmap.Reconstruction(work / "dense" / "sparse")
        cloud = read_cloud(work / "fused.ply")  # everything measured, table included
        world = np.stack([cloud["x"], cloud["y"], cloud["z"]], axis=1).astype(np.float64)
        # A point only counts for the photos that really saw it. Table points hidden behind the piece in this photo
        # came from other photos, and would make the piece look see-through.
        seen_by = self._read_visibility(work / "fused.ply.vis")
        by_name = {image.name: image for image in self.reconstruction.images.values()}
        names = (work / "dense" / "stereo" / "fusion.cfg").read_text().split()  # the order COLMAP's numbers refer to
        self.views = []
        for index, name in enumerate(names):
            image = by_name.get(name)
            if image is not None and image.has_pose:
                self.views.append(self._nearest_seen(image, world[seen_by.get(index, np.empty(0, np.uint32))]))

    @staticmethod
    def _read_visibility(path: Path) -> dict[int, np.ndarray]:
        """COLMAP's .vis file: point count, then for each point how many photos saw it and which ones."""
        raw = np.fromfile(path, dtype=np.uint32)
        count = int(raw[0]) | (int(raw[1]) << 32)
        photos, owners, position = [], [], 2
        for point in range(count):
            many = int(raw[position])
            photos.append(raw[position + 1 : position + 1 + many])
            owners.append(np.full(many, point, dtype=np.uint32))
            position += 1 + many
        photos, owners = np.concatenate(photos), np.concatenate(owners)
        return {int(photo): owners[photos == photo] for photo in np.unique(photos)}

    def _project(self, pose, camera, points):
        cam = points @ pose[:3, :3].T + pose[:3, 3]
        z = cam[:, 2]
        fx, fy, cx, cy = camera.params
        safe = np.where(z > 1e-9, z, np.nan)
        col = np.floor((fx * cam[:, 0] / safe + cx) / self.CELL)
        row = np.floor((fy * cam[:, 1] / safe + cy) / self.CELL)
        rows, cols = camera.height // self.CELL + 1, camera.width // self.CELL + 1
        inside = (col >= 0) & (col < cols) & (row >= 0) & (row < rows)
        return np.where(inside, row * cols + col, -1).astype(np.int64), z, rows * cols

    def _nearest_seen(self, image, points: np.ndarray):
        pose = image.cam_from_world().matrix()
        camera = self.reconstruction.cameras[image.camera_id]
        cell, z, total = self._project(pose, camera, points)
        seen = np.full(total, np.inf)
        np.minimum.at(seen, cell[cell >= 0], z[cell >= 0])
        return pose, camera, seen

    def share(self, final_points: np.ndarray) -> np.ndarray:
        """For each point (in the final frame): of the cameras with data there, the share that saw past it."""
        world = (final_points / self.scale - self.shift) @ self.rotation
        past, total = np.zeros(len(world)), np.zeros(len(world))
        for pose, camera, seen in self.views:
            cell, z, _ = self._project(pose, camera, world)
            hit = cell >= 0
            nearest = np.full(len(world), np.inf)
            nearest[hit] = seen[cell[hit]]
            has_data = np.isfinite(nearest)
            past += has_data & (z < nearest - self.tolerance)
            total += has_data
        return np.where(past >= self.MIN_VOTES, past / np.maximum(total, 1), 0.0)

    def calibrate(self, solid_points: np.ndarray) -> None:
        """Sets how strict to be, from points we know are on solid surface.

        On a plain face COLMAP's depth can snap to the table behind it, so cameras seem to see through solid plastic.
        How often that happens varies a lot from scan to scan. Tested against known shapes, one fixed limit either
        carved away good surface on plain parts or kept bad surface on textured ones. So each scan measures its own.
        """
        self.limit = max(float(np.percentile(self.share(solid_points), 95)), 0.35)

    def __call__(self, final_points: np.ndarray) -> np.ndarray:
        """True for each point that cameras saw straight past, beyond what this scan does on solid surface."""
        return self.share(final_points) > self.limit


def rotations_of_a_cube() -> list[np.ndarray]:
    """The 24 ways to turn a box. Hunyuan's model comes out square to its axes, but which way it faces varies."""
    out = []
    for axes in ((0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0)):
        for signs in np.ndindex(2, 2, 2):
            matrix = np.zeros((3, 3))
            for row, (axis, sign) in enumerate(zip(axes, signs)):
                matrix[row, axis] = 1 if sign else -1
            if np.linalg.det(matrix) > 0:
                out.append(matrix)
    return out


def best_fit(source: np.ndarray, target: np.ndarray) -> tuple[np.ndarray, float, np.ndarray]:
    """Rotation, scale and shift that best move source onto target (matched point pairs)."""
    source_mid, target_mid = source.mean(axis=0), target.mean(axis=0)
    a, b = source - source_mid, target - target_mid
    u, s, vt = np.linalg.svd(b.T @ a)
    fix = np.diag([1, 1, np.sign(np.linalg.det(u @ vt))])
    rotation = u @ fix @ vt
    scale = float((s * np.diag(fix)).sum() / (a**2).sum())
    return rotation, scale, target_mid - scale * rotation @ source_mid


def align(ai_points: np.ndarray, scan_points: np.ndarray, longest: float) -> tuple[np.ndarray, float]:
    """Moves the Hunyuan points onto the scan. Returns the moved points and how far off the fit is, in scan units."""
    center = (ai_points.max(axis=0) + ai_points.min(axis=0)) / 2
    start = (ai_points - center) * (longest / np.ptp(ai_points, axis=0).max())
    goal_center = (scan_points.max(axis=0) + scan_points.min(axis=0)) / 2
    scan_tree = cKDTree(scan_points)

    best, best_score = None, np.inf
    for turn in rotations_of_a_cube():
        moved = start @ turn.T + goal_center
        for _ in range(25):
            distance, nearest = cKDTree(moved).query(scan_points)
            keep = distance <= np.percentile(distance, 85)
            rotation, scale, shift = best_fit(moved[nearest[keep]], scan_points[keep])
            scale = np.clip(scale, 0.7, 1.4)
            moved = scale * moved @ rotation.T + shift
        # Scan points must land on the model, and the model must land on the scan (except the parts the scan lacks)
        there = np.percentile(cKDTree(moved).query(scan_points)[0], 85)
        back = np.percentile(scan_tree.query(moved)[0], 60)
        if there + back < best_score:
            best, best_score = moved, there + back

    # Hunyuan's proportions are off even when its shape is right, so stretch each axis on its own to match
    moved = best
    for _ in range(10):
        distance, nearest = cKDTree(moved).query(scan_points)
        keep = distance <= np.percentile(distance, 85)
        for axis in range(3):
            slope, offset = np.polyfit(moved[nearest[keep], axis], scan_points[keep, axis], 1)
            moved[:, axis] = np.clip(slope, 0.6, 1.6) * moved[:, axis] + offset
    distance = cKDTree(moved).query(scan_points)[0]
    return moved, float(np.percentile(distance, 85))


def bend_to_scan(moved: np.ndarray, scan_points: np.ndarray, longest: float) -> np.ndarray:
    """Nudges the model so it meets the scan with no step at the seam. The nudge fades out away from the scan."""
    nearest = cKDTree(moved).query(scan_points)[1]
    pull = scan_points - moved[nearest]
    fine = np.linalg.norm(pull, axis=1) < longest * 0.08  # anything further than this isn't really the same spot
    width = longest * 0.05
    distance, index = cKDTree(scan_points[fine]).query(moved, k=min(24, int(fine.sum())))
    weight = np.exp(-((distance / width) ** 2))
    average = (pull[fine][index] * weight[..., None]).sum(axis=1) / np.maximum(weight.sum(axis=1, keepdims=True), 1e-9)
    return moved + average * np.exp(-((distance[:, 0] / (2 * width)) ** 2))[:, None]


def merge(work: Path, ai_file: Path) -> dict:
    frame = np.load(work / "frame.npz")
    points, normals, colors, source = frame["points"], frame["normals"], frame["colors"], frame["source"]
    longest = float(frame["longest"])
    report_path = work / "report.json"
    report = json.loads(report_path.read_text())

    loaded = trimesh.load(ai_file, process=False)
    ai_mesh = loaded.to_geometry() if isinstance(loaded, trimesh.Scene) else loaded
    rng = np.random.default_rng(0)
    scan_subset = points[rng.choice(len(points), min(len(points), 12_000), replace=False)]
    ai_samples, faces = trimesh.sample.sample_surface(ai_mesh, 60_000, seed=0)
    ai_normals = ai_mesh.face_normals[faces]

    moved, fit_error = align(np.asarray(ai_samples), scan_subset, longest)
    report["ai_fill_fit_mm"] = round(fit_error * 1000, 3)
    if fit_error > longest * MAX_FIT_ERROR:
        report["ai_fill_points"] = 0
        report["warnings"].append(
            f"The AI model didn't line up with the scan (off by {fit_error * 1000:.1f} mm), so it wasn't used. "
            "Unseen areas like the underside are still missing."
        )
        report_path.write_text(json.dumps(report, indent=2))
        return report

    moved = bend_to_scan(moved, scan_subset, longest)
    # Work out how the fit turned the model, so the normals turn the same way. Comparing before and after does it.
    start = np.asarray(ai_samples) - np.asarray(ai_samples).mean(axis=0)
    turned, _, _ = best_fit(start, moved - moved.mean(axis=0))
    moved_normals = np.asarray(ai_normals) @ turned.T

    # What stays: in the scan's gaps, and inside the box the scan measured
    margin = longest * 0.005
    low, high = np.percentile(points, 0.2, axis=0) - margin, np.percentile(points, 99.8, axis=0) + margin
    if bool(frame["has_table"]):
        low[1] = 0.0
    inside = np.all((moved >= low) & (moved <= high), axis=1)
    open_space = cKDTree(points).query(moved)[0] > longest * GAP_DISTANCE
    seen_through = SeenThrough(work, frame, longest)
    # Estimated points are the best stand-in for "plain but solid", since plain faces are where false votes happen.
    # With too few of them the scan has hardly any plain faces, and measured points are the right reference.
    estimated = points[source != 0]
    solid = estimated if len(estimated) >= 500 else points[source == 0]
    seen_through.calibrate(solid[rng.choice(len(solid), min(len(solid), 20_000), replace=False)])
    report["ai_fill_carve_limit"] = round(seen_through.limit, 3)
    keep = inside & open_space & ~seen_through(moved)
    added, added_normals = moved[keep], moved_normals[keep]

    # Point the normals outward from the middle of the piece: the turn above is only an estimate
    middle = (low + high) / 2
    flip = np.sum(added_normals * (added - middle), axis=1) < 0
    added_normals[flip] = -added_normals[flip]

    # Unseen spots borrow the color of the nearest measured point
    added_colors = colors[cKDTree(points).query(added)[1]]
    all_points = np.vstack([points, added])
    all_normals = np.vstack([normals, added_normals])
    all_colors = np.vstack([colors, added_colors])
    all_source = np.r_[source, np.full(len(added), AI_FILL, np.uint8)]

    # A closed surface through everything, which only gets used as the source of the patches
    cloud = np.zeros(len(all_points), dtype=[
        ("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("nx", "<f4"), ("ny", "<f4"), ("nz", "<f4"),
        ("red", "u1"), ("green", "u1"), ("blue", "u1"),
    ])
    for index, axis in enumerate("xyz"):
        cloud[axis] = all_points[:, index]
        cloud["n" + axis] = all_normals[:, index]
    for index, channel in enumerate(("red", "green", "blue")):
        cloud[channel] = all_colors[:, index]
    write_cloud(work / "object_filled.ply", cloud)
    closed = build_mesh(work / "object_filled.ply", work, trim=0)

    # The version before filling stays available, unless the depth step already saved a measured-only one
    if not (work / "model_measured.glb").exists():
        shutil.copy(work / "model.glb", work / "model_measured.glb")
    loaded_base = trimesh.load(work / "model.glb", process=False)
    base = loaded_base.to_geometry() if isinstance(loaded_base, trimesh.Scene) else loaded_base
    final, kept_vertices = stitch(base, closed, points, normals, added, seen_through, low, high, longest, bool(frame["has_table"]))

    exported = export_models(
        final, all_points, all_colors, all_source, work, reach=longest * REACH, patched_from=kept_vertices,
    )
    report["ai_fill_points"] = int(len(added))
    report["faces"] = exported["faces"]
    report["surface_pct"] = exported["shares"]
    report_path.write_text(json.dumps(report, indent=2))
    return report


def faces_where(mesh: trimesh.Trimesh, keep_vertex: np.ndarray) -> trimesh.Trimesh:
    """The part of a mesh whose three corners are all kept."""
    faces = np.asarray(mesh.faces)
    kept = faces[keep_vertex[faces].all(axis=1)]
    used, inverse = np.unique(kept, return_inverse=True)
    return trimesh.Trimesh(np.asarray(mesh.vertices)[used], inverse.reshape(-1, 3), process=False)


def face_outward(mesh: trimesh.Trimesh, points: np.ndarray, normals: np.ndarray) -> trimesh.Trimesh:
    """Flips a mesh if its faces mostly point against the normals of the points near them."""
    nearest = cKDTree(points).query(mesh.triangles_center)[1]
    if np.sum(mesh.face_normals * normals[nearest], axis=1).mean() < 0:
        mesh.invert()
    return mesh


def stitch(base, closed, points, normals, added, seen_through, low, high, longest, has_table) -> tuple[trimesh.Trimesh, int]:
    """Keeps the scan's surface as is wherever data backs it, and takes only the holes from the closed surface.

    Returns the mesh and how many of its vertices are the scan's. Everything after those is a patch.
    """
    reach = longest * REACH
    backed = cKDTree(points).query(base.vertices)[0] <= reach
    kept = face_outward(faces_where(base, backed), points, normals)

    kept_count = len(kept.vertices)

    # Patches: the parts of the closed surface away from what we kept...
    corners = np.asarray(closed.vertices)
    wanted = cKDTree(kept.vertices).query(corners)[0] > reach * 0.5
    # ...with Hunyuan points behind them, or lying on the table, where the piece's flat base is a safe assumption...
    supported = cKDTree(added).query(corners)[0] <= reach if len(added) else np.zeros(len(corners), bool)
    on_table = has_table & (corners[:, 1] <= reach)
    # ...and not somewhere the cameras saw straight past
    wanted &= (supported | on_table) & ~seen_through(corners)
    if not wanted.any():
        return kept, kept_count
    patch = faces_where(closed, wanted)
    # Blobs of only a few faces are leftovers along the seam, not real patches
    pieces = [piece for piece in patch.split(only_watertight=False) if len(piece.faces) >= 30] if len(patch.faces) else []
    if not pieces:
        return kept, kept_count
    patch = trimesh.util.concatenate(pieces)

    # Nothing may stick out past the box the scan measured
    vertices = np.clip(np.asarray(patch.vertices), low, high)

    # Close the seam by pulling the patch's open edge onto the scan's edge. Only the patch moves.
    edges = np.sort(np.asarray(patch.edges), axis=1)
    unique, counts = np.unique(edges, axis=0, return_counts=True)
    open_edge = np.unique(unique[counts == 1])
    kept_edges = np.sort(np.asarray(kept.edges), axis=1)
    kept_unique, kept_counts = np.unique(kept_edges, axis=0, return_counts=True)
    kept_open = np.unique(kept_unique[kept_counts == 1])
    if len(open_edge) and len(kept_open):
        distance, nearest = cKDTree(kept.vertices[kept_open]).query(vertices[open_edge])
        close = distance < reach * 2
        vertices[open_edge[close]] = kept.vertices[kept_open[nearest[close]]]
    patch = trimesh.Trimesh(vertices, patch.faces, process=False)
    # The closed surface's faces all point the same way, so check them against its own center of mass
    if patch.face_normals.size and np.sum(patch.face_normals * (patch.triangles_center - (low + high) / 2), axis=1).mean() < 0:
        patch.invert()
    return trimesh.util.concatenate([kept, patch]), kept_count


def main() -> int:
    work = Path(sys.argv[2])
    try:
        result = pick(work) if sys.argv[1] == "pick" else merge(work, Path(sys.argv[3]))
    except ValueError as exc:
        print(f"ERROR {exc}", flush=True)
        return 2
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
