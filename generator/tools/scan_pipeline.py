"""Photos of a real object -> a measured 3D model, using photogrammetry (COLMAP).

Every point starts out triangulated from photos that see it. Two optional helpers then add to that, and every point
remembers where it came from, so the report can say how much of the model was really measured:
  --ai-python  adds points on plain faces COLMAP can't measure (scan_depth_help.py)
  scan_fill.py fills what no photo saw, like the underside, from Hunyuan3D (runs after this script)
The size of the model only ever comes from measured points.

Steps: find features -> match them -> solve the camera positions -> depth map per photo -> merge into a point
cloud -> drop the table and stray clutter -> build a mesh -> stand it upright, scale to real size -> GLB.

Usage: .venv-scan/bin/python tools/scan_pipeline.py <photo dir> <work dir> [--longest-mm 62.5] [--ai-python .venv/bin/python]
Prints "PROGRESS <pct> <stage>" lines, "ERROR <message for the user>" on failure, and writes to <work dir>:
  model.glb           the model
  model_measured.glb  only what was measured (written when something was added)
  model_sources.glb   the same model colored by where each part came from
  report.json, frame.npz (what scan_fill.py needs)
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import fast_simplification
import numpy as np
import pycolmap
import trimesh
from PIL import Image
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree

import scan_depth_help

# Depth maps above this size don't reliably fit in the 3060's 12 GB
DENSE_MAX_SIDE = 2000
MAX_FACES = 150_000
MIN_PHOTOS = 8

# Where a surface point came from
MEASURED, AI_DEPTH, AI_FILL = 0, 1, 2
# Colors for model_sources.glb: blue measured, orange AI depth, pink AI fill, grey smoothed over by the mesher
SOURCE_COLORS = np.array([[47, 128, 237, 255], [245, 158, 11, 255], [219, 39, 119, 255], [150, 150, 150, 255]], np.uint8)


class ScanFailed(Exception):
    """Something the person taking the photos can fix. The message is shown on their phone."""


def say(pct: int, stage: str) -> None:
    print(f"PROGRESS {pct} {stage}", flush=True)


def group_by_size(photos: Path, images: Path) -> int:
    """COLMAP treats each folder as one camera, so portrait and landscape shots get a folder each."""
    count = 0
    for photo in sorted(photos.iterdir()):
        if photo.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            continue
        with Image.open(photo) as img:
            folder = images / f"{img.width}x{img.height}"
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copy(photo, folder / photo.name)
        count += 1
    return count


def solve_cameras(images: Path, work: Path, total: int) -> tuple[pycolmap.Reconstruction, Path]:
    database, sparse = work / "colmap.db", work / "sparse"
    sparse.mkdir(exist_ok=True)

    say(5, "finding features")
    extraction = pycolmap.FeatureExtractionOptions()
    extraction.max_image_size = 3200
    extraction.sift.max_num_features = 12000
    pycolmap.extract_features(database, images, camera_mode=pycolmap.CameraMode.PER_FOLDER, extraction_options=extraction)

    say(15, "matching photos")
    pycolmap.match_exhaustive(database)

    say(25, "solving camera positions")
    solved = pycolmap.incremental_mapping(database, images, sparse)
    if not solved:
        raise ScanFailed(
            "None of the photos lined up with each other. Put the piece on a sheet of newspaper or printed text, "
            "and overlap each shot with the last one by about two thirds."
        )
    best_id = max(solved, key=lambda key: solved[key].num_reg_images())
    best = solved[best_id]
    aligned = best.num_reg_images()
    if aligned < MIN_PHOTOS or aligned < total * 0.5:
        raise ScanFailed(
            f"Only {aligned} of {total} photos lined up. Move in smaller steps around the piece and keep the "
            "printed sheet in frame."
        )
    return best, sparse / str(best_id)


def build_point_cloud(sparse: Path, images: Path, work: Path) -> Path:
    dense = work / "dense"
    say(42, "preparing photos")
    undistort = pycolmap.UndistortCameraOptions()
    undistort.max_image_size = DENSE_MAX_SIDE
    pycolmap.undistort_images(dense, sparse, images, undistort_options=undistort)

    say(45, "computing depth (slowest step)")
    stereo = pycolmap.PatchMatchOptions()
    stereo.max_image_size = DENSE_MAX_SIDE
    stereo.geom_consistency = True
    stereo.gpu_index = "0"
    stereo.window_step = 2  # compares every other pixel: several times faster, checked against scan_test_compare.py
    pycolmap.patch_match_stereo(dense, options=stereo)

    say(82, "merging depth maps")
    fusion = pycolmap.StereoFusionOptions()
    fusion.min_num_pixels = 4
    cloud = work / "fused.ply"
    pycolmap.stereo_fusion(cloud, dense, options=fusion, output_type="PLY")
    if not cloud.exists() or cloud.stat().st_size < 2000:
        raise ScanFailed("No surface could be measured. The piece is probably too shiny or too plain: see the tips.")
    return cloud


_PLY_TYPES = {"float": "f4", "float32": "f4", "double": "f8", "uchar": "u1", "uint8": "u1"}


def read_cloud(path: Path) -> np.ndarray:
    with path.open("rb") as fh:
        count, fields = 0, []
        while (line := fh.readline().decode().strip()) != "end_header":
            parts = line.split()
            if parts[:2] == ["element", "vertex"]:
                count = int(parts[2])
            elif parts[0] == "property":
                fields.append((parts[2], "<" + _PLY_TYPES[parts[1]]))
        return np.fromfile(fh, dtype=np.dtype(fields), count=count)


def write_cloud(path: Path, cloud: np.ndarray) -> None:
    names = {"f4": "float", "f8": "double", "u1": "uchar"}
    header = ["ply", "format binary_little_endian 1.0", f"element vertex {len(cloud)}"]
    header += [f"property {names[cloud.dtype[n].str[1:]]} {n}" for n in cloud.dtype.names]
    with path.open("wb") as fh:
        fh.write(("\n".join(header + ["end_header"]) + "\n").encode())
        cloud.tofile(fh)


def find_table(points: np.ndarray, cameras: np.ndarray, size: float) -> tuple[np.ndarray, float] | None:
    """Finds the big flat surface the cameras all sit above. Returns (unit normal pointing up, offset)."""
    rng = np.random.default_rng(0)
    sample = points[rng.choice(len(points), min(len(points), 50_000), replace=False)]
    tolerance = size * 0.004
    best, best_count = None, len(sample) * 0.15  # a real table is a good chunk of what the photos see
    for _ in range(500):
        a, b, c = sample[rng.choice(len(sample), 3, replace=False)]
        normal = np.cross(b - a, c - a)
        length = np.linalg.norm(normal)
        if length < 1e-12:
            continue
        normal /= length
        offset = normal @ a
        heights = cameras @ normal - offset
        if np.mean(heights < 0) > 0.5:
            normal, offset, heights = -normal, -offset, -heights
        if np.mean(heights > 0) < 0.9:
            continue
        inliers = np.count_nonzero(np.abs(sample @ normal - offset) < tolerance)
        if inliers > best_count:
            best, best_count = (normal, offset), inliers
    if best is None:
        return None
    # Tidy the plane up using everything that landed on it
    normal, offset = best
    near = sample[np.abs(sample @ normal - offset) < tolerance]
    center = near.mean(axis=0)
    refined = np.linalg.svd(near - center, full_matrices=False)[2][2]
    if refined @ normal < 0:
        refined = -refined
    return refined, float(refined @ center)


def aim_point(best: pycolmap.Reconstruction) -> tuple[np.ndarray, np.ndarray]:
    """Where the cameras were all pointing (should be the object). Also returns the camera positions."""
    centers, directions = [], []
    for image in best.images.values():
        if image.has_pose:
            centers.append(image.projection_center())
            directions.append(image.viewing_direction())
    centers, directions = np.array(centers), np.array(directions)
    # Least-squares closest point to all the viewing rays
    system = np.zeros((3, 3))
    target = np.zeros(3)
    for center, direction in zip(centers, directions):
        project = np.eye(3) - np.outer(direction, direction)
        system += project
        target += project @ center
    return np.linalg.solve(system, target), centers


def isolate_object(cloud: np.ndarray, best: pycolmap.Reconstruction, warnings: list[str]):
    """Throws away the table and anything not connected to the object. Returns (kept cloud, up, table offset)."""
    points = np.stack([cloud["x"], cloud["y"], cloud["z"]], axis=1).astype(np.float64)
    target, cameras = aim_point(best)
    size = float(np.linalg.norm(np.percentile(points, 95, axis=0) - np.percentile(points, 5, axis=0)))

    table = find_table(points, cameras, size)
    keep = np.ones(len(points), bool)
    up, offset = None, 0.0
    if table is None:
        warnings.append("Couldn't find the table, so the base of the model may not be flat.")
    else:
        up, offset = table
        heights = points @ up - offset
        near = np.abs(heights) < size * 0.004
        noise = float(np.median(np.abs(heights[near]))) * 1.4826
        keep = heights > max(3 * noise, size * 0.0015)

    candidates = np.flatnonzero(keep)
    if len(candidates) < 500:
        raise ScanFailed("Almost nothing was measured on the piece itself. It may be too shiny or plain: see the tips.")

    # Connect points that touch; the object is the blob the cameras were aimed at
    rng = np.random.default_rng(0)
    subset = candidates if len(candidates) <= 200_000 else rng.choice(candidates, 200_000, replace=False)
    tree = cKDTree(points[subset])
    spacing = float(np.median(tree.query(points[subset], k=2)[0][:, 1]))
    pairs = tree.query_pairs(spacing * 5, output_type="ndarray")
    graph = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(len(subset), len(subset)))
    _, labels = connected_components(graph, directed=False)
    counts = np.bincount(labels)
    # Blobs far from the aim point count for less, so a big bit of clutter can't beat the object
    centers = np.stack([np.bincount(labels, points[subset, i]) / counts for i in range(3)], axis=1)
    scores = counts * np.exp(-np.linalg.norm(centers - target, axis=1) / (size * 0.25))
    chosen = np.argmax(scores)
    nearest = tree.query(points[candidates])[1]
    kept = candidates[labels[nearest] == chosen]

    # Drop lone noisy points hovering off the surface
    kept_points = points[kept]
    tree = cKDTree(kept_points)
    mean_gap = tree.query(kept_points, k=9)[0][:, 1:].mean(axis=1)
    kept = kept[mean_gap < mean_gap.mean() + 2.5 * mean_gap.std()]
    return cloud[kept], up, offset


def upright_frame(points: np.ndarray, up: np.ndarray | None, offset: float):
    """Rotation and shift that lay the object on y=0 with its long side along x (what glTF and Unity expect)."""
    center = points.mean(axis=0)
    if up is None:
        # No table found: fall back to the object's own main axes
        axes = np.linalg.svd(points - center, full_matrices=False)[2]
        up = axes[2]
        offset = float(np.percentile(points @ up, 0.2))
    seed = np.array([1.0, 0, 0]) if abs(up[0]) < 0.9 else np.array([0, 1.0, 0])
    u = np.cross(up, seed)
    u /= np.linalg.norm(u)
    v = np.cross(up, u)
    flat = np.stack([points @ u, points @ v], axis=1)
    if len(flat) > 50_000:
        flat = flat[:: len(flat) // 50_000]
    # Smallest footprint rectangle: how you'd naturally measure it with a ruler
    low, high = np.percentile(flat, [0.2, 99.8], axis=0)
    flat = flat[np.all((flat >= low) & (flat <= high), axis=1)]
    best_angle, best_area = 0.0, np.inf
    for angle in np.radians(np.arange(0, 90, 0.5)):
        c, s = np.cos(angle), np.sin(angle)
        spread = np.ptp(np.stack([flat @ (c, s), flat @ (-s, c)], axis=1), axis=0)
        if spread.prod() < best_area:
            best_angle, best_area = angle, spread.prod()
    c, s = np.cos(best_angle), np.sin(best_angle)
    x_axis, z_axis = c * u + s * v, -s * u + c * v
    if np.ptp(points @ z_axis) > np.ptp(points @ x_axis):
        x_axis, z_axis = z_axis, -x_axis
    rotation = np.stack([x_axis, up, np.cross(x_axis, up)])
    moved = points @ rotation.T
    shift = np.array([*[-(np.percentile(moved[:, i], 0.2) + np.percentile(moved[:, i], 99.8)) / 2 for i in (0,)], -offset, 0.0])
    shift[2] = -(np.percentile(moved[:, 2], 0.2) + np.percentile(moved[:, 2], 99.8)) / 2
    return rotation, shift


def add_ai_depth(best, cloud_file: Path, work: Path, ai_python: str, warnings: list[str]) -> tuple[Path, dict | None]:
    """Adds estimated points on faces COLMAP couldn't measure. If anything goes wrong the scan just carries on without."""
    say(83, "estimating plain faces")
    predictions = work / "ai_depth"
    script = Path(__file__).resolve().parent / "scan_depth_model.py"
    done = subprocess.run(
        [ai_python, str(script), str(work / "dense" / "images"), str(predictions)],
        capture_output=True, text=True, errors="replace",
    )
    if done.returncode != 0:
        print(done.stdout[-2000:], done.stderr[-2000:], flush=True)
        warnings.append("The plain-face estimate didn't run, so this model only has what COLMAP measured.")
        return cloud_file, None

    cloud = read_cloud(cloud_file)
    object_cloud, up, offset = isolate_object(cloud, best, [])
    object_points = np.stack([object_cloud["x"], object_cloud["y"], object_cloud["z"]], axis=1).astype(np.float64)
    center = object_points.mean(axis=0)
    size = float(np.linalg.norm(np.ptp(object_points, axis=0)))

    def keep_region(points: np.ndarray) -> np.ndarray:
        """Near the object and above the table. Estimates of the table itself would only get in the way."""
        near = np.linalg.norm(points - center, axis=1) < size * 0.75
        return near if up is None else near & (points @ up - offset > size * 0.004)

    reconstruction = pycolmap.Reconstruction(work / "dense" / "sparse")
    extra, stats = scan_depth_help.estimate(reconstruction, work / "dense", predictions, object_cloud, object_points, keep_region)
    if not len(extra):
        warnings.append("The plain-face estimate didn't pass its checks, so this model only has what COLMAP measured.")
        return cloud_file, stats
    combined = work / "fused_plus.ply"
    write_cloud(combined, np.concatenate([cloud, extra]))
    np.save(work / "fused_plus_source.npy", np.r_[np.full(len(cloud), MEASURED, np.uint8), np.full(len(extra), AI_DEPTH, np.uint8)])
    return combined, stats


