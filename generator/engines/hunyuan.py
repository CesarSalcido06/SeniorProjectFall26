"""Hunyuan3D-2: Hunyuan3D-2mini-turbo for shape, Hunyuan3D-Paint v2.0 turbo for texture.

Fits a 12 GB GPU: the shape model runs fully on the GPU, the paint model is CPU-offloaded.
License: Tencent Hunyuan 3D 2.0 Community License (not valid in the EU, UK, or South Korea).
"""
import sys
from pathlib import Path

VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "Hunyuan3D-2"
sys.path.insert(0, str(VENDOR))

import torch  # noqa: E402

SHAPE_REPO, SHAPE_SUBFOLDER = "tencent/Hunyuan3D-2mini", "hunyuan3d-dit-v2-mini-turbo"
PAINT_REPO, PAINT_SUBFOLDER = "tencent/Hunyuan3D-2", "hunyuan3d-paint-v2-0-turbo"


class HunyuanEngine:
    name = "hunyuan"

    def __init__(self, max_faces: int = 40000, octree_resolution: int = 380) -> None:
        self.max_faces = max_faces
        self.octree_resolution = octree_resolution
        self._shape = None
        self._paint = None
        self._cleanup = None

    @property
    def loaded(self) -> bool:
        return self._shape is not None

    def load(self) -> None:
        if self.loaded:
            return
        from hy3dgen.shapegen import DegenerateFaceRemover, FaceReducer, FloaterRemover, Hunyuan3DDiTFlowMatchingPipeline
        from hy3dgen.texgen import Hunyuan3DPaintPipeline

        shape = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(
            SHAPE_REPO, subfolder=SHAPE_SUBFOLDER, use_safetensors=False, device="cuda"
        )
        shape.enable_flashvdm(topk_mode="merge")
        paint = Hunyuan3DPaintPipeline.from_pretrained(PAINT_REPO, subfolder=PAINT_SUBFOLDER)
        paint.enable_model_cpu_offload()  # without this, shape + paint don't fit in 12 GB
        self._shape, self._paint = shape, paint
        self._cleanup = (FloaterRemover(), DegenerateFaceRemover(), FaceReducer())

    def unload(self) -> None:
        self._shape = self._paint = self._cleanup = None
        torch.cuda.empty_cache()

    def generate(self, image, texture: bool, progress) -> "trimesh.Trimesh":
        """image: RGBA PIL image with the background already removed."""
        self.load()
        progress(10, "shape")
        mesh = self._shape(
            image=image,
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
        return self._paint(mesh, image=image)
