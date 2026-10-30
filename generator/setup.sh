#!/usr/bin/env bash
# Builds the local generator environment (Linux, NVIDIA GPU, CUDA toolkit 12.4). Run from anywhere.
set -euo pipefail
cd "$(dirname "$0")"

HUNYUAN_COMMIT=f8db63096c8282cb27354314d896feba5ba6ff8a
CUDA_HOME=${CUDA_HOME:-/usr/local/cuda-12.4}
export CUDA_HOME PATH="$CUDA_HOME/bin:$PATH"
export TORCH_CUDA_ARCH_LIST=${TORCH_CUDA_ARCH_LIST:-8.6}   # 8.6 = RTX 30xx; 8.9 = RTX 40xx

command -v uv >/dev/null || { echo "Install uv first: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
[ -x "$CUDA_HOME/bin/nvcc" ] || { echo "nvcc not found in $CUDA_HOME (set CUDA_HOME)"; exit 1; }

if [ ! -d vendor/Hunyuan3D-2 ]; then
  git clone https://github.com/Tencent-Hunyuan/Hunyuan3D-2.git vendor/Hunyuan3D-2
fi
git -C vendor/Hunyuan3D-2 fetch -q origin "$HUNYUAN_COMMIT" 2>/dev/null || true
git -C vendor/Hunyuan3D-2 checkout -q "$HUNYUAN_COMMIT"

[ -d .venv ] || uv venv --python 3.10 .venv
uv pip install --python .venv/bin/python torch==2.5.1 torchvision==0.20.1 --index-url https://download.pytorch.org/whl/cu124
uv pip install --python .venv/bin/python -r requirements.txt
# Hunyuan's texture baker: a CUDA rasterizer and a C++ mesh painter
uv pip install --python .venv/bin/python --no-build-isolation \
  ./vendor/Hunyuan3D-2/hy3dgen/texgen/custom_rasterizer \
  ./vendor/Hunyuan3D-2/hy3dgen/texgen/differentiable_renderer
echo "Done. Start with: .venv/bin/uvicorn server:app --host 127.0.0.1 --port 8100"