def build_mesh(cloud_file: Path, work: Path, trim: int = 6) -> trimesh.Trimesh:
    meshed = work / "mesh.ply"
    options = pycolmap.PoissonMeshingOptions()
    options.depth = 10
    options.trim = trim
    pycolmap.poisson_meshing(cloud_file, meshed, options)
    mesh = trimesh.load(meshed, process=False)
    pieces = mesh.split(only_watertight=False)
    mesh = max(pieces, key=lambda piece: len(piece.faces))  # stray islands aren't part of the object
    if len(mesh.faces) > MAX_FACES:
        vertices, faces = fast_simplification.simplify(
            np.asarray(mesh.vertices, dtype=np.float64), np.asarray(mesh.faces), target_count=MAX_FACES
        )
        mesh = trimesh.Trimesh(vertices, faces, process=False)
    return mesh


def source_of_vertices(vertices: np.ndarray, points: np.ndarray, source: np.ndarray, reach: float) -> np.ndarray:
    """For each mesh vertex: the first source with a point within reach, measured first. 3 means none was close."""
    result = np.full(len(vertices), 3, np.uint8)
    for kind in (AI_FILL, AI_DEPTH, MEASURED):  # last one wins
        chosen = points[source == kind]
        if len(chosen):
            result[cKDTree(chosen).query(vertices)[0] <= reach] = kind
    return result


