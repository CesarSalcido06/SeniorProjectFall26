"""Builds the object's shape from its outlines: anything that lands outside the outline in a photo can't be the object.

Carve away everything the outlines rule out and what's left is the "hull", a shape that's tight on the outside but
can't show dents or hollows, since those never appear in an outline. It works on plain and even clear objects,
which is where matching points between photos falls apart, and it's also the check for whether a scan makes sense:
if the outlines can't agree on one solid shape, the object moved or the cameras are wrong.
"""
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pycolmap
from PIL import Image
from scipy import ndimage

# How well a photo's outline has to match the shape, as overlap / combined area
GOOD_PHOTO = 0.72
GOOD_AVERAGE = 0.80
# A measured scan needs this many photos that agree, and no gap around the object wider than this
MIN_AGREEING_PHOTOS = 8
MAX_GAP_DEGREES = 150
# Each pass zooms in on what the last one found. Fine detail only matters on the final shape.
FAST_PASSES = (96, 140)
FULL_PASSES = (96, 160, 220)
PASS_PADDING = (0.15, 0.08, 0.03)


@dataclass
class View:
    name: str
    pose: np.ndarray
    camera: object
    outline: np.ndarray
    center: np.ndarray
    photo: Path


@dataclass
class Hull:
    solid: np.ndarray  # True where the object is
    axes: list  # the grid's coordinates along x, y and z

    def points(self) -> np.ndarray:
        cells = np.argwhere(self.solid)
        return np.stack([self.axes[i][cells[:, i]] for i in range(3)], axis=1)


def load_views(reconstruction, outlines: Path, images: Path) -> list[View]:
    views = []
    for image in reconstruction.images.values():
        if image.has_pose:
            outline = np.asarray(Image.open(outlines / f"{image.name}.png")) > 128
            views.append(View(
                image.name, image.cam_from_world().matrix(), reconstruction.cameras[image.camera_id],
                outline, np.asarray(image.projection_center()), images / image.name,
            ))
    return sorted(views, key=lambda view: view.name)


