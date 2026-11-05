"""Fills in plain, shiny faces that COLMAP couldn't measure, using depth maps from scan_depth_model.py.

A depth-from-one-photo model sees the shape of a blank face but has no real scale. COLMAP has real scale but sees
nothing on a blank face. So for each photo:
  1. Fit the model's depth to the points COLMAP did measure, and bend it locally to match them.
  2. Hide a fifth of those points from the fit and check how well it predicts them. Bad fits get thrown out.
  3. Only keep a new point if several other photos, each fitted on their own, put the surface in the same place.

The result is a set of extra points, marked as estimated. They never move or replace a measured point.
"""
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage
from scipy.spatial import cKDTree

STRIDE = 4  # work on every 4th pixel: plenty of detail, and much quicker
AGREE_TOLERANCE = 0.006  # how close two photos must land to count as agreeing, as a fraction of the object's size
MIN_AGREEING = 3  # other photos that must agree with a new point
MAX_HELDOUT_ERROR = 0.012  # a photo whose fit misses hidden points by more than this (fraction of size) is dropped
MIN_ANCHORS = 300


@dataclass
class View:
    rotation: np.ndarray
    translation: np.ndarray
    fx: float
    fy: float
    cx: float
    cy: float
    relative: np.ndarray  # the model's raw output on the working grid
    colors: np.ndarray
    center: np.ndarray
    depth: np.ndarray | None = None  # fitted depth on the working grid, nan where it can't be trusted
    measured: np.ndarray | None = None  # grid cells that already have a measured point close by

    def project(self, points: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Returns (grid row, grid column, depth). Row and column are -1 for points outside the photo."""
        cam = points @ self.rotation.T + self.translation
        z = cam[:, 2]
        safe = np.where(z > 1e-9, z, np.nan)
        col = np.round((self.fx * cam[:, 0] / safe + self.cx - STRIDE // 2) / STRIDE)
        row = np.round((self.fy * cam[:, 1] / safe + self.cy - STRIDE // 2) / STRIDE)
        height, width = self.relative.shape
        inside = (col >= 0) & (col < width) & (row >= 0) & (row < height)
        return np.where(inside, row, -1).astype(int), np.where(inside, col, -1).astype(int), z


def load_view(image, camera, dense: Path, predictions: Path) -> View | None:
    path = predictions / Path(image.name).with_suffix(".npy")
    if not path.exists():
        return None
    start = STRIDE // 2
    pose = image.cam_from_world().matrix()
    fx, fy, cx, cy = camera.params
    with Image.open(dense / "images" / image.name) as photo:
        colors = np.asarray(photo.convert("RGB"))[start::STRIDE, start::STRIDE]
    return View(
        pose[:3, :3], pose[:3, 3], fx, fy, cx, cy, np.load(path)[start::STRIDE, start::STRIDE], colors,
        np.asarray(image.projection_center()),
    )


def robust_line(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Straight line through (x, y), ignoring points that sit far off it."""
    keep = np.ones(len(x), bool)
    slope, offset = 0.0, 0.0
    for _ in range(6):
        slope, offset = np.polyfit(x[keep], y[keep], 1)
        residual = np.abs(y - (slope * x + offset))
        keep = residual < max(2.5 * 1.4826 * np.median(residual), 1e-9)
    return float(slope), float(offset)


def fit_view(view: View, points: np.ndarray, normals: np.ndarray, size: float, rng) -> np.ndarray | None:
    """Sets view.depth from the object's measured points. Returns the errors on the hidden points, or None if it can't fit.

    The table is left out on purpose: tested with it, the fit got worse, because the model's depth scale over a big
    flat sheet doesn't carry over to a small object sitting on it.
    """
    row, col, z = view.project(points)
    # Only measured points this photo can actually see: facing it, and not behind something closer
    to_camera = view.center - points
    to_camera /= np.linalg.norm(to_camera, axis=1, keepdims=True)
    seen = (row >= 0) & (z > 0) & (np.sum(normals * to_camera, axis=1) > 0.25)
    row, col, z = row[seen], col[seen], z[seen]
    coarse = row // 4 * 100_000 + col // 4
    nearest = np.full(coarse.max() + 1 if len(coarse) else 1, np.inf)
    np.minimum.at(nearest, coarse, z)
    front = z <= nearest[coarse] + size * 0.02
    row, col, z = row[front], col[front], z[front]
    # These models blur across edges, so a point right on an edge says little about the depth there: leave them out.
    # Only the fit and the check skip them. They still count as measured, so nothing gets added on top of them.
    has_point = np.zeros(view.relative.shape, bool)
    has_point[row, col] = True
    view.measured = ndimage.binary_dilation(has_point, iterations=2)
    change_y, change_x = np.gradient(view.relative)
    change = np.hypot(change_x, change_y)
    edges = ndimage.binary_dilation(change > np.percentile(change, 90))
    calm = ~edges[row, col]
    row, col, z = row[calm], col[calm], z[calm]
    if len(z) < MIN_ANCHORS:
        return None

    hidden = rng.random(len(z)) < 0.2
    train_row, train_col, train_inverse = row[~hidden], col[~hidden], 1 / z[~hidden]
    slope, offset = robust_line(view.relative[train_row, train_col], train_inverse)
    if slope <= 0:  # bigger has to mean closer, or the fit found nonsense
        return None

    # Smooth local correction: leftovers of the straight-line fit, spread out to the cells around them
    leftover = train_inverse - (slope * view.relative[train_row, train_col] + offset)
    limit = 2.5 * 1.4826 * np.median(np.abs(leftover))
    usable = np.abs(leftover) < max(limit, 1e-9)
    total = np.zeros(view.relative.shape)
    weight = np.zeros(view.relative.shape)
    np.add.at(total, (train_row[usable], train_col[usable]), leftover[usable])
    np.add.at(weight, (train_row[usable], train_col[usable]), 1.0)
    sigma = view.relative.shape[1] / 16
    spread = ndimage.gaussian_filter(weight, sigma)
    correction = ndimage.gaussian_filter(total, sigma) / np.maximum(spread, 1e-9)

    inverse = slope * view.relative + offset + correction
    depth = np.where(inverse > 0, 1 / np.maximum(inverse, 1e-9), np.nan)
    depth[spread < 1e-4] = np.nan  # too far from anything measured to bend it with any confidence
    view.depth = depth
    return np.abs(depth[row[hidden], col[hidden]] - z[hidden])


def back_project(view: View) -> tuple[np.ndarray, np.ndarray]:
    """Turns the fitted depth grid into world points and surface normals facing the camera."""
    height, width = view.depth.shape
    rows, cols = np.mgrid[0:height, 0:width]
    x = (cols * STRIDE + STRIDE // 2 - view.cx) / view.fx * view.depth
    y = (rows * STRIDE + STRIDE // 2 - view.cy) / view.fy * view.depth
    cam = np.stack([x, y, view.depth], axis=-1)
    world = (cam - view.translation) @ view.rotation
    normals = np.cross(np.gradient(world, axis=1), np.gradient(world, axis=0))
    normals /= np.maximum(np.linalg.norm(normals, axis=-1, keepdims=True), 1e-12)
    flip = np.sum(normals * (view.center - world), axis=-1) < 0
    normals[flip] = -normals[flip]
    return world, normals


def new_points(views: list[View], size: float, keep_region) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Points on unmeasured surface that enough photos agree on. Returns (points, normals, colors)."""
    tolerance = size * AGREE_TOLERANCE
    all_points, all_normals, all_colors = [], [], []
    for view in views:
        world, normals = back_project(view)
        to_camera = view.center - world
        to_camera /= np.maximum(np.linalg.norm(to_camera, axis=-1, keepdims=True), 1e-12)
        # Faces seen nearly edge-on are where these models smear foreground into background
        facing = np.sum(normals * to_camera, axis=-1) > 0.3
        wanted = np.isfinite(view.depth) & ~view.measured & facing
        points = world[wanted]
        inside = keep_region(points)
        points, point_normals, colors = points[inside], normals[wanted][inside], view.colors[wanted][inside]
        if not len(points):
            continue
        agree = np.zeros(len(points), int)
        floating = np.zeros(len(points), int)
        for other in views:
            if other is view:
                continue
            row, col, z = other.project(points)
            found = row >= 0
            there = np.full(len(points), np.nan)
            there[found] = other.depth[row[found], col[found]]
            agree += np.abs(z - there) < tolerance
            floating += z < there - tolerance  # the other photo sees straight through where this point would be
        good = (agree >= MIN_AGREEING) & (floating <= 0.25 * (agree + floating))
        all_points.append(points[good])
        all_normals.append(point_normals[good])
        all_colors.append(colors[good])
    if not all_points:
        return np.empty((0, 3)), np.empty((0, 3)), np.empty((0, 3), np.uint8)
    return np.concatenate(all_points), np.concatenate(all_normals), np.concatenate(all_colors)


def thin_out(points, normals, colors, spacing: float):
    """Averages everything landing in the same small cube. Also steadies the estimate, since many photos vote."""
    cells = np.floor(points / spacing).astype(np.int64)
    _, group, counts = np.unique(cells, axis=0, return_inverse=True, return_counts=True)
    group = group.ravel()
    mean = lambda values: np.stack([np.bincount(group, values[:, i]) / counts for i in range(3)], axis=1)
    out_normals = mean(normals)
    out_normals /= np.maximum(np.linalg.norm(out_normals, axis=1, keepdims=True), 1e-12)
    return mean(points), out_normals, mean(colors.astype(float)), counts


def estimate(reconstruction, dense: Path, predictions: Path, cloud: np.ndarray, object_points: np.ndarray, keep_region):
    """cloud: the object's measured points, object_points: the same as an array. Returns (extra points, stats)."""
    size = float(np.linalg.norm(np.ptp(object_points, axis=0)))
    points = object_points
    normals = np.stack([cloud["nx"], cloud["ny"], cloud["nz"]], axis=1).astype(np.float64)
    rng = np.random.default_rng(0)
    views, errors, dropped = [], [], 0
    for image in reconstruction.images.values():
        view = load_view(image, reconstruction.cameras[image.camera_id], dense, predictions) if image.has_pose else None
        if view is None:
            continue
        missed = fit_view(view, points, normals, size, rng)
        # A photo that barely sees the object's measured points can't be checked, so it can't be trusted either
        if missed is None or np.isfinite(missed).sum() < 20 or np.nanmedian(missed) > size * MAX_HELDOUT_ERROR:
            dropped += 1
            continue
        views.append(view)
        errors.append(missed[np.isfinite(missed)])

    stats = {"photos_fitted": len(views), "photos_dropped": dropped, "points_added": 0, "check_median": None, "check_p90": None}
    extra = np.empty(0, dtype=cloud.dtype)
    if len(views) <= MIN_AGREEING:
        return extra, stats
    errors = np.concatenate(errors)
    stats["check_median"], stats["check_p90"] = float(np.median(errors)), float(np.percentile(errors, 90))

    found, found_normals, colors = new_points(views, size, keep_region)
    if not len(found):
        return extra, stats
    found, found_normals, colors, _ = thin_out(found, found_normals, colors, size * 0.004)
    # Measured surface wins: drop anything sitting right on top of it
    open_space = cKDTree(object_points).query(found)[0] > size * 0.006
    found, found_normals, colors = found[open_space], found_normals[open_space], colors[open_space]

    extra = np.zeros(len(found), dtype=cloud.dtype)
    for index, axis in enumerate("xyz"):
        extra[axis] = found[:, index]
        extra["n" + axis] = found_normals[:, index]
    for index, channel in enumerate(("red", "green", "blue")):
        extra[channel] = colors[:, index]
    stats["points_added"] = len(extra)
    return extra, stats