def surface_shares(mesh: trimesh.Trimesh, per_vertex: np.ndarray) -> dict:
    """Percent of the surface area from each source. A face counts for its first corner, which is close enough."""
    area = mesh.area_faces
    kinds = per_vertex[mesh.faces[:, 0]]
    share = lambda kind: round(float(area[kinds == kind].sum() / area.sum() * 100), 1)
    return {"measured": share(MEASURED), "ai_depth": share(AI_DEPTH), "ai_fill": share(AI_FILL), "smoothed": share(3)}


def export_models(mesh: trimesh.Trimesh, points, colors, source, work: Path, reach: float, patched_from: int | None = None) -> dict:
    """Writes the model and the color-by-source copy. Each surface point takes its color from the nearest point.

    patched_from: vertices from this index on are gap-fill patches, whatever happens to be near them.
    """
    nearest = cKDTree(points).query(mesh.vertices)[1]
    model = trimesh.Trimesh(mesh.vertices, mesh.faces, vertex_colors=colors[nearest], process=False)
    # glTF needs consistent facing or parts render inside out
    if model.is_winding_consistent and model.volume < 0:
        model.invert()
    model.export(work / "model.glb")
    per_vertex = source_of_vertices(model.vertices, points, source, reach)
    if patched_from is not None:
        per_vertex[patched_from:] = AI_FILL
    trimesh.Trimesh(model.vertices, model.faces, vertex_colors=SOURCE_COLORS[per_vertex], process=False).export(work / "model_sources.glb")
    return {"faces": len(model.faces), "shares": surface_shares(model, per_vertex)}


