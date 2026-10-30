"""Feasibility spike: Hunyuan3D-2 shape + texture on a 12 GB GPU. Usage: python spike_hunyuan.py out_dir img1 [img2 ...]"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "vendor" / "Hunyuan3D-2"))

import torch
from PIL import Image

from hy3dgen.rembg import BackgroundRemover
from hy3dgen.shapegen import DegenerateFaceRemover, FaceReducer, FloaterRemover, Hunyuan3DDiTFlowMatchingPipeline
from hy3dgen.texgen import Hunyuan3DPaintPipeline


def gb(n):
    return f"{n / 1024**3:.1f} GB"


out_dir = Path(sys.argv[1])
out_dir.mkdir(parents=True, exist_ok=True)
rembg = BackgroundRemover()

t = time.time()
shape = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(
    "tencent/Hunyuan3D-2mini", subfolder="hunyuan3d-dit-v2-mini-turbo", use_safetensors=False, device="cuda"
)
shape.enable_flashvdm(topk_mode="merge")
paint = Hunyuan3DPaintPipeline.from_pretrained("tencent/Hunyuan3D-2", subfolder="hunyuan3d-paint-v2-0-turbo")
paint.enable_model_cpu_offload()
print(f"models loaded in {time.time() - t:.0f}s, vram {gb(torch.cuda.memory_allocated())}", flush=True)

for path in sys.argv[2:]:
    name = Path(path).stem
    image = Image.open(path)
    if image.mode != "RGBA" or image.getextrema()[3][0] == 255:
        image = rembg(image.convert("RGB"))
    image.save(out_dir / f"{name}-cutout.png")

    torch.cuda.reset_peak_memory_stats()
    t = time.time()
    mesh = shape(image=image, num_inference_steps=5, octree_resolution=380, num_chunks=20000,
                 generator=torch.manual_seed(12345), output_type="trimesh")[0]
    mesh = FloaterRemover()(mesh)
    mesh = DegenerateFaceRemover()(mesh)
    mesh = FaceReducer()(mesh, max_facenum=40000)
    t_shape = time.time() - t
    peak_shape = torch.cuda.max_memory_allocated()
    mesh.export(out_dir / f"{name}-shape.glb")

    torch.cuda.reset_peak_memory_stats()
    t = time.time()
    textured = paint(mesh, image=image)
    t_tex = time.time() - t
    textured.export(out_dir / f"{name}-textured.glb")
    print(f"{name}: shape {t_shape:.0f}s peak {gb(peak_shape)}, faces {len(mesh.faces)} | "
          f"texture {t_tex:.0f}s peak {gb(torch.cuda.max_memory_allocated())}", flush=True)
