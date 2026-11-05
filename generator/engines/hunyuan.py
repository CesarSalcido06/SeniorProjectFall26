"""Hunyuan3D-2: Hunyuan3D-2mini-turbo (one photo) or Hunyuan3D-2mv-turbo (several angles) for shape,
Hunyuan3D-Paint v2.0 turbo for texture.

Fits a 12 GB GPU: one shape model at a time runs fully on the GPU, the paint model is CPU-offloaded.
License: Tencent Hunyuan 3D 2.0 Community License (not valid in the EU, UK, or South Korea).
"""
import sys
from pathlib import Path

VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "Hunyuan3D-2"
sys.path.insert(0, str(VENDOR))

import torch  # noqa: E402

SHAPE_REPO, SHAPE_SUBFOLDER = "tencent/Hunyuan3D-2mini", "hunyuan3d-dit-v2-mini-turbo"
# Multi-view model: takes front/left/back/right photos (any subset that includes front)
MV_REPO, MV_SUBFOLDER = "tencent/Hunyuan3D-2mv", "hunyuan3d-dit-v2-mv-turbo"
VIEW_ORDER = ("front", "left", "back", "right")
PAINT_REPO, PAINT_SUBFOLDER = "tencent/Hunyuan3D-2", "hunyuan3d-paint-v2-0-turbo"


class HunyuanEngine:
    name = "hunyuan"

    def __init__(self, max_faces: int = 40000, octree_resolution: int = 380) -> None:
        self.max_faces = max_faces
        self.octree_resolution = octree_resolution
        self._shape = None
        self._shape_kind = None  # "single" or "multiview"
        self._paint = None
        self._cleanup = None

    @property
    def loaded(self) -> bool:
        return self._shape is not None

    def load(self, multiview: bool = False, paint: bool = True) -> None:
        from hy3dgen.shapegen import DegenerateFaceRemover, FaceReducer, FloaterRemover, Hunyuan3DDiTFlowMatchingPipeline
        from hy3dgen.texgen import Hunyuan3DPaintPipeline

        kind = "multiview" if multiview else "single"
        if self._shape_kind != kind:
            # Only one shape model fits next to the paint model, so swap rather than keep both
            self._shape = self._shape_kind = None
            torch.cuda.empty_cache()
            repo, subfolder = (MV_REPO, MV_SUBFOLDER) if multiview else (SHAPE_REPO, SHAPE_SUBFOLDER)
            shape = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(
                repo, subfolder=subfolder, use_safetensors=False, device="cuda"
            )
            shape.enable_flashvdm(topk_mode="merge")
            self._shape, self._shape_kind = shape, kind
        if self._cleanup is None:
            self._cleanup = (FloaterRemover(), DegenerateFaceRemover(), FaceReducer())
        if paint and self._paint is None:
            pipeline = Hunyuan3DPaintPipeline.from_pretrained(PAINT_REPO, subfolder=PAINT_SUBFOLDER)
            pipeline.enable_model_cpu_offload()  # without this, shape + paint don't fit in 12 GB
            self._paint = pipeline

    def unload(self) -> None:
        self._shape = self._shape_kind = self._paint = self._cleanup = None
        torch.cuda.empty_cache()

    def generate(self, images: dict, texture: bool, progress) -> "trimesh.Trimesh":
        """images: {"front": RGBA PIL image, optionally "left"/"back"/"right"}, backgrounds already removed."""
        multiview = len(images) > 1
        self.load(multiview, paint=texture)  # scan gap filling only wants the shape
        progress(10, "shape (multi-view)" if multiview else "shape")
        mesh = self._shape(
            image=images if multiview else images["front"],
            num_inference_steps=5,
            octree_resolution=self.octree_resolution,
            num_chunks=20000,
            generator=torch.manual_seed(12345),
            output_type="trimesh",
        )[0]
        floaters, degenerate, reducer = self._cleanup
        mesh = reducer(degenerate(floaters(mesh)), max_facenum=self.max_faces)
        if not texture:
            return mesh
        progress(45, "texture")
        # The paint model takes several reference photos; front first, since it's the main one
        return self._paint(mesh, image=[images[v] for v in VIEW_ORDER if v in images])
