#!/usr/bin/env bash
# Creates the isolated ML environment sing.py needs (Demucs + rvc-python).
# rvc-python pins numpy<=1.23.5 / fairseq 0.12.2, which only build on
# Python 3.10, so it gets its own venv instead of polluting the main one.
#
#   bash setup_sing.sh            # NVIDIA GPU (CUDA 11.8 wheels)
#   bash setup_sing.sh cpu        # CPU only (works, ~10-20x slower)
#
# Then:  export SING_TOOLS_PYTHON=$PWD/.sing-venv/bin/python
set -euo pipefail

MODE="${1:-cuda}"
VENV="${SING_VENV:-.sing-venv}"

command -v uv >/dev/null || { echo "Install uv first: https://docs.astral.sh/uv/"; exit 1; }
command -v ffmpeg >/dev/null || { echo "Install ffmpeg first (apt install ffmpeg)"; exit 1; }

uv venv --python 3.10 "$VENV"
PY="$VENV/bin/python"

# fairseq 0.12.2's metadata is rejected by pip>=24.1.
uv pip install --python "$PY" "pip<24.1"

if [ "$MODE" = "cpu" ]; then
  uv pip install --python "$PY" torch==2.1.1 torchaudio==2.1.1 \
    --index-url https://download.pytorch.org/whl/cpu
else
  uv pip install --python "$PY" torch==2.1.1+cu118 torchaudio==2.1.1+cu118 \
    --index-url https://download.pytorch.org/whl/cu118
fi

"$PY" -m pip install rvc-python demucs

# rvc-python downloads hubert_base.pt + rmvpe.pt from Hugging Face on first
# use; do it now so the first cover does not stall.
"$PY" -c "from rvc_python.infer import RVCInference; RVCInference(device='cpu')"

echo
echo "Ready. Add this to your shell profile:"
echo "  export SING_TOOLS_PYTHON=$(cd "$(dirname "$PY")" && pwd)/python"
