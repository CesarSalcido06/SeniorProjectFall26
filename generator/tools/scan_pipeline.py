"""Photos of a real object -> a measured 3D model, using photogrammetry (COLMAP).

How the work is split: the AI finds the object in each photo (scan_outlines.py), and photogrammetry works out where
the cameras were. Together those give a shape with real proportions (scan_hull.py). Every point on the model remembers
where it came from, so the report can say how much of it was really measured:
  measured     triangulated from photos, only possible where there's texture
  outline      from the hull: right on the outside, but it can't show dents or hollows
  AI depth     estimated on plain faces (--ai-depth, scan_depth_help.py)
  AI fill      from Hunyuan3D, for what's still left (scan_fill.py, runs after this script)

If the outlines can't agree on one shape, which is what happens when the piece gets turned instead of the phone, the
scan can't be measured. Rather than hand back a piece of the table, it stops early with "mode": "estimate" in the
report, and scan_fill.py makes the model from Hunyuan3D alone, sized from the typed measurement and labeled as a guess.

Steps: outlines -> find features -> match them -> solve the camera positions -> check the outlines agree -> depth map
per photo -> merge into a point cloud -> keep what's inside the outlines -> add the hull where nothing was measured ->
build a mesh -> stand it upright -> scale to real size, measured off the finished mesh -> GLB.

Usage: .venv-scan/bin/python tools/scan_pipeline.py <photo dir> <work dir> --ai-python .venv/bin/python
           [--longest-mm 62.5] [--check-mm 35] [--ai-depth]
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
from scipy.ndimage import binary_erosion
from scipy.spatial import cKDTree

import scan_depth_help
import scan_hull

# Depth maps above this size don't reliably fit in the 3060's 12 GB
DENSE_MAX_SIDE = 2000
MAX_FACES = 150_000
MIN_PHOTOS = 8

# Where a surface point came from
MEASURED, AI_DEPTH, AI_FILL, OUTLINE = 0, 1, 2, 3
SMOOTHED = 4  # no point nearby: the mesher bridged the gap
# Colors for model_sources.glb: blue measured, orange AI depth, pink AI fill, green outline, grey smoothed
SOURCE_COLORS = np.array([
    [47, 128, 237, 255], [245, 158, 11, 255], [219, 39, 119, 255], [16, 185, 129, 255], [150, 150, 150, 255],
], np.uint8)
# If this much of the hull already has measured points on it, measured points decide the size. The hull runs a little
# big, because no outline can see into a corner from every side.
TRUST_MEASURED_SHARE = 0.6
# A side counts as a real flat face if measured points within this much of it (fraction of the longest side) land in
# this share of a grid laid over it
FACE_THICKNESS = 0.015
FACE_CELLS = 20
FACE_COVERAGE = 0.35


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


def find_outlines(images: Path, work: Path, ai_python: str) -> Path:
    say(3, "finding the piece in each photo")
    outlines = work / "outlines"
    script = Path(__file__).resolve().parent / "scan_outlines.py"
    done = subprocess.run([ai_python, str(script), str(images), str(outlines)], capture_output=True, text=True, errors="replace")
    if done.returncode != 0:
        print(done.stdout[-2000:], done.stderr[-2000:], flush=True)
        raise ScanFailed("Couldn't find the piece in the photos because the outline step crashed. See the scan log.")
    return outlines


def shrunk_outlines(outlines: Path, target: Path) -> Path:
    """Copies of the outlines pulled in a bit, so features on the edge, which are half background, get left out."""
    for mask in outlines.glob("*/*.png"):
        folder = target / mask.parent.name
        folder.mkdir(parents=True, exist_ok=True)
        pulled_in = binary_erosion(np.asarray(Image.open(mask)) > 128, iterations=6)
        Image.fromarray((pulled_in * 255).astype(np.uint8)).save(folder / mask.name)
    return target


def solve_cameras(images: Path, work: Path, name: str = "", masks: Path | None = None) -> tuple[pycolmap.Reconstruction, Path] | None:
    """Works out where each photo was taken from. Given masks, only features on the object count."""
    database, sparse = work / f"colmap{name}.db", work / f"sparse{name}"
    sparse.mkdir(exist_ok=True)

    extraction = pycolmap.FeatureExtractionOptions()
    extraction.max_image_size = 3200
    extraction.sift.max_num_features = 20000
    # Lower than the default, so low-contrast surfaces like a plain table or plastic still give features
    extraction.sift.peak_threshold = 0.002
    reader = pycolmap.ImageReaderOptions()
    if masks is not None:
        reader.mask_path = str(masks)
    pycolmap.extract_features(database, images, camera_mode=pycolmap.CameraMode.PER_FOLDER, reader_options=reader, extraction_options=extraction)
    pycolmap.match_exhaustive(database)

    # Real photos of plain things match weakly, and COLMAP's defaults left half of them out. The outline check after
    # this is what catches a bad solve, so it's fine to let more photos in here.
    mapping = pycolmap.IncrementalPipelineOptions()
    mapping.min_num_matches = 8
    mapping.mapper.init_min_num_inliers = 30
    mapping.mapper.abs_pose_min_num_inliers = 12
    mapping.mapper.abs_pose_min_inlier_ratio = 0.12
    solved = pycolmap.incremental_mapping(database, images, sparse, options=mapping)
    if not solved:
        return None
    best_id = max(solved, key=lambda key: solved[key].num_reg_images())
    return solved[best_id], sparse / str(best_id)


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


def tagged_xyz(cloud: np.ndarray) -> np.ndarray:
    return np.stack([cloud["x"], cloud["y"], cloud["z"]], axis=1).astype(np.float64)


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


def isolate_object(cloud: np.ndarray, views: list, warnings: list[str], object_size: float):
    """Keeps what's inside the object's outline in the photos that can see it. Returns (kept cloud, up, table offset).

    The table is found from the points outside the outlines, since those can't be the object. Anything in the photos
    that isn't the object goes the same way, without needing to guess which part of the point cloud is which.
    """
    points = np.stack([cloud["x"], cloud["y"], cloud["z"]], axis=1).astype(np.float64)
    inside, seen = scan_hull.object_votes(views, points)
    on_object = (seen >= 2) & (inside >= seen * 0.8)
    # Most photos, not all: one bad outline shouldn't be able to take a point off the table
    outside = inside <= seen * 0.25

    # Only look near the object. Walls and furniture also make flat surfaces, and there can be a lot more of them.
    cameras = np.array([view.center for view in views])
    if on_object.any():
        middle = np.median(points[on_object], axis=0)
        outside &= np.linalg.norm(points - middle, axis=1) < object_size * 1.5
    table = find_table(points[outside], cameras, object_size) if outside.sum() >= 1000 else None
    up, offset = None, 0.0
    if table is None:
        warnings.append("Couldn't find the table, so the base of the model comes from the lowest point seen.")
    else:
        up, offset = table
        heights = points @ up - offset
        near = np.abs(heights) < object_size * 0.004
        noise = float(np.median(np.abs(heights[near]))) * 1.4826 if near.any() else 0.0
        on_object &= heights > max(3 * noise, object_size * 0.0015)

    kept = np.flatnonzero(on_object)
    if len(kept) >= 50:
        # Drop lone noisy points hovering off the surface
        tree = cKDTree(points[kept])
        mean_gap = tree.query(points[kept], k=9)[0][:, 1:].mean(axis=1)
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


def add_ai_depth(cloud_file: Path, work: Path, ai_python: str, views: list, object_size: float, warnings: list[str]) -> tuple[Path, dict | None]:
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
    object_cloud, up, offset = isolate_object(cloud, views, [], object_size)
    if len(object_cloud) < 500:
        warnings.append("Too little was measured on the piece to fit the plain-face estimate to.")
        return cloud_file, None
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
    if not isinstance(mesh, trimesh.Trimesh) or not len(mesh.faces):
        raise ScanFailed("No surface could be built from these photos. Take more of them, with smaller steps between shots.")
    pieces = mesh.split(only_watertight=False)
    mesh = max(pieces, key=lambda piece: len(piece.faces))  # stray islands aren't part of the object
    if len(mesh.faces) > MAX_FACES:
        vertices, faces = fast_simplification.simplify(
            np.asarray(mesh.vertices, dtype=np.float64), np.asarray(mesh.faces), target_count=MAX_FACES
        )
        mesh = trimesh.Trimesh(vertices, faces, process=False)
    return mesh


def source_of_vertices(vertices: np.ndarray, points: np.ndarray, source: np.ndarray, reach: float) -> np.ndarray:
    """For each mesh vertex: the first source with a point within reach, measured first."""
    result = np.full(len(vertices), SMOOTHED, np.uint8)
    for kind in (AI_FILL, OUTLINE, AI_DEPTH, MEASURED):  # last one wins
        chosen = points[source == kind]
        if len(chosen):
            result[cKDTree(chosen).query(vertices)[0] <= reach] = kind
    return result


def surface_shares(mesh: trimesh.Trimesh, per_vertex: np.ndarray) -> dict:
    """Percent of the surface area from each source. A face counts for its first corner, which is close enough."""
    area = mesh.area_faces
    kinds = per_vertex[mesh.faces[:, 0]]
    share = lambda kind: round(float(area[kinds == kind].sum() / area.sum() * 100), 1)
    return {"measured": share(MEASURED), "outline": share(OUTLINE), "ai_depth": share(AI_DEPTH), "ai_fill": share(AI_FILL), "smoothed": share(SMOOTHED)}


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


def measurable_solve(images: Path, work: Path, outlines: Path) -> dict:
    """Finds a camera solve the outlines agree with. Returns the best try, whether or not it can be measured."""
    say(8, "solving camera positions")
    # Using the whole photo is the most stable when the phone went around the piece
    first = _check_solve(solve_cameras(images, work), images, outlines, False)
    if first["usable"]:
        return first
    # If the piece got turned, the background is what fooled the first try, so try again with only the piece
    say(25, "solving again from the piece alone")
    second = _check_solve(
        solve_cameras(images, work, "_object", shrunk_outlines(outlines, work / "outlines_shrunk")), images, outlines, True,
    )
    return second if second["usable"] or first["solve"] is None else first


def _check_solve(solved, images: Path, outlines: Path, object_only: bool) -> dict:
    if solved is None or solved[0].num_reg_images() < scan_hull.MIN_AGREEING_PHOTOS:
        return {"usable": False, "reason": "no_solve", "views": [], "dropped": [], "agreement": 0.0, "gap": 360.0, "solve": solved, "object_only": object_only}
    best, _ = solved
    say(30, "checking the photos agree")
    check = scan_hull.agreeing_views(scan_hull.load_views(best, outlines, images))
    print(
        f"outline check: object_only={object_only} photos={best.num_reg_images()} kept={len(check['views'])} "
        f"agreement={check['agreement']:.3f} gap={check['gap']:.0f} reason={check['reason']}", flush=True,
    )
    return {**check, "solve": solved, "object_only": object_only}


ESTIMATE_REASONS = {
    "no_circle": "These photos don't go all the way around the piece, usually because the piece was turned while the "
                 "phone stayed still. The scan can only measure what the phone moved around.",
    "disagree": "The piece's outline doesn't match from photo to photo, usually because it was moved or flipped "
                "between shots. The scan needs it to stay put the whole time.",
    "no_solve": "Too few of the photos could be lined up with each other. That happens when the piece gets moved, or "
                "is clear or very shiny. Keep it still on newspaper or printed text, and overlap each shot by about two thirds.",
}


def pick_estimate_photo(work: Path, outlines: Path, images: Path) -> str:
    """The clearest side-on photo for the AI model: the whole piece in frame, big, and with its longest outline."""
    best_name, best_score = None, -1.0
    for mask in sorted(outlines.glob("*/*.png")):
        outline = np.asarray(Image.open(mask)) > 128
        rows, columns = np.nonzero(outline)
        if not len(rows):
            continue
        height, width = outline.shape
        margin = int(min(outline.shape) * 0.01)
        if rows.min() <= margin or columns.min() <= margin or rows.max() >= height - margin or columns.max() >= width - margin:
            continue  # cut off by the frame
        long_side, short_side = sorted((np.ptp(rows) + 1, np.ptp(columns) + 1), reverse=True)
        score = outline.mean() * (long_side / short_side)
        if score > best_score:
            best_name, best_score = str(mask.relative_to(outlines))[: -len(".png")], score
    if best_name is None:
        raise ScanFailed("The whole piece isn't in frame in any of the photos. Step back so it fits, with some space around it.")
    return str(images / best_name)


def run(photos: Path, work: Path, longest_mm: float | None, ai_python: str, ai_depth: bool = False, check_mm: float | None = None) -> dict:
    started = time.monotonic()
    images = work / "images"
    total = group_by_size(photos, images)
    if total < MIN_PHOTOS:
        raise ScanFailed(f"A scan needs at least {MIN_PHOTOS} photos ({total} uploaded); 30 to 50 works best.")

    outlines = find_outlines(images, work, ai_python)
    check = measurable_solve(images, work, outlines)
    if not check["usable"]:
        solved = check.get("solve")
        report = {
            "mode": "estimate",
            "photos_uploaded": total,
            "photos_used": 1,  # the AI model only gets the one photo
            "photos_lined_up": solved[0].num_reg_images() if solved else 0,
            "estimate_reason": ESTIMATE_REASONS[check["reason"]],
            "estimate_photo": pick_estimate_photo(work, outlines, images),
            "outline_agreement": round(check["agreement"], 3),
            "scaled_from_mm": longest_mm,
            "checked_with_mm": check_mm,
            "warnings": [],
            "seconds": round(time.monotonic() - started, 1),
        }
        return report

    best, sparse = check["solve"]
    views = check["views"]
    rough = scan_hull.carve(views, passes=scan_hull.FAST_PASSES)
    object_size = float(np.linalg.norm(np.ptp(rough.points(), axis=0)))
    cloud_file = build_point_cloud(sparse, images, work)
    warnings: list[str] = []
    if check["dropped"]:
        warnings.append(f"{len(check['dropped'])} photo(s) didn't agree with the rest and weren't used for the shape.")
    depth_stats = None
    if ai_depth:
        cloud_file, depth_stats = add_ai_depth(cloud_file, work, ai_python, views, object_size, warnings)
    report = finish(best, cloud_file, work, longest_mm, total, views, object_size, depth_stats, warnings, check)
    report["seconds"] = round(time.monotonic() - started, 1)
    measure_final(work, longest_mm, check_mm, report)
    return report


def finish(best: pycolmap.Reconstruction, cloud_file: Path, work: Path, longest_mm: float | None, total: int, views: list,
           object_size: float, depth_stats: dict | None = None, warnings: list[str] | None = None, check: dict | None = None) -> dict:
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
    tagged, up, offset = isolate_object(tagged, views, warnings, object_size)

    # The shape from the outlines, cut off at the table. It covers whatever COLMAP couldn't measure.
    say(87, "shaping from the outlines")
    hull = scan_hull.carve(views, up, offset)
    if hull is None:
        raise ScanFailed("The piece's outline doesn't match from photo to photo. Keep it still and move the phone instead.")
    hull_points, hull_normals, hull_colors = scan_hull.surface(hull, views)
    flaps = trim_flaps(hull_points, views, work, object_size)
    hull_points, hull_normals, hull_colors = hull_points[~flaps], hull_normals[~flaps], hull_colors[~flaps]
    has_measured_nearby = np.zeros(len(hull_points), bool)
    if len(tagged):
        has_measured_nearby = cKDTree(tagged_xyz(tagged)).query(hull_points)[0] <= object_size * 0.012
    measured_share = float(has_measured_nearby.mean())
    extra = np.zeros(int((~has_measured_nearby).sum()), dtype=tagged.dtype)
    gaps = ~has_measured_nearby
    for index, axis in enumerate("xyz"):
        extra[axis] = hull_points[gaps, index]
        extra["n" + axis] = hull_normals[gaps, index]
    for index, channel in enumerate(("red", "green", "blue")):
        extra[channel] = hull_colors[gaps, index]
    extra["source"] = OUTLINE
    tagged = np.concatenate([tagged, extra])

    # Where measured points cover most of the piece they set the size, so the hull's small overshoot gets trimmed off
    xyz = tagged_xyz(tagged)
    kind = tagged["source"]
    trusted = kind != AI_DEPTH
    frame_points = xyz[trusted]
    if up is None:
        up = scan_hull.camera_up(views)
        offset = float(np.percentile(frame_points[kind[trusted] == MEASURED] @ up, 0.2)) if (kind == MEASURED).sum() > 2000 else float(np.percentile(frame_points @ up, 0.2))
        has_table = False
    else:
        has_table = True
    # With plenty measured, line the model up on that alone, so a leftover flap can't twist the frame
    is_real_measured = kind[trusted] == MEASURED
    rotation, shift = upright_frame(frame_points[is_real_measured] if is_real_measured.sum() > 2000 else frame_points, up, offset)
    upright = xyz @ rotation.T + shift
    if (kind == MEASURED).sum() > 2000:
        real = upright[kind == MEASURED]
        real_low, real_high = np.percentile(real, 0.2, axis=0), np.percentile(real, 99.8, axis=0)
        margin = float(np.ptp(real, axis=0).max()) * 0.005
        off_box = kind == OUTLINE
        if measured_share >= TRUST_MEASURED_SHARE:
            off_box = off_box & np.any((upright < real_low - margin) | (upright > real_high + margin), axis=1)
        elif not has_table:
            off_box = off_box & (upright[:, 1] < real_low[1] - margin)  # no table to stop the hull running on underneath
        else:
            off_box = np.zeros(len(tagged), bool)
        tagged = tagged[~off_box]
        upright = upright[~off_box]
        kind = tagged["source"]
        tagged = tagged[~past_flat_faces(upright, kind == MEASURED, kind == OUTLINE)]
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
    # The frame and the size come from measured points and the outlines. An AI estimate can never change them.
    is_real = source != AI_DEPTH
    rotation, shift = upright_frame(points[is_real], up, offset)
    points = points @ rotation.T + shift
    normals = normals @ rotation.T
    vertices = np.asarray(mesh.vertices) @ rotation.T + shift
    vertices[:, 1] = np.maximum(vertices[:, 1], 0)  # the surface can't go through the table

    # Trim the extreme 0.2% so one stray point can't change the reported size
    low = np.percentile(points[is_real], 0.2, axis=0)
    high = np.percentile(points[is_real], 99.8, axis=0)
    sizes = high - low
    sizes[1] = high[1]  # height is measured from the table, or from the base where no table was found
    longest = float(sizes.max())

    if longest_mm:
        scale = longest_mm / 1000 / longest  # meters, like the rest of Unity
    else:
        scale = 1 / longest
        warnings.append("No real size given: the proportions are right but the size isn't in millimeters.")
    # The mesher can balloon past the data over unmeasured faces. The surface can't be bigger than what was measured.
    margin = longest * 0.005
    box_low, box_high = low - margin, high + margin
    box_low[1] = 0
    vertices = np.clip(vertices, box_low, box_high) * scale
    points = points * scale

    colors = np.stack([cloud["red"], cloud["green"], cloud["blue"]], axis=1)
    reach = float(sizes.max() * scale * 0.012)
    final = trimesh.Trimesh(vertices, np.asarray(mesh.faces), process=False)
    exported = export_models(final, points, colors, source, work, reach)

    # The measured-only version, so anyone can see exactly what the helpers added
    if not is_measured.all() and is_measured.sum() >= 500:
        write_cloud(work / "object_measured.ply", cloud[is_measured])
        try:
            plain = build_mesh(work / "object_measured.ply", work)
        except ScanFailed:
            warnings.append("Too little was measured directly to build a measured-only model.")
        else:
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
        has_table=has_table, longest=sizes.max() * scale, height=sizes[1] * scale,
        rotation=rotation, shift=shift, scale=scale,
    )
    (work / "views.json").write_text(json.dumps(views))

    aligned = best.num_reg_images()
    if aligned < total * 0.8:
        warnings.append(f"{total - aligned} of {total} photos couldn't be used, so some sides may be less detailed.")
    if exported["shares"]["outline"] > exported["shares"]["measured"]:
        warnings.append(
            "Most of this surface comes from the piece's outline, so its overall shape and size are good but dents and "
            "hollows won't show. Dusting it with powder lets more of it get measured."
        )

    report = {
        "mode": "measured",
        "outline_agreement": round(check["agreement"], 3) if check else None,
        "object_only_solve": bool(check["object_only"]) if check else False,
        "photos_uploaded": total,
        "photos_used": aligned,
        "reprojection_error_px": round(best.compute_mean_reprojection_error(), 3),
        "points_measured": measured_points,
        "points_on_object": int(is_measured.sum()),
        "points_ai_depth": int((source == AI_DEPTH).sum()),
        "points_outline": int((source == OUTLINE).sum()),
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


def past_flat_faces(points: np.ndarray, is_measured: np.ndarray, is_outline: np.ndarray) -> np.ndarray:
    """Outline points sticking out past a flat side that was really measured.

    Looks at each side of the measured points' box, except the bottom. If measured points cover a good part of that
    side, it's a real flat face and nothing can be outside it. A rounded side only touches its box along a line, so it
    doesn't count, and keeps its outline.
    """
    measured = points[is_measured]
    low, high = np.percentile(measured, 0.2, axis=0), np.percentile(measured, 99.8, axis=0)
    longest = float((high - low).max())
    slab, margin = longest * FACE_THICKNESS, longest * 0.005
    outside = np.zeros(len(points), bool)
    for axis, direction in ((0, -1), (0, 1), (1, 1), (2, -1), (2, 1)):
        plane = low[axis] if direction < 0 else high[axis]
        on_side = measured[np.abs(measured[:, axis] - plane) < slab]
        if not len(on_side):
            continue
        across = [other for other in range(3) if other != axis]
        cells = np.floor((on_side[:, across] - low[across]) / np.maximum(high[across] - low[across], 1e-9) * FACE_CELLS).astype(int)
        cells = cells[np.all((cells >= 0) & (cells < FACE_CELLS), axis=1)]
        covered = len(np.unique(cells, axis=0)) / FACE_CELLS**2
        print(f"flat face check: axis={axis} direction={direction} covered={covered:.2f}", flush=True)
        if covered >= FACE_COVERAGE:
            outside |= is_outline & (direction * (points[:, axis] - plane) > margin)
    return outside


def trim_flaps(hull_points: np.ndarray, views: list, work: Path, object_size: float) -> np.ndarray:
    """Which hull points stick out past surface that was really measured."""
    fused = work / "fused.ply"
    visibility, order_file = work / "fused.ply.vis", work / "dense" / "stereo" / "fusion.cfg"
    if not (fused.exists() and visibility.exists() and order_file.exists()):
        return np.zeros(len(hull_points), bool)
    cloud = tagged_xyz(read_cloud(fused))
    inside, seen = scan_hull.object_votes(views, cloud)
    return scan_hull.seen_through(
        hull_points, views, cloud, (seen >= 2) & (inside >= seen * 0.8), scan_hull.read_visibility(visibility),
        order_file.read_text().split(), object_size * 0.01,
    )


def measure_final(work: Path, longest_mm: float | None, check_mm: float | None, report: dict) -> None:
    """Sets the real size on the finished model files, then reads the size back off them.

    Every step before this works on points, and building a mesh can lose some of them, so the size has to be measured
    on the mesh itself. That way the numbers in the report are always the numbers in the file. Run again after gap
    filling, since that changes the mesh.
    """
    loaded = trimesh.load(work / "model.glb", process=False)
    model = loaded.to_geometry() if isinstance(loaded, trimesh.Scene) else loaded
    sizes = model.bounds[1] - model.bounds[0]
    longest = float(sizes.max())
    if longest <= 0:
        raise ScanFailed("The finished model came out empty, so it can't be measured. Please try again with more photos.")
    factor = longest_mm / 1000 / longest if longest_mm else 1.0
    for name in ("model.glb", "model_sources.glb", "model_measured.glb"):
        path = work / name
        if path.exists():
            part = trimesh.load(path, process=False)
            part = part.to_geometry() if isinstance(part, trimesh.Scene) else part
            part.apply_scale(factor)
            part.export(path)
    sizes = sizes * factor
    report["size_mm"] = [round(float(side) * 1000, 2) for side in sizes] if longest_mm else None
    report["proportions"] = [round(float(side / sizes.max()), 4) for side in sizes]
    report["scaled_from_mm"] = longest_mm
    report["checked_with_mm"] = check_mm
    report["check"] = None
    # The longest side matches by definition, since it sets the scale. A second measurement is the real test.
    if longest_mm and check_mm:
        others = sorted(sizes * 1000)[:2]
        closest = float(min(others, key=lambda side: abs(side - check_mm)))
        off = closest - check_mm
        report["check"] = {"typed_mm": check_mm, "model_mm": round(closest, 2), "off_mm": round(off, 2), "off_pct": round(off / check_mm * 100, 1)}
        if abs(off / check_mm) > 0.05:
            report["warnings"].append(
                f"The side you measured as {check_mm} mm is {closest:.1f} mm on the model ({off / check_mm * 100:+.1f}%). "
                "Treat these sizes with caution, or re-measure and scan again."
            )
    (work / "report.json").write_text(json.dumps(report, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("photos", type=Path)
    parser.add_argument("work", type=Path)
    parser.add_argument("--longest-mm", type=float)
    parser.add_argument("--check-mm", type=float, help="another side of the piece, to test the scale against")
    parser.add_argument("--ai-python", required=True, help="python of the environment with rembg and torch")
    parser.add_argument("--ai-depth", action="store_true", help="turns on the plain-face estimate")
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    try:
        report = run(args.photos, args.work, args.longest_mm, args.ai_python, args.ai_depth, args.check_mm)
    except ScanFailed as exc:
        print(f"ERROR {exc}", flush=True)
        return 2
    (args.work / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
