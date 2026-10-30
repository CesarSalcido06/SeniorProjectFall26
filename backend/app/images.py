"""Photo pre-processing: reject unusable photos, then normalize what Meshy and the classifier receive."""
import io

from PIL import Image, ImageFilter, ImageOps, ImageStat, UnidentifiedImageError

# 3x3 Laplacian. Its variance is a standard sharpness score: blurry photos have few strong edges.
_LAPLACIAN = ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1, offset=128)


class PhotoRejected(ValueError):
    """The upload can't be used. `reason` is safe to show to the user."""

    def __init__(self, reason: str, quality: dict | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.quality = quality or {}


def measure(img: Image.Image) -> dict:
    gray = img.convert("L")
    # Score sharpness at a fixed size so the threshold doesn't depend on camera resolution
    gray.thumbnail((640, 640))
    sharpness = ImageStat.Stat(gray.filter(_LAPLACIAN)).var[0]
    brightness = ImageStat.Stat(gray).mean[0]
    return {"sharpness": round(sharpness, 1), "brightness": round(brightness, 1)}


def process_photo(data: bytes, settings, force: bool = False) -> tuple[bytes, dict]:
    """Returns (normalized JPEG bytes, quality report). Raises PhotoRejected.

    force=True skips the blur/lighting checks (handy for testing odd photos) but never
    lets through files that aren't images or are too small.
    """
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise PhotoRejected(f"Photo is larger than {settings.max_upload_mb:g} MB.")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError):
        raise PhotoRejected("That file isn't a readable image (use JPG, PNG, WebP, or HEIC exported as JPG).")

    img = ImageOps.exif_transpose(img)  # phone photos are often stored sideways with a rotation tag
    if img.mode != "RGB":
        background = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        background.paste(rgba, mask=rgba.getchannel("A"))
        img = background

    width, height = img.size
    quality = {"width": width, "height": height, **measure(img), "forced": force}

    if min(width, height) < settings.min_side:
        raise PhotoRejected(
            f"Photo is too small ({width}x{height}); the short side needs at least {settings.min_side}px.", quality
        )
    if not force:
        if quality["sharpness"] < settings.blur_threshold:
            raise PhotoRejected("Photo looks blurry. Hold steady, tap to focus, and retake.", quality)
        if quality["brightness"] < settings.dark_threshold:
            raise PhotoRejected("Photo is too dark. Add light or move somewhere brighter.", quality)
        if quality["brightness"] > settings.bright_threshold:
            raise PhotoRejected("Photo is overexposed. Avoid direct light behind or on the object.", quality)

    img.thumbnail((settings.max_side, settings.max_side), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90)  # re-encoding also drops EXIF (GPS location etc.)
    quality["normalized_width"], quality["normalized_height"] = img.size
    return out.getvalue(), quality