def run(photos: Path, work: Path, longest_mm: float | None, ai_python: str | None = None) -> dict:
    started = time.monotonic()
    images = work / "images"
    total = group_by_size(photos, images)
    if total < MIN_PHOTOS:
        raise ScanFailed(f"A scan needs at least {MIN_PHOTOS} photos ({total} uploaded); 30 to 50 works best.")

    best, sparse = solve_cameras(images, work, total)
    cloud_file = build_point_cloud(sparse, images, work)
    warnings: list[str] = []
    depth_stats = None
    if ai_python:
        cloud_file, depth_stats = add_ai_depth(best, cloud_file, work, ai_python, warnings)
    report = finish(best, cloud_file, work, longest_mm, total, depth_stats, warnings)
    report["seconds"] = round(time.monotonic() - started, 1)
    return report


def finish(best: pycolmap.Reconstruction, cloud_file: Path, work: Path, longest_mm: float | None, total: int,
           depth_stats: dict | None = None, warnings: list[str] | None = None) -> dict:
    """Everything after the point cloud. Separate so it can be rerun without redoing the slow depth step."""
    say(85, "isolating the object")
    warnings = [] if warnings is None else warnings
    raw = read_cloud(cloud_file)
    measured_points = int(np.sum(np.load(work / "fused_plus_source.npy") == MEASURED)) if cloud_file.name == "fused_plus.ply" else len(raw)
    source = np.load(work / "fused_plus_source.npy") if cloud_file.name == "fused_plus.ply" else np.zeros(len(raw), np.uint8)
    # Carry the source along as an extra field while the object gets picked out of the scene
    tagged = np.empty(len(raw), dtype=raw.dtype.descr + [("source", "u1")])
    for field in raw.dtype.names:
        tagged[field] = raw[field]
    tagged["source"] = source
    tagged, up, offset = isolate_object(tagged, best, warnings)
    cloud = np.empty(len(tagged), dtype=raw.dtype)
    for field in raw.dtype.names:
        cloud[field] = tagged[field]
    source = tagged["source"]
    filtered = work / "object.ply"
    write_cloud(filtered, cloud)
    say(88, "building surface")
    mesh = build_mesh(filtered, work)

    say(95, "measuring")
    points = np.stack([cloud["x"], cloud["y"], cloud["z"]], axis=1).astype(np.float64)
    normals = np.stack([cloud["nx"], cloud["ny"], cloud["nz"]], axis=1).astype(np.float64)
    is_measured = source == MEASURED
    # The frame and the size come from measured points only, so an estimate can never change them
    rotation, shift = upright_frame(points[is_measured], up, offset)
    points = points @ rotation.T + shift
    normals = normals @ rotation.T
    vertices = np.asarray(mesh.vertices) @ rotation.T + shift
    vertices[:, 1] = np.maximum(vertices[:, 1], 0)  # the surface can't go through the table

    # Trim the extreme 0.2% so one stray point can't change the reported size
    low = np.percentile(points[is_measured], 0.2, axis=0)
    high = np.percentile(points[is_measured], 99.8, axis=0)
    sizes = high - low
    sizes[1] = high[1] if up is not None else sizes[1]  # with a table, height is measured from the table
    longest = float(sizes.max())

    if longest_mm:
        scale = longest_mm / 1000 / longest  # meters, like the rest of Unity
    else:
        scale = 1 / longest
        warnings.append("No real size given: the proportions are right but the size isn't in millimeters.")
    # The mesher can balloon past the data over unmeasured faces. The surface can't be bigger than what was measured.
    margin = longest * 0.005
    box_low, box_high = low - margin, high + margin
    box_low[1] = 0 if up is not None else box_low[1]
    vertices = np.clip(vertices, box_low, box_high) * scale
    points = points * scale

    colors = np.stack([cloud["red"], cloud["green"], cloud["blue"]], axis=1)
    reach = float(sizes.max() * scale * 0.012)
    final = trimesh.Trimesh(vertices, np.asarray(mesh.faces), process=False)
    exported = export_models(final, points, colors, source, work, reach)

    # The measured-only version, so anyone can see exactly what the helpers added
    if not is_measured.all():
        write_cloud(work / "object_measured.ply", cloud[is_measured])
        plain = build_mesh(work / "object_measured.ply", work)
        plain_vertices = np.clip(np.asarray(plain.vertices) @ rotation.T + shift, box_low, box_high)
        plain_model = trimesh.Trimesh(plain_vertices * scale, plain.faces, process=False)
        nearest = cKDTree(points[is_measured]).query(plain_model.vertices)[1]
        plain_model.visual.vertex_colors = colors[is_measured][nearest]
        plain_model.export(work / "model_measured.glb")

    # Everything scan_fill.py needs to line a Hunyuan3D model up with this one, without repeating any of the above
    views = []
    for image in best.images.values():
        if not image.has_pose:
            continue
        camera_position = (np.asarray(image.projection_center()) @ rotation.T + shift) * scale
        forward = np.asarray(image.viewing_direction()) @ rotation.T
        world = (points[np.random.default_rng(0).choice(len(points), min(len(points), 5000), replace=False)] / scale - shift) @ rotation
        cam = world @ image.cam_from_world().matrix()[:3, :3].T + image.cam_from_world().matrix()[:3, 3]
        pixels = best.cameras[image.camera_id].img_from_cam(cam[cam[:, 2] > 0])
        views.append({
            "name": image.name, "position": camera_position.tolist(), "forward": forward.tolist(),
            "box": [*np.percentile(pixels, 1, axis=0).tolist(), *np.percentile(pixels, 99, axis=0).tolist()],
        })
    np.savez(
        work / "frame.npz", points=points, normals=normals, colors=colors, source=source,
        has_table=up is not None, longest=sizes.max() * scale, height=sizes[1] * scale,
        rotation=rotation, shift=shift, scale=scale,
    )
    (work / "views.json").write_text(json.dumps(views))

    aligned = best.num_reg_images()
    if aligned < total * 0.8:
        warnings.append(f"{total - aligned} of {total} photos couldn't be used, so some sides may be less detailed.")
    if int(is_measured.sum()) < 20_000:
        warnings.append("Few points landed on the piece: expect a rough surface. Dust shiny plastic with powder and redo.")

    report = {
        "photos_uploaded": total,
        "photos_used": aligned,
        "reprojection_error_px": round(best.compute_mean_reprojection_error(), 3),
        "points_measured": measured_points,
        "points_on_object": int(is_measured.sum()),
        "points_ai_depth": int((source == AI_DEPTH).sum()),
        "faces": exported["faces"],
        "surface_pct": exported["shares"],
        "size_mm": [round(float(s) * scale * 1000, 2) for s in sizes] if longest_mm else None,
        "proportions": [round(float(s) / longest, 4) for s in sizes],
        "scaled_from_mm": longest_mm,
        "warnings": warnings,
    }
    if depth_stats and depth_stats.get("check_median") is not None:
        # How far off the depth fit was on measured points it wasn't allowed to see. Only meaningful with a real size.
        report["ai_depth_check_mm"] = {
            "median": round(depth_stats["check_median"] * scale * 1000, 3),
            "p90": round(depth_stats["check_p90"] * scale * 1000, 3),
        } if longest_mm else None
        report["ai_depth_photos"] = depth_stats["photos_fitted"]
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("photos", type=Path)
    parser.add_argument("work", type=Path)
    parser.add_argument("--longest-mm", type=float)
    parser.add_argument("--ai-python", help="python of the environment with torch: turns on the plain-face estimate")
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    try:
        report = run(args.photos, args.work, args.longest_mm, args.ai_python)
    except ScanFailed as exc:
        print(f"ERROR {exc}", flush=True)
        return 2
    (args.work / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