def to_pixels(view: View, points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Rows and columns of each point in a photo, and whether it landed in the photo at all."""
    camera_space = points @ view.pose[:3, :3].T + view.pose[:3, 3]
    front = camera_space[:, 2] > 1e-6
    pixels = np.full((len(points), 2), -1.0)
    pixels[front] = view.camera.img_from_cam(camera_space[front])
    # Points right beside the camera can project absurdly far away
    pixels = np.clip(np.nan_to_num(pixels, nan=-1.0, posinf=-1.0, neginf=-1.0), -1.0, 1e6)
    columns, rows = np.floor(pixels[:, 0]).astype(int), np.floor(pixels[:, 1]).astype(int)
    height, width = view.outline.shape
    return rows, columns, front & (columns >= 0) & (columns < width) & (rows >= 0) & (rows < height)


def inside_outline(view: View, points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """For each point: is it on the object in this photo, and could this photo see it at all."""
    rows, columns, in_frame = to_pixels(view, points)
    inside = np.zeros(len(points), bool)
    inside[in_frame] = view.outline[rows[in_frame], columns[in_frame]]
    return inside, in_frame


def object_votes(views: list[View], points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """How many photos put each point on the object, and how many photos could see it."""
    inside_count, seen_count = np.zeros(len(points), np.int32), np.zeros(len(points), np.int32)
    for view in views:
        inside, in_frame = inside_outline(view, points)
        inside_count += inside
        seen_count += in_frame
    return inside_count, seen_count


def _aim_point(views: list[View]) -> np.ndarray:
    """The spot closest to the line through the middle of every outline."""
    system, target = np.zeros((3, 3)), np.zeros(3)
    for view in views:
        rows, columns = np.nonzero(view.outline[::8, ::8])
        middle = np.array([[columns.mean() * 8, rows.mean() * 8]])
        direction = np.r_[view.camera.cam_from_img(middle)[0], 1.0] @ view.pose[:3, :3]
        direction /= np.linalg.norm(direction)
        project = np.eye(3) - np.outer(direction, direction)
        system += project
        target += project @ view.center
    return np.linalg.solve(system, target)


def carve(views: list[View], up: np.ndarray | None = None, offset: float = 0.0, passes=FULL_PASSES) -> Hull | None:
    """Keeps the grid cells every photo but one puts on the object. One gets a pass, since an outline can be wrong."""
    aim = _aim_point(views)
    reach = float(np.median([np.linalg.norm(view.center - aim) for view in views])) * 0.9
    low, high = aim - reach, aim + reach
    hull = None
    for size, padding in zip(passes, PASS_PADDING):
        axes = [np.linspace(low[i], high[i], size) for i in range(3)]
        cells = np.stack(np.meshgrid(*axes, indexing="ij"), axis=-1).reshape(-1, 3)
        inside, seen = object_votes(views, cells)
        solid = (seen >= max(3, len(views) // 2)) & (seen - inside <= 1)
        if up is not None:
            solid &= cells @ up - offset > 0
        solid = solid.reshape(size, size, size)
        labels, count = ndimage.label(solid)
        if count == 0:
            return None
        solid = labels == int(np.argmax(ndimage.sum(solid, labels, range(1, count + 1)))) + 1
        hull = Hull(solid, axes)
        found = hull.points()
        margin = float(np.ptp(found, axis=0).max()) * padding
        low, high = found.min(axis=0) - margin, found.max(axis=0) + margin
    return hull


def outline_match(view: View, hull: Hull) -> float:
    """How well the shape's outline, seen from this photo, covers the object's real outline. 1 is perfect."""
    points = hull.points()
    points = points[:: max(1, len(points) // 60_000)]
    rows, columns, in_frame = to_pixels(view, points)
    target = view.outline[::4, ::4]
    drawn = np.zeros(target.shape, bool)
    drawn[np.minimum(rows[in_frame] // 4, target.shape[0] - 1), np.minimum(columns[in_frame] // 4, target.shape[1] - 1)] = True
    drawn = ndimage.binary_closing(drawn, iterations=3)
    return float((drawn & target).sum() / max(1, (drawn | target).sum()))


def camera_up(views: list[View]) -> np.ndarray:
    """People hold the phone roughly level, so the average of the cameras' up direction is the object's up."""
    up = np.mean([-view.pose[1, :3] for view in views], axis=0)
    return up / np.linalg.norm(up)


def widest_gap(views: list[View], middle: np.ndarray, up: np.ndarray) -> float:
    """The widest stretch, in degrees, of going around the object without a photo."""
    seed = np.array([1.0, 0, 0]) if abs(up[0]) < 0.9 else np.array([0, 1.0, 0])
    side = np.cross(up, seed)
    side /= np.linalg.norm(side)
    other = np.cross(up, side)
    offsets = np.array([view.center for view in views]) - middle
    angles = np.sort(np.degrees(np.arctan2(offsets @ other, offsets @ side)) % 360)
    return float(np.diff(np.r_[angles, angles[0] + 360]).max())


def agreeing_views(views: list[View]) -> dict:
    """Drops photos that disagree with the rest, one at a time, then says whether what's left can be measured."""
    kept = list(views)
    dropped: list[str] = []
    while True:
        hull = carve(kept, passes=FAST_PASSES)
        if hull is None:
            return {"usable": False, "reason": "disagree", "views": [], "dropped": [v.name for v in views], "agreement": 0.0, "gap": 360.0}
        scores = {view.name: outline_match(view, hull) for view in kept}
        worst = min(scores, key=scores.get)
        if scores[worst] >= GOOD_PHOTO or len(kept) <= MIN_AGREEING_PHOTOS:
            break
        dropped.append(worst)
        kept = [view for view in kept if view.name != worst]
    agreement = float(np.mean(list(scores.values())))
    gap = widest_gap(kept, hull.points().mean(axis=0), camera_up(kept))
    if gap > MAX_GAP_DEGREES:
        reason = "no_circle"
    elif agreement < GOOD_AVERAGE or len(kept) < MIN_AGREEING_PHOTOS:
        reason = "disagree"
    else:
        reason = None
    return {"usable": reason is None, "reason": reason, "views": kept, "dropped": dropped, "agreement": agreement, "gap": gap}


def read_visibility(path: Path) -> dict[int, np.ndarray]:
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


def seen_through(points: np.ndarray, views: list[View], cloud: np.ndarray, on_object: np.ndarray, seen_by: dict[int, np.ndarray],
                 photo_order: list[str], tolerance: float, min_votes: int = 2, cell: int = 8) -> np.ndarray:
    """True for points a camera looked straight through to see measured surface behind them.

    Outlines can't see into a corner, so the hull sticks out past flat faces. Those flaps can't be solid: photos saw
    the real surface behind them. Each photo only uses the cloud points it actually saw, because a point on the far
    side would otherwise make the near side look see-through.
    """
    order = {name: index for index, name in enumerate(photo_order)}
    votes = np.zeros(len(points), np.int32)

    def depth_of(view: View, where: np.ndarray) -> np.ndarray:
        return (where @ view.pose[:3, :3].T + view.pose[:3, 3])[:, 2]

    for view in views:
        chosen = seen_by.get(order.get(view.name, -1))
        if chosen is None:
            continue
        chosen = cloud[chosen[on_object[chosen]]]
        if not len(chosen):
            continue
        # The nearest measured surface in each small patch of the photo
        rows, columns, in_frame = to_pixels(view, chosen)
        nearest = np.full((view.outline.shape[0] // cell + 1, view.outline.shape[1] // cell + 1), np.inf)
        np.minimum.at(nearest, (rows[in_frame] // cell, columns[in_frame] // cell), depth_of(view, chosen)[in_frame])
        rows, columns, in_frame = to_pixels(view, points)
        behind = np.full(len(points), np.inf)
        behind[in_frame] = nearest[rows[in_frame] // cell, columns[in_frame] // cell]
        votes += np.isfinite(behind) & (depth_of(view, points) < behind - tolerance)
    return votes >= min_votes


def surface(hull: Hull, views: list[View], count: int = 150_000) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Points on the outside of the shape, with outward normals, colored from the photo that faces each one best."""
    smooth = ndimage.gaussian_filter(hull.solid.astype(np.float32), 1.0)
    gradient = np.stack(np.gradient(smooth), axis=-1)
    skin = hull.solid & ~ndimage.binary_erosion(hull.solid)
    cells = np.argwhere(skin)
    if len(cells) > count:
        cells = cells[np.random.default_rng(0).choice(len(cells), count, replace=False)]
    spacing = np.array([axis[1] - axis[0] for axis in hull.axes])
    normals = -gradient[cells[:, 0], cells[:, 1], cells[:, 2]] / spacing
    lengths = np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-9)
    normals = normals / lengths
    # Smooth out the grid's stair steps by sliding each point to where the blurred shape crosses halfway
    slide = (smooth[cells[:, 0], cells[:, 1], cells[:, 2]] - 0.5)[:, None] / lengths
    points = np.stack([hull.axes[i][cells[:, i]] for i in range(3)], axis=1) + normals * np.clip(slide, -spacing.max(), spacing.max())

    colors = np.full((len(points), 3), 170, np.uint8)
    best_facing = np.full(len(points), -1.0)
    for view in views:
        toward = view.center - points
        facing = np.sum(normals * toward, axis=1) / np.maximum(np.linalg.norm(toward, axis=1), 1e-9)
        rows, columns, in_frame = to_pixels(view, points)
        better = in_frame & (facing > best_facing)
        if better.any():
            with Image.open(view.photo) as opened:
                pixels = np.asarray(opened.convert("RGB"))
            colors[better] = pixels[rows[better], columns[better]]
            best_facing[better] = facing[better]
    return points, normals, colors
